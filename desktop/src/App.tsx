import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
import { useEffect, useMemo, useState } from "react";
import type { CSSProperties } from "react";

import type {
  FormErrors,
  TranslationEvent,
  TranslationForm,
  TranslationResult,
  TranslationStage,
} from "./types";
import {
  PIPELINE_STAGES,
  progressPercent,
  stagePosition,
  suggestOutputPath,
  validateTranslationForm,
} from "./workflow";

type PathField = "sdkPath" | "projectPath";

const INITIAL_FORM: TranslationForm = {
  sdkPath: "",
  projectPath: "",
  outputPath: "",
  endpoint: "",
  model: "",
  apiKey: "",
};

function FolderIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M3.75 6.75A1.75 1.75 0 0 1 5.5 5h4.1l1.7 1.75h7.2a1.75 1.75 0 0 1 1.75 1.75v8A2.5 2.5 0 0 1 17.75 19H6.25a2.5 2.5 0 0 1-2.5-2.5V6.75Z" />
    </svg>
  );
}

function ShieldIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 3.25 19 6v5.45c0 4.33-2.75 7.75-7 9.3-4.25-1.55-7-4.97-7-9.3V6l7-2.75Z" />
      <path d="m9 12 2 2 4-4" className="icon-cut" />
    </svg>
  );
}

function SparkIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 2.75c.65 4.15 2.85 6.35 7 7-4.15.65-6.35 2.85-7 7-.65-4.15-2.85-6.35-7-7 4.15-.65 6.35-2.85 7-7Z" />
      <path d="M18.2 15.1c.3 1.95 1.35 3 3.3 3.3-1.95.3-3 1.35-3.3 3.3-.3-1.95-1.35-3-3.3-3.3 1.95-.3 3-1.35 3.3-3.3Z" />
    </svg>
  );
}

interface FieldProps {
  label: string;
  value: string;
  placeholder: string;
  error?: string;
  disabled: boolean;
  secret?: boolean;
  onChange: (value: string) => void;
  onBrowse?: () => void;
}

function Field({
  label,
  value,
  placeholder,
  error,
  disabled,
  secret = false,
  onChange,
  onBrowse,
}: FieldProps) {
  return (
    <label className={`field ${error ? "field-error" : ""}`}>
      <span className="field-label">{label}</span>
      <span className="field-control">
        <input
          type={secret ? "password" : "text"}
          value={value}
          placeholder={placeholder}
          disabled={disabled}
          spellCheck={false}
          autoComplete={secret ? "new-password" : "off"}
          onChange={(event) => onChange(event.target.value)}
        />
        {onBrowse ? (
          <button
            type="button"
            className="browse-button"
            onClick={onBrowse}
            disabled={disabled}
            aria-label={`选择${label}`}
          >
            <FolderIcon />
            选择
          </button>
        ) : null}
      </span>
      {error ? <span className="field-message">{error}</span> : null}
    </label>
  );
}

function App() {
  const [form, setForm] = useState<TranslationForm>(INITIAL_FORM);
  const [errors, setErrors] = useState<FormErrors>({});
  const [running, setRunning] = useState(false);
  const [eventsReady, setEventsReady] = useState(false);
  const [stage, setStage] = useState<TranslationStage | null>(null);
  const [status, setStatus] = useState("准备开始");
  const [percent, setPercent] = useState(0);
  const [logs, setLogs] = useState<string[]>([
    "原项目保持只读，只有验证通过后才发布新输出。",
  ]);
  const [result, setResult] = useState<TranslationResult | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const unlisten = listen<TranslationEvent>("translation-event", ({ payload }) => {
      if (!active) return;
      if (payload.type === "progress") {
        setStage(payload.stage);
        setStatus(payload.message);
        setPercent(
          progressPercent(
            payload.stage,
            payload.completed_batches,
            payload.total_batches,
          ),
        );
        setLogs((current) => [...current.slice(-7), payload.message]);
      } else if (payload.type === "succeeded") {
        setStage("completed");
        setStatus("翻译补丁已经安全生成");
        setPercent(100);
        setResult(payload.result);
        setFailure(null);
      } else {
        setStatus("任务安全停止");
        setFailure(payload.message);
        setLogs((current) => [...current.slice(-7), `失败：${payload.message}`]);
      }
    });
    unlisten.then(() => active && setEventsReady(true));
    return () => {
      active = false;
      void unlisten.then((dispose) => dispose());
    };
  }, []);

  const activePosition = stagePosition(stage);
  const completedCount = result?.segment_count ?? 0;
  const lowConfidenceCount = result?.low_confidence_segment_ids.length ?? 0;
  const formComplete = useMemo(
    () => Object.keys(validateTranslationForm(form)).length === 0,
    [form],
  );

  function updateField(field: keyof TranslationForm, value: string) {
    setForm((current) => ({ ...current, [field]: value }));
    setErrors((current) => ({ ...current, [field]: undefined }));
  }

  async function chooseDirectory(field: PathField, title: string) {
    const selected = await open({ directory: true, multiple: false, title });
    if (typeof selected === "string") updateField(field, selected);
  }

  async function chooseOutputParent() {
    const selected = await open({
      directory: true,
      multiple: false,
      title: "选择汉化补丁输出的父目录",
    });
    if (typeof selected === "string") {
      updateField("outputPath", suggestOutputPath(selected, form.projectPath));
    }
  }

  async function startTranslation() {
    const validation = validateTranslationForm(form);
    setErrors(validation);
    if (Object.keys(validation).length > 0 || running || !eventsReady) return;

    const request = { ...form };
    setForm((current) => ({ ...current, apiKey: "" }));
    setRunning(true);
    setResult(null);
    setFailure(null);
    setStage(null);
    setPercent(0);
    setStatus("正在连接安全翻译后端…");
    setLogs(["开始新的自动汉化任务。API key 已从输入框清除。"]);
    try {
      await invoke("start_translation", { request });
    } catch (error) {
      const message = String(error || "桌面后台任务异常结束");
      setFailure(message);
      setStatus("任务安全停止");
      setLogs((current) => [...current.slice(-7), `失败：${message}`]);
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="app-shell">
      <div className="ambient ambient-one" />
      <div className="ambient ambient-two" />

      <header className="topbar">
        <div className="brand">
          <span className="brand-mark"><span /></span>
          <span>
            <strong>GalTrans</strong>
            <small>Visual Novel Localization</small>
          </span>
        </div>
        <div className="preview-badge">
          <span /> Source-only Ren'Py Preview
        </div>
      </header>

      <main className="workspace">
        <section className="configuration-panel">
          <div className="hero-copy">
            <span className="eyebrow">AUTOMATIC WORKSPACE</span>
            <h1>把日文视觉小说<br />变成你的中文补丁</h1>
            <p>一次配置，自动提取、翻译、质检和验证。原游戏目录始终保持只读。</p>
          </div>

          <div className="step-card">
            <div className="step-heading">
              <span className="step-number">01</span>
              <span><strong>选择游戏源文件</strong><small>当前支持带 .rpy/.rpym 的 Ren'Py 项目</small></span>
            </div>
            <div className="field-grid">
              <Field
                label="Ren'Py SDK"
                value={form.sdkPath}
                placeholder="例如 D:\\renpy-8.5.3-sdk"
                error={errors.sdkPath}
                disabled={running}
                onChange={(value) => updateField("sdkPath", value)}
                onBrowse={() => void chooseDirectory("sdkPath", "选择 Ren'Py SDK")}
              />
              <Field
                label="源项目"
                value={form.projectPath}
                placeholder="选择包含 game 目录的项目"
                error={errors.projectPath}
                disabled={running}
                onChange={(value) => updateField("projectPath", value)}
                onBrowse={() => void chooseDirectory("projectPath", "选择 Ren'Py 源项目")}
              />
            </div>
          </div>

          <div className="step-card">
            <div className="step-heading">
              <span className="step-number">02</span>
              <span><strong>连接翻译服务</strong><small>只发送筛选后的文本批次，不发送游戏路径</small></span>
            </div>
            <div className="field-grid two-columns">
              <Field
                label="服务地址"
                value={form.endpoint}
                placeholder="https://…/v1/chat/completions"
                error={errors.endpoint}
                disabled={running}
                onChange={(value) => updateField("endpoint", value)}
              />
              <Field
                label="模型"
                value={form.model}
                placeholder="填写服务提供的模型名"
                error={errors.model}
                disabled={running}
                onChange={(value) => updateField("model", value)}
              />
            </div>
            <Field
              label="API key"
              value={form.apiKey}
              placeholder="只在本次任务的内存中使用"
              error={errors.apiKey}
              disabled={running}
              secret
              onChange={(value) => updateField("apiKey", value)}
            />
            <div className="privacy-note"><ShieldIcon /><span>密钥不会写入命令行、环境变量、数据库或日志</span></div>
          </div>

          <div className="step-card compact-card">
            <div className="step-heading">
              <span className="step-number">03</span>
              <span><strong>设置全新输出</strong><small>已有目录永不覆盖，失败不会发布半成品</small></span>
            </div>
            <Field
              label="输出路径"
              value={form.outputPath}
              placeholder="选择父目录后自动建议新名称"
              error={errors.outputPath}
              disabled={running}
              onChange={(value) => updateField("outputPath", value)}
              onBrowse={() => void chooseOutputParent()}
            />
          </div>

          <button
            className="start-button"
            type="button"
            disabled={running || !eventsReady}
            onClick={() => void startTranslation()}
          >
            <SparkIcon />
            <span>{running ? "自动汉化进行中…" : "开始自动汉化"}<small>{formComplete ? "配置已就绪" : "完成上方配置后开始"}</small></span>
            <span className="button-arrow">→</span>
          </button>
        </section>

        <aside className="progress-panel">
          <div className="progress-header">
            <div><span className="eyebrow">LIVE PIPELINE</span><h2>本次任务</h2></div>
            <span className={`status-dot ${running ? "is-running" : result ? "is-done" : failure ? "is-failed" : ""}`} />
          </div>

          <div className="progress-summary">
            <div
              className="progress-orbit"
              style={{ "--progress": percent } as CSSProperties}
            >
              <span>{percent}<small>%</small></span>
            </div>
            <div><strong>{status}</strong><small>{running ? "请保持窗口开启，任务状态会自动保存" : result ? `已处理 ${completedCount} 条文本` : "等待安全配置"}</small></div>
          </div>

          <div className="pipeline-list">
            {PIPELINE_STAGES.map((item, index) => {
              const complete = index < activePosition || stage === "completed";
              const active = index === activePosition && stage !== "completed";
              return (
                <div className={`pipeline-item ${complete ? "complete" : ""} ${active ? "active" : ""}`} key={item.id}>
                  <span className="pipeline-node">{complete ? "✓" : index + 1}</span>
                  <span><strong>{item.label}</strong><small>{item.detail}</small></span>
                </div>
              );
            })}
          </div>

          {result ? (
            <div className="result-card">
              <span className="result-kicker">验证完成</span>
              <strong>{result.translation_files.length} 个翻译文件已发布</strong>
              <small>Ren'Py {result.sdk_version} · 低置信度 {lowConfidenceCount} 条</small>
              <code title={result.output_root}>{result.output_root}</code>
            </div>
          ) : failure ? (
            <div className="failure-card"><strong>没有发布未验证输出</strong><span>{failure}</span></div>
          ) : (
            <div className="safety-card"><ShieldIcon /><span><strong>安全边界已启用</strong><small>输入只读 · 输出全新 · SDK 隔离验证</small></span></div>
          )}

          <div className="event-log">
            <span className="event-log-title">运行记录</span>
            {logs.slice(-4).map((line, index) => <p key={`${index}-${line}`}>{line}</p>)}
          </div>
        </aside>
      </main>
    </div>
  );
}

export default App;
