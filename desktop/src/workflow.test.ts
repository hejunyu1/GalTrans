import { describe, expect, it } from "vitest";

import {
  progressPercent,
  stagePosition,
  suggestOutputPath,
  validateTranslationForm,
} from "./workflow";

const validForm = {
  sdkPath: "C:\\renpy-sdk",
  projectPath: "C:\\games\\demo",
  outputPath: "D:\\output\\demo-schinese",
  endpoint: "https://provider.example/v1/chat/completions",
  model: "test-model",
  apiKey: "test-secret",
};

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
});
