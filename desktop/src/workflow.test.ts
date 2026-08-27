import { describe, expect, it } from "vitest";

import {
  compatibilityPresentation,
  compatibleProjectError,
  progressPercent,
  stagePosition,
  suggestOutputPath,
  validateTranslationForm,
} from "./workflow";
import type { RenpyCompatibilityReport } from "./types";

const validForm = {
  sdkPath: "C:\\renpy-sdk",
  projectPath: "C:\\games\\demo",
  outputPath: "D:\\output\\demo-schinese",
  endpoint: "https://provider.example/v1/chat/completions",
  model: "test-model",
  apiKey: "test-secret",
};

function report(
  status: RenpyCompatibilityReport["status"],
): RenpyCompatibilityReport {
  return {
    schema_version: 1,
    selected_root: "C:\\games\\demo",
    project_root: "C:\\games\\demo",
    game_directory: "game",
    status,
    summary: "test",
    can_translate_now: status === "source_ready",
    counts: {
      source_scripts: status === "source_ready" ? 1 : 0,
      compiled_scripts: status === "packaged_requires_import" ? 1 : 0,
      archives: 0,
      translation_files: 0,
      launchers: 0,
    },
    source_scripts: status === "source_ready" ? ["game/script.rpy"] : [],
    compiled_scripts:
      status === "packaged_requires_import" ? ["game/script.rpyc"] : [],
    archives: [],
    translation_files: [],
    launchers: [],
    runtime_markers: [],
    version_hints: [],
    issues: [],
  };
}

describe("player workflow helpers", () => {
  it("accepts a complete safe form and rejects unsafe provider URLs", () => {
    expect(validateTranslationForm(validForm)).toEqual({});
    expect(
      validateTranslationForm({
        ...validForm,
        endpoint: "http://provider.example/v1/chat/completions",
      }).endpoint,
    ).toContain("HTTPS");
    expect(
      validateTranslationForm({ ...validForm, apiKey: "bad key" }).apiKey,
    ).toContain("空白");
  });

  it("suggests a new sibling name without treating the source as output", () => {
    expect(suggestOutputPath("D:\\exports", "C:\\games\\demo")).toBe(
      "D:\\exports\\demo-schinese",
    );
  });

  it("maps translation batches and stable stages to monotonic progress", () => {
    expect(progressPercent("translating", 0, 4)).toBe(25);
    expect(progressPercent("translating", 2, 4)).toBe(48);
    expect(progressPercent("translating", 4, 4)).toBe(70);
    expect(progressPercent("completed")).toBe(100);
    expect(stagePosition("quality_check")).toBeGreaterThan(stagePosition("extracting"));
  });

  it("unlocks only the checked source project and explains packaged input", () => {
    expect(compatibleProjectError(report("source_ready"), validForm.projectPath)).toBeUndefined();
    expect(compatibleProjectError(null, validForm.projectPath)).toContain("先运行");
    expect(
      compatibleProjectError(report("source_ready"), "C:\\games\\other"),
    ).toContain("先运行");

    const packaged = report("packaged_requires_import");
    expect(compatibleProjectError(packaged, validForm.projectPath)).toContain("当前工作台");
    expect(compatibilityPresentation(packaged)).toMatchObject({
      label: "已识别成品，当前不能导入",
      tone: "blocked",
    });
  });
});
