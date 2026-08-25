from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from galtrans import __version__
from galtrans.adapters.renpy import (
    RenpyExportValidation,
    RenpyLaunchValidation,
    RenpySdkCrosscheck,
)
from galtrans.automated import (
    AutomatedRenpyTranslationError,
    AutomatedRenpyTranslationResult,
)
from galtrans.cli import main
from galtrans.qa import TranslationQualityOutcome


class CliTests(unittest.TestCase):
    def test_doctor_succeeds(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main(["doctor"])

        self.assertEqual(exit_code, 0)
        self.assertIn(f"GalTrans: {__version__}", output.getvalue())
        self.assertIn("Status:   OK", output.getvalue())

    def test_scan_json_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "script.rpy").write_text('e "hello"\n', encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = main(["scan", str(root), "--json"])

        self.assertEqual(exit_code, 0)
        self.assertIn('"engine_hint": "renpy"', output.getvalue())

    def test_extract_renpy_writes_jsonl_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "script.rpy"
            output_path = root / "output" / "segments.jsonl"
            source.write_text('label start:\n    "こんにちは"\n', encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                first_exit_code = main(
                    ["extract-renpy", str(source), "--output", str(output_path)]
                )
            with contextlib.redirect_stderr(io.StringIO()):
                second_exit_code = main(
                    ["extract-renpy", str(source), "--output", str(output_path)]
                )

            contents = output_path.read_text(encoding="utf-8")

        self.assertEqual(first_exit_code, 0)
        self.assertEqual(second_exit_code, 3)
        self.assertIn('"source_text": "こんにちは"', contents)

    def test_check_renpy_sdk_reports_mismatch_with_distinct_exit_code(self) -> None:
        result = RenpySdkCrosscheck(
            sdk_root=Path("C:/sdk"),
            executable=Path("C:/sdk/renpy.exe"),
            version="8.5.3.26051504",
            language="schinese",
            source_file_count=1,
            template_file_count=1,
            galtrans_dialogue_count=2,
            official_dialogue_count=1,
            galtrans_string_count=1,
            official_string_count=1,
            mappings=(),
            unmatched_segment_ids=("seg_missing",),
            unmatched_template_entries=(),
            template_warnings=(),
            lint_report="lint report",
        )
        output = io.StringIO()
        with (
            mock.patch("galtrans.cli.crosscheck_renpy_sdk", return_value=result),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["check-renpy-sdk", "C:/sdk", "C:/project"])

        self.assertEqual(exit_code, 4)
        self.assertIn("交叉验证：不一致", output.getvalue())

    def test_validate_renpy_export_reports_compilation_success(self) -> None:
        result = RenpyExportValidation(
            sdk_root=Path("C:/sdk"),
            version="8.5.3.26051504",
            language="schinese",
            source_file_count=1,
            translation_file_count=1,
            compiled_file_count=2,
            lint_report="lint report",
        )
        output = io.StringIO()
        with (
            mock.patch("galtrans.cli.validate_renpy_export", return_value=result),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(
                [
                    "validate-renpy-export",
                    "C:/sdk",
                    "C:/project",
                    "C:/export",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("导出验证：通过", output.getvalue())
        self.assertIn("2 个项目脚本已生成编译文件", output.getvalue())

    def test_validate_renpy_launch_reports_window_evidence(self) -> None:
        result = RenpyLaunchValidation(
            sdk_root=Path("C:/sdk"),
            version="8.5.3.26051504",
            language="schinese",
            source_file_count=1,
            translation_file_count=1,
            window_title="GalTrans sample",
            client_width=1280,
            client_height=720,
            shutdown_method="window-close",
        )
        output = io.StringIO()
        with (
            mock.patch("galtrans.cli.validate_renpy_launch", return_value=result),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(
                [
                    "validate-renpy-launch",
                    "C:/sdk",
                    "C:/project",
                    "C:/export",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("显示证据：GalTrans sample | 1280 x 720", output.getvalue())
        self.assertIn("启动显示验证：通过", output.getvalue())

    def test_translate_renpy_uses_environment_key_without_printing_it(self) -> None:
        result = AutomatedRenpyTranslationResult(
            task_id="task_" + "a" * 24,
            segment_count=3,
            batch_count=2,
            quality_outcome=TranslationQualityOutcome.LOW_CONFIDENCE,
            low_confidence_segment_ids=("seg_1",),
            workspace_root=Path("C:/workspace"),
            database_path=Path("C:/workspace/translation.sqlite3"),
            output_root=Path("C:/translated"),
            translation_files=(Path("C:/translated/game/tl/schinese/script.rpy"),),
            sdk_version="8.5.3.test",
        )
        output = io.StringIO()
        secret = "cli-secret-key"

        def run_without_secret(
            *_args: object,
            **_kwargs: object,
        ) -> AutomatedRenpyTranslationResult:
            self.assertNotIn("TEST_GALTRANS_KEY", os.environ)
            return result

        with (
            mock.patch.dict("os.environ", {"TEST_GALTRANS_KEY": secret}),
            mock.patch(
                "galtrans.cli.run_automated_renpy_translation",
                side_effect=run_without_secret,
            ) as run,
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(
                [
                    "translate-renpy",
                    "C:/sdk",
                    "C:/project",
                    "C:/translated",
                    "--endpoint",
                    "https://example.com/v1/chat/completions",
                    "--model",
                    "test-model",
                    "--api-key-env",
                    "TEST_GALTRANS_KEY",
                ]
            )
            self.assertEqual(os.environ["TEST_GALTRANS_KEY"], secret)

        self.assertEqual(exit_code, 0)
        self.assertTrue(run.called)
        self.assertIn("自动翻译：3 条文本", output.getvalue())
        self.assertIn("低置信度 1 条", output.getvalue())
        self.assertNotIn(secret, output.getvalue())

    def test_translate_renpy_requires_api_key_environment(self) -> None:
        error = io.StringIO()
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            contextlib.redirect_stderr(error),
        ):
            exit_code = main(
                [
                    "translate-renpy",
                    "C:/sdk",
                    "C:/project",
                    "C:/translated",
                    "--endpoint",
                    "https://example.com/v1/chat/completions",
                    "--model",
                    "test-model",
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("GALTRANS_API_KEY", error.getvalue())

    def test_translate_renpy_restores_key_after_safe_failure(self) -> None:
        secret = "restored-secret-key"
        error = io.StringIO()
        with (
            mock.patch.dict("os.environ", {"TEST_GALTRANS_KEY": secret}),
            mock.patch(
                "galtrans.cli.run_automated_renpy_translation",
                side_effect=AutomatedRenpyTranslationError("safe stop"),
            ),
            contextlib.redirect_stderr(error),
        ):
            exit_code = main(
                [
                    "translate-renpy",
                    "C:/sdk",
                    "C:/project",
                    "C:/translated",
                    "--endpoint",
                    "https://example.com/v1/chat/completions",
                    "--model",
                    "test-model",
                    "--api-key-env",
                    "TEST_GALTRANS_KEY",
                ]
            )
            self.assertEqual(os.environ["TEST_GALTRANS_KEY"], secret)

        self.assertEqual(exit_code, 2)
        self.assertIn("safe stop", error.getvalue())
        self.assertNotIn(secret, error.getvalue())



if __name__ == "__main__":
    unittest.main()
