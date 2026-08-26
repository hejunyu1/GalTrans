from __future__ import annotations

import io
import json
import unittest
from pathlib import Path

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
            "schema_version": 1,
            "sdk_path": "C:/sdk",
            "project_path": "C:/project",
            "output_path": "C:/output",
            "endpoint": "https://provider.example/v1/chat/completions",
            "model": "test-model",
            "api_key": secret,
        }
    )


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

        exit_code = run_desktop_bridge(io.StringIO(_request()), output, execute)
        events = [json.loads(line) for line in output.getvalue().splitlines()]

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured_request[0].api_key, "desktop-test-secret")
        self.assertEqual(
            events[0],
            {
                "schema_version": 1,
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
        )

        self.assertEqual(exit_code, 1)
        self.assertNotIn(secret, output.getvalue())
        self.assertIn("[凭据已隐藏]", output.getvalue())


if __name__ == "__main__":
    unittest.main()
