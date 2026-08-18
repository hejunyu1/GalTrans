from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from galtrans.adapters.renpy.launch import (
    RenpySdkError,
    _WindowEvidence,
    validate_renpy_launch,
)


class _FakeProcess:
    def __init__(
        self,
        command: list[str],
        *,
        wait_results: list[int | BaseException] | None = None,
        returncode: int | None = None,
        **kwargs: object,
    ) -> None:
        self.command = command
        self.kwargs = kwargs
        self.pid = 4242
        self._handle = 4242
        self.returncode = returncode
        self.wait_results = list(wait_results or [0])
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, *, timeout: float) -> int:
        result = self.wait_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        self.returncode = result
        return result

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1


class _FakeJob:
    instances: ClassVar[list[_FakeJob]] = []

    def __init__(self, process: _FakeProcess) -> None:
        self.process = process
        self.closed = False
        self.instances.append(self)

    def close(self, *, timeout_seconds: float = 2.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.closed = True


class RenpyLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeJob.instances.clear()

    def _make_inputs(self, root: Path) -> tuple[Path, Path, Path, bytes, bytes]:
        sdk_root = root / "sdk"
        (sdk_root / "renpy").mkdir(parents=True)
        (sdk_root / "renpy.exe").write_bytes(b"fake executable")
        (sdk_root / "renpy.py").write_text("# fake launcher\n", encoding="utf-8")

        project_root = root / "project"
        source = b'label start:\n    "Hello"\n'
        (project_root / "game").mkdir(parents=True)
        (project_root / "game" / "script.rpy").write_bytes(source)

        export_root = root / "export"
        translation = (
            '\ufefftranslate schinese strings:\n    old "Hello"\n    new "你好"\n'
        ).encode()
        translation_path = export_root / "game" / "tl" / "schinese" / "script.rpy"
        translation_path.parent.mkdir(parents=True)
        translation_path.write_bytes(translation)
        return sdk_root, project_root, export_root, source, translation

    def test_launch_uses_isolated_paths_and_closes_observed_window(self) -> None:
        processes: list[_FakeProcess] = []
        temporary_roots: set[Path] = set()

        def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
            process = _FakeProcess(command, **kwargs)
            processes.append(process)
            temporary_roots.add(Path(command[0]).parent.parent)
            harness = Path(command[1]) / "game" / "_galtrans_launch_validation.rpy"
            self.assertIn("Quit(confirm=False)", harness.read_text(encoding="utf-8"))
            return process

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sdk_root, project_root, export_root, source, translation = self._make_inputs(root)
            with (
                mock.patch(
                    "galtrans.adapters.renpy.launch._renpy_version",
                    return_value="8.5.3.26051504",
                ),
                mock.patch(
                    "galtrans.adapters.renpy.launch.subprocess.Popen",
                    side_effect=fake_popen,
                ),
                mock.patch("galtrans.adapters.renpy.launch._WindowsJob", _FakeJob),
                mock.patch(
                    "galtrans.adapters.renpy.launch._visible_window",
                    return_value=_WindowEvidence(10, "GalTrans sample", 1280, 720),
                ),
                mock.patch("galtrans.adapters.renpy.launch._request_window_close") as close,
                mock.patch("galtrans.adapters.renpy.launch.time.sleep"),
            ):
                result = validate_renpy_launch(
                    sdk_root,
                    project_root,
                    export_root,
                    stability_seconds=0,
                )

            self.assertEqual((project_root / "game" / "script.rpy").read_bytes(), source)
            self.assertEqual(
                (export_root / "game" / "tl" / "schinese" / "script.rpy").read_bytes(),
                translation,
            )
            self.assertFalse((project_root / "game" / "script.rpyc").exists())

        self.assertEqual(len(processes), 1)
        process = processes[0]
        executable, staged_project, command, savedir_flag, save_root = process.command
        self.assertEqual(command, "run")
        self.assertEqual(savedir_flag, "--savedir")
        self.assertNotEqual(Path(executable), sdk_root / "renpy.exe")
        self.assertNotEqual(Path(staged_project), project_root)
        temporary_root = next(iter(temporary_roots))
        self.assertTrue(Path(staged_project).is_relative_to(temporary_root))
        self.assertTrue(Path(save_root).is_relative_to(Path(executable).parent.parent))
        self.assertEqual(process.kwargs["cwd"], Path(executable).parent)
        environment = process.kwargs["env"]
        self.assertIsInstance(environment, dict)
        self.assertEqual(environment["RENPY_LANGUAGE"], "schinese")
        self.assertEqual(environment["SDL_AUDIODRIVER"], "dummy")
        for name in ("APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
            self.assertTrue(Path(environment[name]).is_relative_to(Path(executable).parent.parent))
        close.assert_called_once_with(process.pid)
        self.assertTrue(_FakeJob.instances[0].closed)
        self.assertEqual(result.window_title, "GalTrans sample")
        self.assertEqual((result.client_width, result.client_height), (1280, 720))
        self.assertEqual(result.shutdown_method, "window-close")
        self.assertTrue(all(not path.exists() for path in temporary_roots))

    def test_normal_exit_before_display_is_failure(self) -> None:
        processes: list[_FakeProcess] = []

        def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
            process = _FakeProcess(command, returncode=0, **kwargs)
            processes.append(process)
            return process

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sdk_root, project_root, export_root, _, _ = self._make_inputs(root)
            with (
                mock.patch(
                    "galtrans.adapters.renpy.launch._renpy_version",
                    return_value="8.5.3.26051504",
                ),
                mock.patch(
                    "galtrans.adapters.renpy.launch.subprocess.Popen",
                    side_effect=fake_popen,
                ),
                mock.patch("galtrans.adapters.renpy.launch._WindowsJob", _FakeJob),
                mock.patch(
                    "galtrans.adapters.renpy.launch._visible_window",
                    return_value=None,
                ),
            ):
                with self.assertRaisesRegex(RenpySdkError, "显示证据出现前退出"):
                    validate_renpy_launch(sdk_root, project_root, export_root)

        self.assertEqual(processes[0].terminate_calls, 0)
        self.assertEqual(processes[0].kill_calls, 0)
        self.assertTrue(_FakeJob.instances[0].closed)

    def test_normal_exit_after_stable_display_is_success(self) -> None:
        processes: list[_FakeProcess] = []

        def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
            process = _FakeProcess(command, **kwargs)
            processes.append(process)
            return process

        def visible_window(_: int) -> _WindowEvidence:
            processes[0].returncode = 0
            return _WindowEvidence(11, "Done", 800, 600)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sdk_root, project_root, export_root, _, _ = self._make_inputs(root)
            with (
                mock.patch(
                    "galtrans.adapters.renpy.launch._renpy_version",
                    return_value="8.5.3.26051504",
                ),
                mock.patch(
                    "galtrans.adapters.renpy.launch.subprocess.Popen",
                    side_effect=fake_popen,
                ),
                mock.patch("galtrans.adapters.renpy.launch._WindowsJob", _FakeJob),
                mock.patch(
                    "galtrans.adapters.renpy.launch._visible_window",
                    side_effect=visible_window,
                ),
                mock.patch("galtrans.adapters.renpy.launch.time.sleep"),
            ):
                result = validate_renpy_launch(
                    sdk_root,
                    project_root,
                    export_root,
                    stability_seconds=0,
                )

        self.assertEqual(result.shutdown_method, "normal-exit")
        self.assertTrue(_FakeJob.instances[0].closed)

    def test_nonzero_exit_after_stable_display_is_failure(self) -> None:
        processes: list[_FakeProcess] = []

        def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
            process = _FakeProcess(command, **kwargs)
            processes.append(process)
            return process

        def visible_window(_: int) -> _WindowEvidence:
            processes[0].returncode = 7
            return _WindowEvidence(13, "Stopped", 800, 600)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sdk_root, project_root, export_root, _, _ = self._make_inputs(root)
            with (
                mock.patch(
                    "galtrans.adapters.renpy.launch._renpy_version",
                    return_value="8.5.3.26051504",
                ),
                mock.patch(
                    "galtrans.adapters.renpy.launch.subprocess.Popen",
                    side_effect=fake_popen,
                ),
                mock.patch("galtrans.adapters.renpy.launch._WindowsJob", _FakeJob),
                mock.patch(
                    "galtrans.adapters.renpy.launch._visible_window",
                    side_effect=visible_window,
                ),
                mock.patch("galtrans.adapters.renpy.launch.time.sleep"),
            ):
                with self.assertRaisesRegex(RenpySdkError, "显示成功后异常退出"):
                    validate_renpy_launch(
                        sdk_root,
                        project_root,
                        export_root,
                        stability_seconds=0,
                    )

        self.assertTrue(_FakeJob.instances[0].closed)

    def test_timeout_escalates_to_terminate_then_kill_and_closes_job(self) -> None:
        processes: list[_FakeProcess] = []
        wait_results: list[int | BaseException] = [
            subprocess.TimeoutExpired("renpy", 1),
            subprocess.TimeoutExpired("renpy", 1),
            1,
        ]

        def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
            process = _FakeProcess(command, wait_results=wait_results, **kwargs)
            processes.append(process)
            return process

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sdk_root, project_root, export_root, _, _ = self._make_inputs(root)
            with (
                mock.patch(
                    "galtrans.adapters.renpy.launch._renpy_version",
                    return_value="8.5.3.26051504",
                ),
                mock.patch(
                    "galtrans.adapters.renpy.launch.subprocess.Popen",
                    side_effect=fake_popen,
                ),
                mock.patch("galtrans.adapters.renpy.launch._WindowsJob", _FakeJob),
                mock.patch(
                    "galtrans.adapters.renpy.launch._visible_window",
                    return_value=None,
                ),
                mock.patch(
                    "galtrans.adapters.renpy.launch.time.monotonic",
                    side_effect=[0.0, 2.0],
                ),
                mock.patch("galtrans.adapters.renpy.launch._request_window_close"),
            ):
                with self.assertRaisesRegex(RenpySdkError, "未出现稳定的可见窗口"):
                    validate_renpy_launch(
                        sdk_root,
                        project_root,
                        export_root,
                        timeout_seconds=1,
                        shutdown_grace_seconds=1,
                    )

        self.assertEqual(processes[0].terminate_calls, 1)
        self.assertEqual(processes[0].kill_calls, 1)
        self.assertTrue(_FakeJob.instances[0].closed)

    def test_visible_error_window_does_not_count_as_display_success(self) -> None:
        def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
            (Path(command[1]) / "traceback.txt").write_text(
                "Traceback (most recent call last):\nRuntimeError: broken\n",
                encoding="utf-8",
            )
            return _FakeProcess(command, **kwargs)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sdk_root, project_root, export_root, _, _ = self._make_inputs(root)
            with (
                mock.patch(
                    "galtrans.adapters.renpy.launch._renpy_version",
                    return_value="8.5.3.26051504",
                ),
                mock.patch(
                    "galtrans.adapters.renpy.launch.subprocess.Popen",
                    side_effect=fake_popen,
                ),
                mock.patch("galtrans.adapters.renpy.launch._WindowsJob", _FakeJob),
                mock.patch(
                    "galtrans.adapters.renpy.launch._visible_window",
                    return_value=_WindowEvidence(12, "Error", 900, 700),
                ),
                mock.patch("galtrans.adapters.renpy.launch._request_window_close"),
                mock.patch("galtrans.adapters.renpy.launch.time.sleep"),
            ):
                with self.assertRaisesRegex(RenpySdkError, "运行时报告了致命错误"):
                    validate_renpy_launch(
                        sdk_root,
                        project_root,
                        export_root,
                        stability_seconds=0,
                    )

    def test_rejects_project_that_occupies_temporary_harness_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sdk_root, project_root, export_root, _, _ = self._make_inputs(root)
            (project_root / "game" / "_galtrans_launch_validation.rpy").write_text(
                "# project-owned file\n",
                encoding="utf-8",
            )
            with (
                mock.patch(
                    "galtrans.adapters.renpy.launch._renpy_version",
                    return_value="8.5.3.26051504",
                ),
                mock.patch("galtrans.adapters.renpy.launch.subprocess.Popen") as popen,
            ):
                with self.assertRaisesRegex(RenpySdkError, "保留脚本路径"):
                    validate_renpy_launch(sdk_root, project_root, export_root)

        popen.assert_not_called()

    def test_job_assignment_failure_kills_unmanaged_process(self) -> None:
        processes: list[_FakeProcess] = []

        def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
            process = _FakeProcess(command, **kwargs)
            processes.append(process)
            return process

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sdk_root, project_root, export_root, _, _ = self._make_inputs(root)
            with (
                mock.patch(
                    "galtrans.adapters.renpy.launch._renpy_version",
                    return_value="8.5.3.26051504",
                ),
                mock.patch(
                    "galtrans.adapters.renpy.launch.subprocess.Popen",
                    side_effect=fake_popen,
                ),
                mock.patch(
                    "galtrans.adapters.renpy.launch._WindowsJob",
                    side_effect=RenpySdkError("job assignment failed"),
                ),
            ):
                with self.assertRaisesRegex(RenpySdkError, "job assignment failed"):
                    validate_renpy_launch(sdk_root, project_root, export_root)

        self.assertEqual(processes[0].kill_calls, 1)
        self.assertEqual(processes[0].returncode, 0)


if __name__ == "__main__":
    unittest.main()
