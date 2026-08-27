from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from galtrans.adapters.renpy.compatibility import (
    RenpyCompatibilityReport,
    RenpyCompatibilityStatus,
)
from galtrans.automated import (
    AutomatedRenpyTranslationProgress,
    AutomatedRenpyTranslationResult,
    AutomatedRenpyTranslationStage,
)
from galtrans.desktop_bridge import run_desktop_bridge
from galtrans.player import PlayerTranslationRequest
from galtrans.qa import TranslationQualityOutcome


def _result() -> AutomatedRenpyTranslationResult:
    return AutomatedRenpyTranslationResult(
        task_id="task_desktop_test",
        segment_count=3,
        batch_count=2,
        quality_outcome=TranslationQualityOutcome.LOW_CONFIDENCE,
        low_confidence_segment_ids=("segment_one",),
        workspace_root=Path("C:/workspace"),
        database_path=Path("C:/workspace/translation.sqlite3"),
        output_root=Path("C:/output"),
        translation_files=(Path("C:/output/game/tl/schinese/script.rpy"),),
        sdk_version="8.5.3.test",
    )


def _request(secret: str = "desktop-test-secret") -> str:
    return json.dumps(
        {
            "schema_version": 2,
            "operation": "translate",
            "sdk_path": "C:/sdk",
            "project_path": "C:/project",
            "output_path": "C:/output",
            "endpoint": "https://provider.example/v1/chat/completions",
            "model": "test-model",
            "api_key": secret,
        }
    )


def _compatibility(
    status: RenpyCompatibilityStatus = RenpyCompatibilityStatus.SOURCE_READY,
) -> RenpyCompatibilityReport:
    source_scripts = (
        ("game/script.rpy",)
        if status is RenpyCompatibilityStatus.SOURCE_READY
        else ()
    )
    compiled_scripts = (
        ("game/script.rpyc",)
        if status is RenpyCompatibilityStatus.PACKAGED_REQUIRES_IMPORT
        else ()
    )
    return RenpyCompatibilityReport(
        schema_version=1,
        selected_root=Path("C:/project"),
        project_root=Path("C:/project"),
        game_directory="game",
        status=status,
        summary="compatibility test",
        source_scripts=source_scripts,
        compiled_scripts=compiled_scripts,
        archives=(),
        translation_files=(),
        launchers=(),
        runtime_markers=(),
        version_hints=(),
        issues=(),
    )


def _ready_compatibility(_path: Path) -> RenpyCompatibilityReport:
    return _compatibility()


class DesktopBridgeTests(unittest.TestCase):
    def test_emits_closed_progress_and_success_json_lines(self) -> None:
        output = io.StringIO()
        captured_request: list[PlayerTranslationRequest] = []

        def execute(
            request: PlayerTranslationRequest,
            progress_callback: object,
        ) -> AutomatedRenpyTranslationResult:
            captured_request.append(request)
            if not callable(progress_callback):
                raise AssertionError("桌面桥接没有提供进度回调")
            progress_callback(
                AutomatedRenpyTranslationProgress(
                    stage=AutomatedRenpyTranslationStage.TRANSLATING,
                    message="翻译批次 1/2",
                    completed_batches=1,
                    total_batches=2,
                )
            )
            return _result()

        exit_code = run_desktop_bridge(
            io.StringIO(_request()),
            output,
            execute,
            _ready_compatibility,
        )
        events = [json.loads(line) for line in output.getvalue().splitlines()]

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured_request[0].api_key, "desktop-test-secret")
        self.assertEqual(
            events[0],
            {
                "schema_version": 2,
                "type": "progress",
                "stage": "translating",
                "message": "翻译批次 1/2",
                "completed_batches": 1,
                "total_batches": 2,
            },
        )
        self.assertEqual(events[1]["type"], "succeeded")
        self.assertEqual(events[1]["result"]["output_root"], "C:\\output")
        self.assertNotIn("desktop-test-secret", output.getvalue())

    def test_translation_rechecks_source_ready_before_executor(self) -> None:
        output = io.StringIO()

        def execute(
            _request: PlayerTranslationRequest,
            _progress_callback: object,
        ) -> AutomatedRenpyTranslationResult:
            raise AssertionError("成品输入不得进入翻译执行器")

        def inspect(_path: Path) -> RenpyCompatibilityReport:
            return _compatibility(RenpyCompatibilityStatus.PACKAGED_REQUIRES_IMPORT)

        exit_code = run_desktop_bridge(
            io.StringIO(_request()),
            output,
            execute,
            inspect,
        )
        event = json.loads(output.getvalue())

        self.assertEqual(exit_code, 1)
        self.assertEqual(event["type"], "failed")
        self.assertIn("兼容性检查未通过", event["message"])

    def test_emits_read_only_compatibility_report_without_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            game = root / "game"
            game.mkdir()
            source = game / "script.rpy"
            source.write_text("label start:\n    pass\n", encoding="utf-8")
            before = source.read_bytes()
            output = io.StringIO()

            def execute(
                _request: PlayerTranslationRequest,
                _progress_callback: object,
            ) -> AutomatedRenpyTranslationResult:
                raise AssertionError("兼容性检查不得进入翻译执行器")

            exit_code = run_desktop_bridge(
                io.StringIO(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "operation": "inspect_renpy_compatibility",
                            "project_path": str(root),
                        }
                    )
                ),
                output,
                execute,
            )
            event = json.loads(output.getvalue())

            self.assertEqual(exit_code, 0)
            self.assertEqual(event["schema_version"], 2)
            self.assertEqual(event["type"], "compatibility_report")
            self.assertEqual(event["report"]["schema_version"], 1)
            self.assertEqual(event["report"]["status"], "source_ready")
            self.assertTrue(event["report"]["can_translate_now"])
            self.assertEqual(source.read_bytes(), before)

    def test_rejects_unknown_fields_before_executor(self) -> None:
        payload = json.loads(_request())
        payload["unexpected"] = True
        output = io.StringIO()

        def execute(
            _request: PlayerTranslationRequest,
            _progress_callback: object,
        ) -> AutomatedRenpyTranslationResult:
            raise AssertionError("无效请求不得进入执行器")

        exit_code = run_desktop_bridge(
            io.StringIO(json.dumps(payload)),
            output,
            execute,
        )
        event = json.loads(output.getvalue())

        self.assertEqual(exit_code, 1)
        self.assertEqual(event["type"], "failed")
        self.assertIn("字段不匹配", event["message"])

    def test_rejects_unknown_compatibility_fields_before_inspection(self) -> None:
        output = io.StringIO()

        def inspect(_path: Path) -> RenpyCompatibilityReport:
            raise AssertionError("无效请求不得进入兼容性检查器")

        exit_code = run_desktop_bridge(
            io.StringIO(
                json.dumps(
                    {
                        "schema_version": 2,
                        "operation": "inspect_renpy_compatibility",
                        "project_path": "C:/project",
                        "unexpected": True,
                    }
                )
            ),
            output,
            compatibility_executor=inspect,
        )
        event = json.loads(output.getvalue())

        self.assertEqual(exit_code, 1)
        self.assertEqual(event["type"], "failed")
        self.assertIn("字段不匹配", event["message"])

    def test_redacts_key_from_unexpected_executor_error(self) -> None:
        secret = "desktop-secret-must-not-leak"
        output = io.StringIO()

        def execute(
            _request: PlayerTranslationRequest,
            _progress_callback: object,
        ) -> AutomatedRenpyTranslationResult:
            raise RuntimeError(f"provider rejected {secret}")

        exit_code = run_desktop_bridge(
            io.StringIO(_request(secret)),
            output,
            execute,
            _ready_compatibility,
        )

        self.assertEqual(exit_code, 1)
        self.assertNotIn(secret, output.getvalue())
        self.assertIn("[凭据已隐藏]", output.getvalue())


if __name__ == "__main__":
    unittest.main()
