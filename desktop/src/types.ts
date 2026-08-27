export type TranslationStage =
  | "preflight"
  | "extracting"
  | "sdk_crosscheck"
  | "translating"
  | "quality_check"
  | "rendering"
  | "validating_export"
  | "publishing"
  | "completed";

export interface TranslationRequest {
  sdkPath: string;
  projectPath: string;
  outputPath: string;
  endpoint: string;
  model: string;
  apiKey: string;
}

export type RenpyCompatibilityStatus =
  | "source_ready"
  | "packaged_requires_import"
  | "uncertain"
  | "not_renpy";

export interface RenpyCompatibilityReport {
  schema_version: 1;
  selected_root: string;
  project_root: string;
  game_directory: string | null;
  status: RenpyCompatibilityStatus;
  summary: string;
  can_translate_now: boolean;
  counts: {
    source_scripts: number;
    compiled_scripts: number;
    archives: number;
    translation_files: number;
    launchers: number;
  };
  source_scripts: string[];
  compiled_scripts: string[];
  archives: string[];
  translation_files: string[];
  launchers: string[];
  runtime_markers: string[];
  version_hints: Array<{ version: string; relative_path: string }>;
  issues: Array<{ code: string; relative_path: string; message: string }>;
}

export interface TranslationResult {
  task_id: string;
  segment_count: number;
  batch_count: number;
  quality_outcome: "clear" | "low_confidence";
  low_confidence_segment_ids: string[];
  workspace_root: string;
  database_path: string;
  output_root: string;
  translation_files: string[];
  sdk_version: string;
}

export type TranslationEvent =
  | {
      schema_version: 2;
      type: "progress";
      stage: TranslationStage;
      message: string;
      completed_batches: number | null;
      total_batches: number | null;
    }
  | {
      schema_version: 2;
      type: "succeeded";
      result: TranslationResult;
    }
  | {
      schema_version: 2;
      type: "failed";
      message: string;
    };

export interface TranslationForm {
  sdkPath: string;
  projectPath: string;
  outputPath: string;
  endpoint: string;
  model: string;
  apiKey: string;
}

export type FormErrors = Partial<Record<keyof TranslationForm, string>>;
