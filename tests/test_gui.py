from __future__ import annotations

import os
import queue
import threading
import unittest
from pathlib import Path
from unittest import mock

from galtrans.automated import (
    AutomatedRenpyTranslationProgress,
    AutomatedRenpyTranslationStage,
)
from galtrans.gui import (
    GalTransApp,
    PlayerTranslationRequest,
    PlayerTranslationWorker,
    PlayerWorkerEventKind,
    execute_player_translation,
    gui_environment_version,
    main,
)


class _BackendStub:
    identity = "gui-backend-test-v1"


class _WidgetStub:
    def __init__(self) -> None:
        self.state = "normal"

    def configure(self, *, state: str) -> None:
        self.state = state


class PlayerGuiTests(unittest.TestCase):
    def test_check_mode_uses_tcl_without_opening_a_window(self) -> None:
        self.assertRegex(gui_environment_version(), r"^\d+\.\d+")
        with mock.patch("builtins.print") as output:
            self.assertEqual(main(["--check"]), 0)
        output.assert_called_once()
        self.assertIn("OK", output.call_args.args[0])

    def test_execute_request_keeps_key_out_of_environment_and_application_arguments(
        self,
    ) -> None:
        secret = "test-secret-never-persist"
        request = PlayerTranslationRequest(
            sdk_path=Path("C:/renpy-sdk"),
            project_path=Path("C:/game-source"),
            output_path=Path("C:/translated-output"),
            endpoint="https://provider.example/v1/chat/completions",
            model="test-model",
            api_key=secret,
        )
        captured_backend: dict[str, object] = {}

        def backend_factory(**kwargs: object) -> _BackendStub:
            captured_backend.update(kwargs)
            return _BackendStub()

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch(
                "galtrans.player.OpenAICompatibleChatBackend",
                side_effect=backend_factory,
            ),
            mock.patch(
                "galtrans.player.run_automated_renpy_translation",
                return_value=mock.sentinel.result,
            ) as automated,
        ):
            result = execute_player_translation(request)
            self.assertNotIn(secret, os.environ.values())

        self.assertIs(result, mock.sentinel.result)
        self.assertEqual(captured_backend["api_key"], secret)
        call = automated.call_args
        self.assertNotIn(secret, call.args)
        self.assertNotIn(secret, call.kwargs.values())
        self.assertEqual(
            call.args[3],
            Path("C:/.translated-output.galtrans"),
        )

    def test_worker_uses_background_thread_for_progress_and_success(self) -> None:
        events: queue.Queue = queue.Queue()
        main_thread = threading.get_ident()
        executor_thread: list[int] = []
        request = PlayerTranslationRequest(
            sdk_path=Path("sdk"),
            project_path=Path("project"),
            output_path=Path("output"),
            endpoint="https://provider.example/v1/chat/completions",
            model="test-model",
            api_key="worker-secret",
        )

        def executor(
            _request: PlayerTranslationRequest,
            progress_callback: object,
        ) -> object:
            executor_thread.append(threading.get_ident())
            if not callable(progress_callback):
                raise AssertionError("缺少进度回调")
            progress_callback(
                AutomatedRenpyTranslationProgress(
                    stage=AutomatedRenpyTranslationStage.EXTRACTING,
                    message="提取中",
                )
            )
            return mock.sentinel.result

        worker = PlayerTranslationWorker(request, events, executor=executor)
        worker.start()
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertNotEqual(executor_thread, [main_thread])
        self.assertEqual(events.get_nowait().kind, PlayerWorkerEventKind.PROGRESS)
        success = events.get_nowait()
        self.assertEqual(success.kind, PlayerWorkerEventKind.SUCCEEDED)
        self.assertIs(success.result, mock.sentinel.result)
        self.assertIsNone(worker._request)

    def test_running_state_disables_and_restores_all_controls(self) -> None:
        app = object.__new__(GalTransApp)
        first = _WidgetStub()
        second = _WidgetStub()
        start = _WidgetStub()
        app._config_widgets = [first, second]
        app.start_button = start

        app._set_running(True)
        self.assertTrue(app._running)
        self.assertEqual((first.state, second.state, start.state), ("disabled",) * 3)

        app._set_running(False)
        self.assertFalse(app._running)
        self.assertEqual((first.state, second.state, start.state), ("normal",) * 3)

    def test_worker_redacts_api_key_from_failure_event(self) -> None:
        events: queue.Queue = queue.Queue()
        secret = "do-not-show-this-key"
        request = PlayerTranslationRequest(
            sdk_path=Path("sdk"),
            project_path=Path("project"),
            output_path=Path("output"),
            endpoint="https://provider.example/v1/chat/completions",
            model="test-model",
            api_key=secret,
        )

        def executor(
            _request: PlayerTranslationRequest,
            _progress_callback: object,
        ) -> object:
            raise RuntimeError(f"provider rejected {secret}")

        worker = PlayerTranslationWorker(request, events, executor=executor)
        worker.start()
        worker.join(timeout=2)

        failure = events.get_nowait()
        self.assertEqual(failure.kind, PlayerWorkerEventKind.FAILED)
        self.assertNotIn(secret, failure.error_message or "")
        self.assertIn("[凭据已隐藏]", failure.error_message or "")
        self.assertIsNone(worker._request)


if __name__ == "__main__":
    unittest.main()
