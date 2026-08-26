use serde::{Deserialize, Serialize};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use tauri::{AppHandle, Emitter, Manager};

const BRIDGE_SCHEMA_VERSION: u32 = 1;
const MAX_BRIDGE_LINE_BYTES: u64 = 256 * 1024;
const MAX_STDERR_BYTES: u64 = 16 * 1024;
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

#[derive(Serialize)]
struct BridgeRequest<'a> {
    schema_version: u32,
    sdk_path: &'a str,
    project_path: &'a str,
    output_path: &'a str,
    endpoint: &'a str,
    model: &'a str,
    api_key: &'a str,
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

fn validate_event(event: &BridgeEvent) -> Result<(), String> {
    let schema_version = match event {
        BridgeEvent::Progress { schema_version, .. }
        | BridgeEvent::Succeeded { schema_version, .. }
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
        BridgeEvent::Failed { message, .. } => {
            checked_text(message, "失败消息", 4000, true)?;
        }
    }
    Ok(())
}

fn repository_root() -> Result<PathBuf, String> {
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    let candidate = manifest.join("..").join("..");
    candidate
        .canonicalize()
        .map_err(|error| format!("无法定位 GalTrans 开发目录：{error}"))
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

fn run_python_bridge(app: AppHandle, request: FrontendTranslationRequest) -> Result<(), String> {
    let repository = repository_root()?;
    let python = repository.join(".venv").join("Scripts").join("python.exe");
    if !python.is_file() {
        return Err(format!("开发版找不到 Python 环境：{}", python.display()));
    }

    let bridge_request = BridgeRequest {
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

    let mut child = Command::new(&python)
        .arg("-m")
        .arg("galtrans.desktop_bridge")
        .current_dir(&repository)
        .env("PYTHONPATH", repository.join("src"))
        .env_remove("GALTRANS_API_KEY")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("无法启动 GalTrans Python 后端：{error}"))?;

    let mut stdin = child.stdin.take().ok_or("无法连接 Python 后端输入")?;
    stdin
        .write_all(&request_json)
        .and_then(|()| stdin.write_all(b"\n"))
        .and_then(|()| stdin.flush())
        .map_err(|error| format!("无法发送桌面翻译请求：{error}"))?;
    drop(stdin);

    let stdout = child.stdout.take().ok_or("无法连接 Python 后端输出")?;
    let stderr = child.stderr.take().ok_or("无法连接 Python 后端错误输出")?;
    let stderr_thread = thread::spawn(move || read_limited_stderr(stderr));

    let mut reader = BufReader::new(stdout);
    let mut saw_terminal = false;
    let mut terminal_succeeded = false;
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
            break if saw_terminal {
                Ok(())
            } else {
                Err("Python 后端结束前没有返回最终结果".into())
            };
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
        if saw_terminal {
            break Err("Python 后端在最终结果后继续输出事件".into());
        }
        let event: BridgeEvent = match serde_json::from_slice(&bytes) {
            Ok(event) => event,
            Err(_) => break Err("Python 后端返回了无效的关闭式 JSON 事件".into()),
        };
        if let Err(error) = validate_event(&event) {
            break Err(error);
        }
        match &event {
            BridgeEvent::Succeeded { .. } => {
                saw_terminal = true;
                terminal_succeeded = true;
            }
            BridgeEvent::Failed { .. } => saw_terminal = true,
            BridgeEvent::Progress { .. } => {}
        }
        if let Err(error) = app.emit("translation-event", &event) {
            break Err(format!("无法向窗口发送翻译事件：{error}"));
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
    bridge_result?;

    if terminal_succeeded && !status.success() {
        return Err(redact_secret(
            &format!("Python 后端成功事件后的退出码异常：{status} {stderr_text}"),
            &request.api_key,
        ));
    }
    if !terminal_succeeded && status.success() {
        return Err("Python 后端报告失败但退出码为成功".into());
    }
    Ok(())
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(AppState::default())
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![start_translation])
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
    }

    #[test]
    fn bridge_events_reject_unknown_stage_and_incomplete_batch_counts() {
        let unknown = BridgeEvent::Progress {
            schema_version: 1,
            stage: "unknown".into(),
            message: "test".into(),
            completed_batches: None,
            total_batches: None,
        };
        assert!(validate_event(&unknown).is_err());

        let incomplete = BridgeEvent::Progress {
            schema_version: 1,
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
            schema_version: 1,
            result: result(),
        };
        assert!(validate_event(&valid).is_ok());

        let mut contradictory_result = result();
        contradictory_result.quality_outcome = "low_confidence".into();
        let contradictory = BridgeEvent::Succeeded {
            schema_version: 1,
            result: contradictory_result,
        };
        assert!(validate_event(&contradictory).is_err());
    }

    #[test]
    fn error_redaction_removes_api_key() {
        let redacted = redact_secret("provider rejected test-secret", "test-secret");
        assert_eq!(redacted, "provider rejected [凭据已隐藏]");
    }
}
