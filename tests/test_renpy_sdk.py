from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from galtrans.adapters.renpy.sdk import (
    RenpySdkError,
    crosscheck_renpy_sdk,
    resolve_renpy_sdk,
)


class RenpySdkTests(unittest.TestCase):
    def _make_sdk(self, root: Path) -> Path:
        sdk_root = root / "download" / "renpy-8.5.3-sdk.7z" / "renpy-8.5.3-sdk"
        (sdk_root / "renpy").mkdir(parents=True)
        (sdk_root / "renpy.exe").write_bytes(b"test executable")
        (sdk_root / "renpy.py").write_text("# test launcher\n", encoding="utf-8")
        return sdk_root

    def _make_project(self, root: Path) -> tuple[Path, bytes]:
        project_root = root / "project"
        game_root = project_root / "game"
        game_root.mkdir(parents=True)
        source = b'''label start:\n    "Hello"\n    menu:\n        "Yes":\n            "Done"\n'''
        (game_root / "script.rpy").write_bytes(source)
        return project_root, source

    def test_resolves_nested_sdk_root_from_outer_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sdk_root = self._make_sdk(root)

            resolved_root, executable = resolve_renpy_sdk(root / "download")

        self.assertEqual(resolved_root, sdk_root)
        self.assertEqual(executable, sdk_root / "renpy.exe")

    def test_rejects_standalone_executable_without_sdk_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "renpy.exe"
            executable.write_bytes(b"not an sdk")

            with self.assertRaisesRegex(RenpySdkError, "缺少 renpy.py"):
                resolve_renpy_sdk(executable)

    def test_crosscheck_uses_temporary_mirror_and_matches_official_counts(self) -> None:
        commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sdk_root = self._make_sdk(root)
            project_root, original_source = self._make_project(root)

            def fake_run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                if command[1:] == ["--version"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout="Ren'Py 8.5.3.26051504\n",
                        stderr="",
                    )

                staged_root = Path(command[1])
                self.assertNotEqual(staged_root, project_root)
                save_index = command.index("--savedir") + 1
                self.assertTrue(Path(command[save_index]).is_relative_to(staged_root))

                if "translate" in command:
                    translation = staged_root / "game" / "tl" / "schinese" / "script.rpy"
                    translation.parent.mkdir(parents=True)
                    translation.write_text(
                        "translate schinese start_first:\n\n"
                        '    # "Hello"\n'
                        '    "Hello"\n\n'
                        "translate schinese start_second:\n\n"
                        '    # "Done"\n'
                        '    "Done"\n\n'
                        "translate schinese strings:\n\n"
                        '    old "Yes"\n'
                        '    new "Yes"\n',
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

                self.assertIn("lint", command)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="\ufeffRen'Py 8.5.3 lint report\n",
                    stderr="",
                )

            with mock.patch(
                "galtrans.adapters.renpy.sdk.subprocess.run",
                side_effect=fake_run,
            ):
                result = crosscheck_renpy_sdk(sdk_root, project_root)

            self.assertEqual((project_root / "game" / "script.rpy").read_bytes(), original_source)
            self.assertFalse((project_root / "game" / "tl").exists())

        self.assertEqual(len(commands), 3)
        self.assertTrue(result.matches)
        self.assertEqual(result.version, "8.5.3.26051504")
        self.assertEqual(result.source_file_count, 1)
        self.assertEqual(result.template_file_count, 1)
        self.assertEqual(result.galtrans_dialogue_count, 2)
        self.assertEqual(result.official_dialogue_count, 2)
        self.assertEqual(result.galtrans_string_count, 1)
        self.assertEqual(result.official_string_count, 1)
        self.assertEqual(result.lint_report, "Ren'Py 8.5.3 lint report")

    def test_rejects_traceback_even_when_sdk_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sdk_root = self._make_sdk(root)
            project_root, _ = self._make_project(root)

            completed = subprocess.CompletedProcess(
                [str(sdk_root / "renpy.exe"), "--version"],
                0,
                stdout="",
                stderr="Traceback (most recent call last):\nPermissionError: denied\n",
            )
            with mock.patch(
                "galtrans.adapters.renpy.sdk.subprocess.run",
                return_value=completed,
            ):
                with self.assertRaisesRegex(RenpySdkError, "退出码 0"):
                    crosscheck_renpy_sdk(sdk_root, project_root)

    def test_reports_count_mismatch_without_modifying_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sdk_root = self._make_sdk(root)
            project_root, original_source = self._make_project(root)

            def fake_run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess[str]:
                if command[1:] == ["--version"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout="Ren'Py 8.5.3.26051504\n",
                        stderr="",
                    )
                if "translate" in command:
                    staged_root = Path(command[1])
                    translation = staged_root / "game" / "tl" / "schinese" / "script.rpy"
                    translation.parent.mkdir(parents=True)
                    translation.write_text(
                        'translate schinese start_only:\n    "Hello"\n',
                        encoding="utf-8",
                    )
                return subprocess.CompletedProcess(command, 0, stdout="lint report\n", stderr="")

            with mock.patch(
                "galtrans.adapters.renpy.sdk.subprocess.run",
                side_effect=fake_run,
            ):
                result = crosscheck_renpy_sdk(sdk_root, project_root)

            self.assertFalse(result.matches)
            self.assertEqual((project_root / "game" / "script.rpy").read_bytes(), original_source)

    def test_rejects_empty_lint_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sdk_root = self._make_sdk(root)
            project_root, _ = self._make_project(root)

            def fake_run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess[str]:
                if command[1:] == ["--version"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout="Ren'Py 8.5.3.26051504\n",
                        stderr="",
                    )
                if "translate" in command:
                    staged_root = Path(command[1])
                    translation = staged_root / "game" / "tl" / "schinese" / "script.rpy"
                    translation.parent.mkdir(parents=True)
                    translation.write_text(
                        'translate schinese start_only:\n    "Hello"\n',
                        encoding="utf-8",
                    )
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch(
                "galtrans.adapters.renpy.sdk.subprocess.run",
                side_effect=fake_run,
            ):
                with self.assertRaisesRegex(RenpySdkError, "没有生成可检查的报告"):
                    crosscheck_renpy_sdk(sdk_root, project_root)


if __name__ == "__main__":
    unittest.main()
