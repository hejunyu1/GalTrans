use serde::{Deserialize, Serialize};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use tauri::{AppHandle, Emitter, Manager};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

const BRIDGE_SCHEMA_VERSION: u32 = 2;
const MAX_BRIDGE_LINE_BYTES: u64 = 256 * 1024;
const MAX_STDERR_BYTES: u64 = 16 * 1024;
const BACKEND_BINARY_NAME: &str = "galtrans-backend.exe";
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;
const ALLOWED_STAGES: [&str; 9] = [
    "preflight",
    "extracting",
    "sdk_crosscheck",
    "translating",
    "quality_check",
    "rendering",
    "validating_export",
    "publishing",
    "completed",
];
const ALLOWED_COMPATIBILITY_STATUSES: [&str; 4] = [
    "source_ready",
    "packaged_requires_import",
    "uncertain",
    "not_renpy",
];
const ALLOWED_COMPATIBILITY_ISSUES: [&str; 7] = [
    "symlink_skipped",
    "depth_limit_reached",
    "entry_limit_reached",
    "directory_unreadable",
    "mixed_source_and_packaged",
    "weak_archive_evidence",
    "version_hint_unreadable",
];

#[derive(Default)]
struct AppState {
    running: Arc<AtomicBool>,
}

struct RunningGuard(Arc<AtomicBool>);

impl Drop for RunningGuard {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct FrontendTranslationRequest {
    sdk_path: String,
    project_path: String,
    output_path: String,
    endpoint: String,
    model: String,
    api_key: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct FrontendCompatibilityRequest {
    project_path: String,
}

#[derive(Serialize)]
#[serde(tag = "operation", rename_all = "snake_case")]
enum BridgeRequest<'a> {
    Translate {
        schema_version: u32,
        sdk_path: &'a str,
        project_path: &'a str,
        output_path: &'a str,
        endpoint: &'a str,
        model: &'a str,
        api_key: &'a str,
    },
    InspectRenpyCompatibility {
        schema_version: u32,
        project_path: &'a str,
    },
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum BridgeEvent {
    Progress {
        schema_version: u32,
        stage: String,
        message: String,
        completed_batches: Option<u32>,
        total_batches: Option<u32>,
    },
    Succeeded {
        schema_version: u32,
        result: TranslationResult,
    },
    CompatibilityReport {
        schema_version: u32,
        report: CompatibilityReport,
    },
    Failed {
        schema_version: u32,
        message: String,
    },
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct TranslationResult {
    task_id: String,
    segment_count: u32,
    batch_count: u32,
    quality_outcome: String,
    low_confidence_segment_ids: Vec<String>,
    workspace_root: String,
    database_path: String,
    output_root: String,
    translation_files: Vec<String>,
    sdk_version: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct CompatibilityReport {
    schema_version: u32,
    selected_root: String,
    project_root: String,
    game_directory: Option<String>,
    status: String,
    summary: String,
    can_translate_now: bool,
    counts: CompatibilityCounts,
    source_scripts: Vec<String>,
    compiled_scripts: Vec<String>,
    archives: Vec<String>,
    translation_files: Vec<String>,
    launchers: Vec<String>,
    runtime_markers: Vec<String>,
    version_hints: Vec<CompatibilityVersionHint>,
    issues: Vec<CompatibilityIssue>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct CompatibilityCounts {
    source_scripts: u32,
    compiled_scripts: u32,
    archives: u32,
    translation_files: u32,
    launchers: u32,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct CompatibilityVersionHint {
    version: String,
    relative_path: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct CompatibilityIssue {
    code: String,
    relative_path: String,
    message: String,
}

fn checked_text(
    value: &str,
    field: &str,
    maximum: usize,
    allow_whitespace: bool,
) -> Result<(), String> {
    if value.is_empty() || value.len() > maximum {
        return Err(format!("{field} 长度无效"));
    }
    if value.chars().any(char::is_control) {
        return Err(format!("{field} 不能包含控制字符"));
    }
    if !allow_whitespace && value.trim() != value {
        return Err(format!("{field} 不能包含首尾空白"));
    }
    Ok(())
}

fn validate_request(request: &FrontendTranslationRequest) -> Result<(), String> {
    checked_text(&request.sdk_path, "Ren'Py SDK", 32767, false)?;
    checked_text(&request.project_path, "源项目", 32767, false)?;
    checked_text(&request.output_path, "输出目录", 32767, false)?;
    checked_text(&request.endpoint, "翻译服务 URL", 2048, false)?;
    checked_text(&request.model, "模型", 200, false)?;
    checked_text(&request.api_key, "API key", 4096, true)?;
    Ok(())
}

fn validate_compatibility_request(request: &FrontendCompatibilityRequest) -> Result<(), String> {
    checked_text(&request.project_path, "游戏目录", 32767, false)
}

fn validate_compatibility_report(report: &CompatibilityReport) -> Result<(), String> {
    if report.schema_version != 1 {
        return Err("Python 桥接兼容性报告版本不受支持".into());
    }
    checked_text(&report.selected_root, "所选目录", 32767, true)?;
    checked_text(&report.project_root, "项目目录", 32767, true)?;
    checked_text(&report.summary, "兼容性摘要", 2000, true)?;
    if let Some(game_directory) = &report.game_directory {
        checked_text(game_directory, "game 目录", 32767, true)?;
    }
    if !ALLOWED_COMPATIBILITY_STATUSES.contains(&report.status.as_str()) {
        return Err("Python 桥接返回未知兼容性状态".into());
    }
    if report.can_translate_now != (report.status == "source_ready") {
        return Err("Python 桥接兼容性状态与可翻译标记矛盾".into());
    }
    if report.counts.source_scripts as usize != report.source_scripts.len()
        || report.counts.compiled_scripts as usize != report.compiled_scripts.len()
        || report.counts.archives as usize != report.archives.len()
        || report.counts.translation_files as usize != report.translation_files.len()
        || report.counts.launchers as usize != report.launchers.len()
    {
        return Err("Python 桥接兼容性报告计数不一致".into());
    }
    if report.status == "source_ready" && report.source_scripts.is_empty() {
        return Err("Python 桥接可翻译报告没有源脚本".into());
    }
    if report.status == "packaged_requires_import"
        && report.compiled_scripts.is_empty()
        && report.archives.is_empty()
    {
        return Err("Python 桥接成品报告没有成品文件证据".into());
    }

    for path in report
        .source_scripts
        .iter()
        .chain(&report.compiled_scripts)
        .chain(&report.archives)
        .chain(&report.translation_files)
        .chain(&report.launchers)
        .chain(&report.runtime_markers)
    {
        checked_text(path, "兼容性报告路径", 32767, true)?;
    }
    for hint in &report.version_hints {
        checked_text(&hint.version, "Ren'Py 版本线索", 200, false)?;
        checked_text(&hint.relative_path, "版本线索路径", 32767, true)?;
    }
    for issue in &report.issues {
        if !ALLOWED_COMPATIBILITY_ISSUES.contains(&issue.code.as_str()) {
            return Err("Python 桥接返回未知兼容性问题代码".into());
        }
        checked_text(&issue.relative_path, "兼容性问题路径", 32767, true)?;
        checked_text(&issue.message, "兼容性问题说明", 2000, true)?;
    }
    Ok(())
}

fn validate_event(event: &BridgeEvent) -> Result<(), String> {
    let schema_version = match event {
        BridgeEvent::Progress { schema_version, .. }
        | BridgeEvent::Succeeded { schema_version, .. }
        | BridgeEvent::CompatibilityReport { schema_version, .. }
        | BridgeEvent::Failed { schema_version, .. } => *schema_version,
    };
    if schema_version != BRIDGE_SCHEMA_VERSION {
        return Err("Python 桥接事件版本不受支持".into());
    }
    match event {
        BridgeEvent::Progress {
            stage,
            message,
            completed_batches,
            total_batches,
            ..
        } => {
            if !ALLOWED_STAGES.contains(&stage.as_str()) {
                return Err("Python 桥接返回未知进度阶段".into());
            }
            checked_text(message, "进度消息", 2000, true)?;
            if completed_batches.is_some() != total_batches.is_some() {
                return Err("Python 桥接批次进度不完整".into());
            }
            if let (Some(completed), Some(total)) = (completed_batches, total_batches) {
                if *total == 0 || completed > total {
                    return Err("Python 桥接批次进度越界".into());
                }
            }
        }
        BridgeEvent::Succeeded { result, .. } => {
            checked_text(&result.task_id, "任务 ID", 200, false)?;
            checked_text(&result.workspace_root, "工作区", 32767, true)?;
            checked_text(&result.database_path, "数据库路径", 32767, true)?;
            checked_text(&result.output_root, "输出目录", 32767, true)?;
            checked_text(&result.sdk_version, "SDK 版本", 200, true)?;
            if result.segment_count == 0
                || result.batch_count == 0
                || result.batch_count > result.segment_count
            {
                return Err("Python 桥接成功结果计数无效".into());
            }
            if !matches!(result.quality_outcome.as_str(), "clear" | "low_confidence") {
                return Err("Python 桥接质量结果无效".into());
            }
            let has_low_confidence = !result.low_confidence_segment_ids.is_empty();
            if (result.quality_outcome == "low_confidence") != has_low_confidence
                || result.low_confidence_segment_ids.len() > result.segment_count as usize
            {
                return Err("Python 桥接低置信度结果矛盾".into());
            }
            for segment_id in &result.low_confidence_segment_ids {
                checked_text(segment_id, "低置信度文本 ID", 200, false)?;
            }
            if result.translation_files.is_empty() {
                return Err("Python 桥接没有返回翻译文件".into());
            }
            for translation_file in &result.translation_files {
                checked_text(translation_file, "翻译文件", 32767, true)?;
            }
        }
        BridgeEvent::CompatibilityReport { report, .. } => {
            validate_compatibility_report(report)?;
        }
        BridgeEvent::Failed { message, .. } => {
            checked_text(message, "失败消息", 4000, true)?;
        }
    }
    Ok(())
}

fn backend_executable_path(resource_directory: &Path) -> PathBuf {
    resource_directory.join(BACKEND_BINARY_NAME)
}

fn backend_command(executable: &Path, resource_directory: &Path) -> Command {
    let mut command = Command::new(executable);
    command
        .current_dir(resource_directory)
        .env_remove("GALTRANS_API_KEY")
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONPATH");
    #[cfg(windows)]
    command.creation_flags(CREATE_NO_WINDOW);
    command
}

fn read_limited_stderr<R: Read>(reader: R) -> String {
    let mut bytes = Vec::new();
    let _ = reader.take(MAX_STDERR_BYTES + 1).read_to_end(&mut bytes);
    if bytes.len() > MAX_STDERR_BYTES as usize {
        bytes.truncate(MAX_STDERR_BYTES as usize);
        bytes.extend_from_slice(b"\n[stderr truncated]");
    }
    String::from_utf8_lossy(&bytes).into_owned()
}

fn redact_secret(message: &str, secret: &str) -> String {
    if secret.is_empty() {
        message.to_owned()
    } else {
        message.replace(secret, "[凭据已隐藏]")
    }
}

#[derive(Clone, Copy)]
enum ExpectedOperation {
    Translation,
    Compatibility,
}

#[derive(Clone)]
enum BridgeTerminal {
    TranslationSucceeded,
    CompatibilityReport(CompatibilityReport),
    Failed(String),
}

fn run_bridge<F>(
    app: &AppHandle,
    request_json: &[u8],
    expected_operation: ExpectedOperation,
    secret: &str,
    mut handle_event: F,
) -> Result<BridgeTerminal, String>
where
    F: FnMut(&BridgeEvent) -> Result<(), String>,
{
    let resource_directory = app
        .path()
        .resource_dir()
        .map_err(|error| format!("无法定位 GalTrans 应用资源目录：{error}"))?;
    let backend = backend_executable_path(&resource_directory);
    if !backend.is_file() {
        return Err(format!(
            "找不到 GalTrans 后端 sidecar：{}",
            backend.display()
        ));
    }

    let mut child = backend_command(&backend, &resource_directory)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("无法启动 GalTrans Python 后端：{error}"))?;

    let mut stdin = child.stdin.take().ok_or("无法连接 Python 后端输入")?;
    stdin
        .write_all(request_json)
        .and_then(|()| stdin.write_all(b"\n"))
        .and_then(|()| stdin.flush())
        .map_err(|error| format!("无法发送桌面翻译请求：{error}"))?;
    drop(stdin);

    let stdout = child.stdout.take().ok_or("无法连接 Python 后端输出")?;
    let stderr = child.stderr.take().ok_or("无法连接 Python 后端错误输出")?;
    let stderr_thread = thread::spawn(move || read_limited_stderr(stderr));

    let mut reader = BufReader::new(stdout);
    let mut terminal: Option<BridgeTerminal> = None;
    let bridge_result = loop {
        let mut bytes = Vec::new();
        let read_result = reader
            .by_ref()
            .take(MAX_BRIDGE_LINE_BYTES + 1)
            .read_until(b'\n', &mut bytes);
        let count = match read_result {
            Ok(count) => count,
            Err(error) => break Err(format!("读取 Python 后端事件失败：{error}")),
        };
        if count == 0 {
            break terminal
                .clone()
                .ok_or_else(|| "Python 后端结束前没有返回最终结果".to_string());
        }
        if bytes.len() > MAX_BRIDGE_LINE_BYTES as usize {
            break Err("Python 后端事件超过 256 KiB".into());
        }
        while matches!(bytes.last(), Some(b'\n' | b'\r')) {
            bytes.pop();
        }
        if bytes.is_empty() {
            break Err("Python 后端返回空事件".into());
        }
        if terminal.is_some() {
            break Err("Python 后端在最终结果后继续输出事件".into());
        }
        let event: BridgeEvent = match serde_json::from_slice(&bytes) {
            Ok(event) => event,
            Err(_) => break Err("Python 后端返回了无效的关闭式 JSON 事件".into()),
        };
        if let Err(error) = validate_event(&event) {
            break Err(error);
        }
        match (&event, expected_operation) {
            (BridgeEvent::Progress { .. }, ExpectedOperation::Translation) => {}
            (BridgeEvent::Succeeded { .. }, ExpectedOperation::Translation) => {
                terminal = Some(BridgeTerminal::TranslationSucceeded);
            }
            (BridgeEvent::CompatibilityReport { report, .. }, ExpectedOperation::Compatibility) => {
                terminal = Some(BridgeTerminal::CompatibilityReport(report.clone()));
            }
            (BridgeEvent::Failed { message, .. }, _) => {
                terminal = Some(BridgeTerminal::Failed(message.clone()));
            }
            _ => break Err("Python 后端返回了与请求操作不匹配的事件".into()),
        }
        if let Err(error) = handle_event(&event) {
            break Err(error);
        }
    };

    if bridge_result.is_err() {
        let _ = child.kill();
    }
    let status = child
        .wait()
        .map_err(|error| format!("等待 Python 后端退出失败：{error}"))?;
    let stderr_text = stderr_thread
        .join()
        .map_err(|_| "读取 Python 后端错误输出的线程失败".to_string())?;
    let terminal = bridge_result?;

    match (&terminal, status.success()) {
        (BridgeTerminal::TranslationSucceeded | BridgeTerminal::CompatibilityReport(_), false) => {
            return Err(redact_secret(
                &format!("Python 后端成功事件后的退出码异常：{status} {stderr_text}"),
                secret,
            ));
        }
        (BridgeTerminal::Failed(_), true) => {
            return Err("Python 后端报告失败但退出码为成功".into());
        }
        _ => {}
    }
    Ok(terminal)
}

fn run_python_bridge(app: AppHandle, request: FrontendTranslationRequest) -> Result<(), String> {
    let bridge_request = BridgeRequest::Translate {
        schema_version: BRIDGE_SCHEMA_VERSION,
        sdk_path: &request.sdk_path,
        project_path: &request.project_path,
        output_path: &request.output_path,
        endpoint: &request.endpoint,
        model: &request.model,
        api_key: &request.api_key,
    };
    let request_json =
        serde_json::to_vec(&bridge_request).map_err(|_| "无法编码桌面翻译请求".to_string())?;
    let terminal = run_bridge(
        &app,
        &request_json,
        ExpectedOperation::Translation,
        &request.api_key,
        |event| {
            app.emit("translation-event", event)
                .map_err(|error| format!("无法向窗口发送翻译事件：{error}"))
        },
    )?;
    match terminal {
        BridgeTerminal::TranslationSucceeded | BridgeTerminal::Failed(_) => Ok(()),
        BridgeTerminal::CompatibilityReport(_) => Err("Python 后端返回了意外的兼容性报告".into()),
    }
}

fn run_compatibility_bridge(
    app: AppHandle,
    request: FrontendCompatibilityRequest,
) -> Result<CompatibilityReport, String> {
    let bridge_request = BridgeRequest::InspectRenpyCompatibility {
        schema_version: BRIDGE_SCHEMA_VERSION,
        project_path: &request.project_path,
    };
    let request_json =
        serde_json::to_vec(&bridge_request).map_err(|_| "无法编码桌面兼容性请求".to_string())?;
    match run_bridge(
        &app,
        &request_json,
        ExpectedOperation::Compatibility,
        "",
        |_| Ok(()),
    )? {
        BridgeTerminal::CompatibilityReport(report) => Ok(report),
        BridgeTerminal::Failed(message) => Err(message),
        BridgeTerminal::TranslationSucceeded => Err("Python 后端返回了意外的翻译结果".into()),
    }
}

#[tauri::command]
async fn start_translation(
    app: AppHandle,
    request: FrontendTranslationRequest,
) -> Result<(), String> {
    validate_request(&request)?;
    let state = app.state::<AppState>();
    if state
        .running
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .is_err()
    {
        return Err("已有自动汉化任务正在运行".into());
    }
    let running = Arc::clone(&state.running);
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = RunningGuard(running);
        run_python_bridge(app, request)
    })
    .await
    .map_err(|error| format!("桌面后台任务异常结束：{error}"))?
}

#[tauri::command]
async fn inspect_renpy_compatibility(
    app: AppHandle,
    request: FrontendCompatibilityRequest,
) -> Result<CompatibilityReport, String> {
    validate_compatibility_request(&request)?;
    let state = app.state::<AppState>();
    if state
        .running
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .is_err()
    {
        return Err("已有 GalTrans 任务正在运行".into());
    }
    let running = Arc::clone(&state.running);
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = RunningGuard(running);
        run_compatibility_bridge(app, request)
    })
    .await
    .map_err(|error| format!("桌面兼容性检查异常结束：{error}"))?
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(AppState::default())
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            inspect_renpy_compatibility,
            start_translation
        ])
        .setup(|app| {
            if let Some(window) = app.get_webview_window("main") {
                window.set_focus()?;
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running GalTrans desktop application");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request() -> FrontendTranslationRequest {
        FrontendTranslationRequest {
            sdk_path: r"C:\sdk".into(),
            project_path: r"C:\project".into(),
            output_path: r"C:\output".into(),
            endpoint: "https://provider.example/v1/chat/completions".into(),
            model: "test-model".into(),
            api_key: "test-secret".into(),
        }
    }

    fn result() -> TranslationResult {
        TranslationResult {
            task_id: "task_test".into(),
            segment_count: 2,
            batch_count: 1,
            quality_outcome: "clear".into(),
            low_confidence_segment_ids: Vec::new(),
            workspace_root: r"C:\workspace".into(),
            database_path: r"C:\workspace\state.sqlite3".into(),
            output_root: r"C:\output".into(),
            translation_files: vec!["game/tl/schinese/script.rpy".into()],
            sdk_version: "8.5.3".into(),
        }
    }

    #[test]
    fn frontend_request_validation_is_closed_and_bounded() {
        assert!(validate_request(&request()).is_ok());
        let mut invalid = request();
        invalid.api_key = "".into();
        assert!(validate_request(&invalid).is_err());
        invalid = request();
        invalid.output_path = "bad\npath".into();
        assert!(validate_request(&invalid).is_err());

        let compatibility = FrontendCompatibilityRequest {
            project_path: r"C:\game".into(),
        };
        assert!(validate_compatibility_request(&compatibility).is_ok());
    }

    #[test]
    fn bridge_requests_use_explicit_v2_operations() {
        let translation = BridgeRequest::Translate {
            schema_version: BRIDGE_SCHEMA_VERSION,
            sdk_path: r"C:\sdk",
            project_path: r"C:\project",
            output_path: r"C:\output",
            endpoint: "https://provider.example/v1/chat/completions",
            model: "test-model",
            api_key: "test-secret",
        };
        let translation_json = serde_json::to_value(translation).unwrap();
        assert_eq!(translation_json["schema_version"], 2);
        assert_eq!(translation_json["operation"], "translate");

        let compatibility = BridgeRequest::InspectRenpyCompatibility {
            schema_version: BRIDGE_SCHEMA_VERSION,
            project_path: r"C:\project",
        };
        let compatibility_json = serde_json::to_value(compatibility).unwrap();
        assert_eq!(compatibility_json["schema_version"], 2);
        assert_eq!(
            compatibility_json["operation"],
            "inspect_renpy_compatibility"
        );
        assert_eq!(compatibility_json.as_object().unwrap().len(), 3);
    }

    #[test]
    fn bridge_events_reject_unknown_stage_and_incomplete_batch_counts() {
        let unknown = BridgeEvent::Progress {
            schema_version: 2,
            stage: "unknown".into(),
            message: "test".into(),
            completed_batches: None,
            total_batches: None,
        };
        assert!(validate_event(&unknown).is_err());

        let incomplete = BridgeEvent::Progress {
            schema_version: 2,
            stage: "translating".into(),
            message: "test".into(),
            completed_batches: Some(1),
            total_batches: None,
        };
        assert!(validate_event(&incomplete).is_err());
    }

    #[test]
    fn bridge_success_rejects_contradictory_quality_results() {
        let valid = BridgeEvent::Succeeded {
            schema_version: 2,
            result: result(),
        };
        assert!(validate_event(&valid).is_ok());

        let mut contradictory_result = result();
        contradictory_result.quality_outcome = "low_confidence".into();
        let contradictory = BridgeEvent::Succeeded {
            schema_version: 2,
            result: contradictory_result,
        };
        assert!(validate_event(&contradictory).is_err());
    }

    fn compatibility_report() -> CompatibilityReport {
        CompatibilityReport {
            schema_version: 1,
            selected_root: r"C:\project".into(),
            project_root: r"C:\project".into(),
            game_directory: Some("game".into()),
            status: "source_ready".into(),
            summary: "发现可读的 Ren'Py 源脚本。".into(),
            can_translate_now: true,
            counts: CompatibilityCounts {
                source_scripts: 1,
                compiled_scripts: 0,
                archives: 0,
                translation_files: 0,
                launchers: 0,
            },
            source_scripts: vec!["game/script.rpy".into()],
            compiled_scripts: Vec::new(),
            archives: Vec::new(),
            translation_files: Vec::new(),
            launchers: Vec::new(),
            runtime_markers: Vec::new(),
            version_hints: Vec::new(),
            issues: Vec::new(),
        }
    }

    #[test]
    fn compatibility_report_validation_is_closed_and_consistent() {
        let valid = BridgeEvent::CompatibilityReport {
            schema_version: 2,
            report: compatibility_report(),
        };
        assert!(validate_event(&valid).is_ok());

        let mut contradictory_report = compatibility_report();
        contradictory_report.can_translate_now = false;
        let contradictory = BridgeEvent::CompatibilityReport {
            schema_version: 2,
            report: contradictory_report,
        };
        assert!(validate_event(&contradictory).is_err());

        let mut unknown_issue_report = compatibility_report();
        unknown_issue_report.issues.push(CompatibilityIssue {
            code: "unknown".into(),
            relative_path: "game".into(),
            message: "test".into(),
        });
        let unknown_issue = BridgeEvent::CompatibilityReport {
            schema_version: 2,
            report: unknown_issue_report,
        };
        assert!(validate_event(&unknown_issue).is_err());
    }

    #[test]
    fn error_redaction_removes_api_key() {
        let redacted = redact_secret("provider rejected test-secret", "test-secret");
        assert_eq!(redacted, "provider rejected [凭据已隐藏]");
    }

    #[test]
    fn backend_command_uses_only_the_packaged_sidecar() {
        let resource_directory = Path::new(r"C:\Program Files\GalTrans");
        let executable = backend_executable_path(resource_directory);
        let command = backend_command(&executable, resource_directory);

        assert_eq!(executable, resource_directory.join("galtrans-backend.exe"));
        assert_eq!(command.get_program(), executable.as_os_str());
        assert_eq!(command.get_args().count(), 0);
        assert_eq!(command.get_current_dir(), Some(resource_directory));

        let removed_environment: Vec<_> = command
            .get_envs()
            .filter_map(|(name, value)| value.is_none().then_some(name))
            .collect();
        assert!(removed_environment.contains(&std::ffi::OsStr::new("GALTRANS_API_KEY")));
        assert!(removed_environment.contains(&std::ffi::OsStr::new("PYTHONHOME")));
        assert!(removed_environment.contains(&std::ffi::OsStr::new("PYTHONPATH")));
    }
}
