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
      schema_version: 1;
      type: "progress";
      stage: TranslationStage;
      message: string;
      completed_batches: number | null;
      total_batches: number | null;
    }
  | {
      schema_version: 1;
      type: "succeeded";
      result: TranslationResult;
    }
  | {
      schema_version: 1;
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
