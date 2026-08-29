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
    validate_renpy_export,
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
        source = b'''label start:\n    "Hello, [name]"\n    menu:\n        "Yes":\n            "Done"\n'''
        (game_root / "script.rpy").write_bytes(source)
        return project_root, source

    def _make_export(self, root: Path) -> tuple[Path, bytes]:
        export_root = root / "export"
        translation = export_root / "game" / "tl" / "schinese" / "script.rpy"
        translation.parent.mkdir(parents=True)
        contents = (
            b"\xef\xbb\xbftranslate schinese start_first:\n"
            b'    "Translated, [name]"\n'
        )
        translation.write_bytes(contents)
        return export_root, contents

    def test_resolves_nested_sdk_root_from_outer_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sdk_root = self._make_sdk(root)
            expected_sdk_root = sdk_root.resolve()

            resolved_root, executable = resolve_renpy_sdk(root / "download")

        self.assertEqual(resolved_root, expected_sdk_root)
        self.assertEqual(executable, expected_sdk_root / "renpy.exe")

    def test_rejects_standalone_executable_without_sdk_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "renpy.exe"
            executable.write_bytes(b"not an sdk")

            with self.assertRaisesRegex(RenpySdkError, "缺少 renpy.py"):
                resolve_renpy_sdk(executable)

    def test_crosscheck_uses_temporary_mirror_and_matches_official_entries(self) -> None:
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
                        "# game/script.rpy:2\n"
                        "translate schinese start_first:\n\n"
                        '    # "Hello, [name]"\n'
                        '    "Hello, [name]"\n\n'
                        "# game/script.rpy:5\n"
                        "translate schinese start_second:\n\n"
                        '    # "Done"\n'
                        '    "Done"\n\n'
                        "translate schinese strings:\n\n"
                        "    # game/script.rpy:4\n"
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
        self.assertEqual(result.mapped_segment_count, 3)
        self.assertEqual(result.unmatched_segment_ids, ())
        self.assertEqual(result.unmatched_template_entries, ())
        self.assertEqual(result.template_warnings, ())
        self.assertEqual(
            [mapping.translation_identifier for mapping in result.mappings],
            ["start_first", "start_second", None],
        )
        self.assertEqual(result.mappings[0].source_code, '"Hello, [name]"')
        self.assertEqual(result.mappings[0].protected_tokens, ("[name]",))
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

    def test_same_counts_with_wrong_text_do_not_match(self) -> None:
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
                        "# game/script.rpy:2\n"
                        "translate schinese start_first:\n\n"
                        '    # "Wrong"\n'
                        '    "Wrong"\n\n'
                        "# game/script.rpy:5\n"
                        "translate schinese start_second:\n\n"
                        '    # "Done"\n'
                        '    "Done"\n\n'
                        "translate schinese strings:\n\n"
                        "    # game/script.rpy:4\n"
                        '    old "Yes"\n'
                        '    new "Yes"\n',
                        encoding="utf-8",
                    )
                return subprocess.CompletedProcess(command, 0, stdout="lint report\n", stderr="")

            with mock.patch(
                "galtrans.adapters.renpy.sdk.subprocess.run",
                side_effect=fake_run,
            ):
                result = crosscheck_renpy_sdk(sdk_root, project_root)

        self.assertEqual(result.galtrans_dialogue_count, result.official_dialogue_count)
        self.assertEqual(result.galtrans_string_count, result.official_string_count)
        self.assertFalse(result.matches)
        self.assertEqual(result.mapped_segment_count, 2)
        self.assertEqual(len(result.unmatched_segment_ids), 1)
        self.assertEqual(len(result.unmatched_template_entries), 1)

    def test_rejects_missing_official_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sdk_root = self._make_sdk(root)
            project_root, _ = self._make_project(root)

            def fake_run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess[str]:
                output = "Ren'Py 8.5.3.26051504\n" if command[1:] == ["--version"] else "ok\n"
                return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

            with mock.patch(
                "galtrans.adapters.renpy.sdk.subprocess.run",
                side_effect=fake_run,
            ):
                with self.assertRaisesRegex(RenpySdkError, "翻译模板目录不存在"):
                    crosscheck_renpy_sdk(sdk_root, project_root)

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

    def test_validates_export_in_writable_temporary_sdk_and_project_copies(self) -> None:
        commands: list[list[str]] = []
        temporary_roots: set[Path] = set()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sdk_root = self._make_sdk(root)
            sdk_sentinel = sdk_root / "renpy" / "common.rpy"
            sdk_sentinel.write_text("# sdk source\n", encoding="utf-8")
            project_root, original_source = self._make_project(root)
            export_root, original_translation = self._make_export(root)

            def fake_run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                copied_executable = Path(command[0])
                self.assertNotEqual(copied_executable, sdk_root / "renpy.exe")
                self.assertEqual(copied_executable.parent.name, "sdk")
                temporary_roots.add(copied_executable.parent.parent)
                if command[1:] == ["--version"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout="Ren'Py 8.5.3.26051504\n",
                        stderr="",
                    )

                staged_project = Path(command[1])
                self.assertNotEqual(staged_project, project_root)
                self.assertTrue(
                    (staged_project / "game" / "tl" / "schinese" / "script.rpy").is_file()
                )
                save_root = Path(command[command.index("--savedir") + 1])
                self.assertTrue(save_root.is_relative_to(copied_executable.parent.parent))
                if "lint" in command:
                    for source in staged_project.rglob("*.rpy"):
                        source.with_suffix(".rpyc").write_bytes(b"lint artifact")
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout="\ufeffRen'Py lint report\n",
                        stderr="",
                    )

                self.assertIn("compile", command)
                self.assertEqual(tuple(staged_project.rglob("*.rpyc")), ())
                for source in staged_project.rglob("*.rpy"):
                    source.with_suffix(".rpyc").write_bytes(b"compiled")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch(
                "galtrans.adapters.renpy.sdk.subprocess.run",
                side_effect=fake_run,
            ):
                result = validate_renpy_export(sdk_root, project_root, export_root)

            self.assertEqual((project_root / "game" / "script.rpy").read_bytes(), original_source)
            self.assertEqual(
                (export_root / "game" / "tl" / "schinese" / "script.rpy").read_bytes(),
                original_translation,
            )
            self.assertEqual(sdk_sentinel.read_text(encoding="utf-8"), "# sdk source\n")
            self.assertFalse((project_root / "game" / "script.rpyc").exists())

        self.assertEqual(len(commands), 3)
        self.assertTrue(all(not path.exists() for path in temporary_roots))
        self.assertEqual(result.version, "8.5.3.26051504")
        self.assertEqual(result.source_file_count, 1)
        self.assertEqual(result.translation_file_count, 1)
        self.assertEqual(result.compiled_file_count, 2)
        self.assertEqual(result.lint_report, "Ren'Py lint report")

    def test_export_validation_rejects_overlapping_or_unexpected_export_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sdk_root = self._make_sdk(root)
            project_root, _ = self._make_project(root)

            with self.assertRaisesRegex(RenpySdkError, "相互独立"):
                validate_renpy_export(
                    sdk_root,
                    project_root,
                    project_root / "game",
                )

            export_root, _ = self._make_export(root)
            unexpected = export_root / "notes.txt"
            unexpected.write_text("not an exported script", encoding="utf-8")
            with self.assertRaisesRegex(RenpySdkError, "语言目录以外"):
                validate_renpy_export(sdk_root, project_root, export_root)

    def test_export_validation_requires_compile_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            sdk_root = self._make_sdk(root)
            project_root, _ = self._make_project(root)
            export_root, _ = self._make_export(root)

            def fake_run(
                command: list[str],
                **_: object,
            ) -> subprocess.CompletedProcess[str]:
                if command[1:] == ["--version"]:
                    output = "Ren'Py 8.5.3.26051504\n"
                elif "lint" in command:
                    output = "lint report\n"
                else:
                    output = ""
                return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

            with mock.patch(
                "galtrans.adapters.renpy.sdk.subprocess.run",
                side_effect=fake_run,
            ):
                with self.assertRaisesRegex(RenpySdkError, "未生成预期编译文件"):
                    validate_renpy_export(sdk_root, project_root, export_root)


if __name__ == "__main__":
    unittest.main()
