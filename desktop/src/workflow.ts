import type {
  FormErrors,
  RenpyCompatibilityReport,
  TranslationForm,
  TranslationStage,
} from "./types";

export interface CompatibilityPresentation {
  label: string;
  nextStep: string;
  tone: "ready" | "blocked" | "uncertain";
}

export function compatibilityPresentation(
  report: RenpyCompatibilityReport,
): CompatibilityPresentation {
  return {
    source_ready: {
      label: "源码项目可用",
      nextStep: "可以继续选择 Ren'Py SDK、翻译服务和全新输出目录。",
      tone: "ready",
    },
    packaged_requires_import: {
      label: "已识别成品，当前不能导入",
      nextStep:
        "当前工作台不会打开 RPA 或反编译 RPYc。请保留原目录；V0.4.4 的受限导入边界尚未接入界面。",
      tone: "blocked",
    },
    uncertain: {
      label: "证据不足，已安全停止",
      nextStep:
        "请改选游戏安装根目录（通常同时包含 game 和启动器）或其 game 目录，然后重新检查。",
      tone: "uncertain",
    },
    not_renpy: {
      label: "未识别为 Ren'Py",
      nextStep: "请重新选择包含 game 目录的 Ren'Py 游戏或源码项目。",
      tone: "blocked",
    },
  }[report.status] as CompatibilityPresentation;
}

export function compatibleProjectError(
  report: RenpyCompatibilityReport | null,
  projectPath: string,
): string | undefined {
  if (!projectPath.trim()) return "请选择 Ren'Py 游戏或源码项目";
  if (!report || report.project_root !== projectPath) return "请先运行只读兼容性检查";
  if (!report.can_translate_now) return compatibilityPresentation(report).nextStep;
  return undefined;
}

export const PIPELINE_STAGES: ReadonlyArray<{
  id: TranslationStage;
  label: string;
  detail: string;
}> = [
  { id: "preflight", label: "安全预检", detail: "确认输入只读、输出全新" },
  { id: "extracting", label: "提取文本", detail: "识别台词、旁白与选项" },
  { id: "sdk_crosscheck", label: "引擎核对", detail: "与 Ren'Py 官方模板逐条匹配" },
  { id: "translating", label: "自动翻译", detail: "按可恢复批次请求翻译服务" },
  { id: "quality_check", label: "质量检查", detail: "标记原文残留和低置信度" },
  { id: "rendering", label: "生成补丁", detail: "保持变量、标签和脚本结构" },
  { id: "validating_export", label: "隔离验证", detail: "执行 lint 与独立 compile" },
  { id: "publishing", label: "安全发布", detail: "原子发布到全新目录" },
  { id: "completed", label: "完成", detail: "保留可恢复工作区" },
];

function isLoopback(hostname: string): boolean {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1";
}

export function validateTranslationForm(form: TranslationForm): FormErrors {
  const errors: FormErrors = {};
  if (!form.sdkPath.trim()) errors.sdkPath = "请选择 Ren'Py SDK";
  if (!form.projectPath.trim()) errors.projectPath = "请选择 Ren'Py 游戏或源码项目";
  if (!form.outputPath.trim()) errors.outputPath = "请选择一个全新的输出路径";
  if (!form.model.trim()) errors.model = "请填写翻译模型名称";
  if (!form.apiKey) {
    errors.apiKey = "请填写 API key";
  } else if (/\s/.test(form.apiKey)) {
    errors.apiKey = "API key 不能包含空白字符";
  }

  if (!form.endpoint.trim()) {
    errors.endpoint = "请填写翻译服务地址";
  } else {
    try {
      const endpoint = new URL(form.endpoint);
      const safeProtocol =
        endpoint.protocol === "https:" ||
        (endpoint.protocol === "http:" && isLoopback(endpoint.hostname));
      if (!safeProtocol) {
        errors.endpoint = "远程服务必须使用 HTTPS";
      } else if (!endpoint.pathname.replace(/\/$/, "").endsWith("/chat/completions")) {
        errors.endpoint = "地址必须指向 /chat/completions";
      } else if (endpoint.search || endpoint.hash || endpoint.username || endpoint.password) {
        errors.endpoint = "服务地址不能包含账号、查询参数或片段";
      }
    } catch {
      errors.endpoint = "请输入完整有效的服务地址";
    }
  }
  return errors;
}

function pathName(path: string): string {
  const normalized = path.replace(/[\\/]+$/, "");
  return normalized.split(/[\\/]/).pop() || "renpy-game";
}

export function suggestOutputPath(parent: string, projectPath: string): string {
  const separator = parent.includes("\\") ? "\\" : "/";
  const cleanParent = parent.replace(/[\\/]+$/, "");
  return `${cleanParent}${separator}${pathName(projectPath)}-schinese`;
}

export function progressPercent(
  stage: TranslationStage,
  completedBatches: number | null = null,
  totalBatches: number | null = null,
): number {
  if (stage === "translating" && completedBatches !== null && totalBatches) {
    return 25 + Math.round((45 * completedBatches) / totalBatches);
  }
  return {
    preflight: 5,
    extracting: 12,
    sdk_crosscheck: 20,
    translating: 25,
    quality_check: 74,
    rendering: 80,
    validating_export: 88,
    publishing: 96,
    completed: 100,
  }[stage];
}

export function stagePosition(stage: TranslationStage | null): number {
  if (stage === null) return -1;
  return PIPELINE_STAGES.findIndex((item) => item.id === stage);
}
