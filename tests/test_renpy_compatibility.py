from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from galtrans.adapters.renpy.compatibility import (
    RENPY_COMPATIBILITY_REPORT_SCHEMA_VERSION,
    RenpyCompatibilityIssueCode,
    RenpyCompatibilityStatus,
    inspect_renpy_compatibility,
)


def _snapshot(root: Path) -> dict[str, tuple[str, int]]:
    return {
        path.relative_to(root).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class RenpyCompatibilityTests(unittest.TestCase):
    def test_source_project_is_ready_and_report_is_closed_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            game = root / "game"
            game.mkdir()
            (game / "z.rpy").write_text("label z:\n    pass\n", encoding="utf-8")
            (game / "a.rpym").write_text("init python:\n    pass\n", encoding="utf-8")
            before = _snapshot(root)

            first = inspect_renpy_compatibility(root)
            second = inspect_renpy_compatibility(root)

            self.assertEqual(_snapshot(root), before)
            self.assertEqual(first, second)
            self.assertEqual(first.status, RenpyCompatibilityStatus.SOURCE_READY)
            self.assertTrue(first.can_translate_now)
            self.assertEqual(first.source_scripts, ("game/a.rpym", "game/z.rpy"))
            self.assertEqual(
                set(first.to_dict()),
                {
                    "schema_version",
                    "selected_root",
                    "project_root",
                    "game_directory",
                    "status",
                    "summary",
                    "can_translate_now",
                    "counts",
                    "source_scripts",
                    "compiled_scripts",
                    "archives",
                    "translation_files",
                    "launchers",
                    "runtime_markers",
                    "version_hints",
                    "issues",
                },
            )
            self.assertEqual(
                first.to_dict()["schema_version"],
                RENPY_COMPATIBILITY_REPORT_SCHEMA_VERSION,
            )

    def test_standard_packaged_game_is_identified_without_opening_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            game = root / "game"
            translation = game / "tl" / "schinese"
            translation.mkdir(parents=True)
            (root / "Example Game.exe").write_bytes(b"self-authored launcher marker")
            (root / "renpy").mkdir()
            (root / "lib").mkdir()
            (game / "archive.rpa").write_bytes(bytes(range(256)))
            (game / "script.rpyc").write_bytes(b"not a real compiled script")
            (translation / "script.rpy").write_text(
                "translate schinese strings:\n    old \"Hello\"\n    new \"你好\"\n",
                encoding="utf-8",
            )
            before = _snapshot(root)

            report = inspect_renpy_compatibility(root)

            self.assertEqual(_snapshot(root), before)
            self.assertEqual(
                report.status,
                RenpyCompatibilityStatus.PACKAGED_REQUIRES_IMPORT,
            )
            self.assertFalse(report.can_translate_now)
            self.assertEqual(report.archives, ("game/archive.rpa",))
            self.assertEqual(report.compiled_scripts, ("game/script.rpyc",))
            self.assertEqual(
                report.translation_files,
                ("game/tl/schinese/script.rpy",),
            )
            self.assertEqual(report.launchers, ("Example Game.exe",))
            self.assertEqual(report.runtime_markers, ("renpy", "lib"))

    def test_reads_only_bounded_explicit_runtime_version_hint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "game").mkdir()
            renpy = root / "renpy"
            renpy.mkdir()
            (root / "game" / "script.rpyc").write_bytes(b"compiled marker")
            (renpy / "__init__.py").write_bytes(
                b"version_tuple = (8, 5, 3, 25082900)\n"
            )

            report = inspect_renpy_compatibility(root)

            self.assertEqual(len(report.version_hints), 1)
            self.assertEqual(report.version_hints[0].version, "8.5.3")
            self.assertEqual(
                report.version_hints[0].relative_path,
                "renpy/__init__.py",
            )

    def test_archive_extension_without_corroboration_is_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            game = root / "game"
            game.mkdir()
            (game / "unrelated.rpa").write_bytes(b"opaque")

            report = inspect_renpy_compatibility(root)

            self.assertEqual(report.status, RenpyCompatibilityStatus.UNCERTAIN)
            self.assertEqual(
                report.issues[-1].code,
                RenpyCompatibilityIssueCode.WEAK_ARCHIVE_EVIDENCE,
            )

    def test_scan_limit_fails_closed_even_after_finding_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            game = root / "game"
            game.mkdir()
            (game / "a.rpy").write_text("label start:\n    pass\n", encoding="utf-8")
            (game / "b.txt").write_text("bounded", encoding="utf-8")

            report = inspect_renpy_compatibility(root, max_entries=1)

            self.assertEqual(report.status, RenpyCompatibilityStatus.UNCERTAIN)
            self.assertIn(
                RenpyCompatibilityIssueCode.ENTRY_LIMIT_REACHED,
                {issue.code for issue in report.issues},
            )

    def test_non_renpy_directory_and_runtime_only_directory_are_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "notes.txt").write_text("ordinary files", encoding="utf-8")

            plain = inspect_renpy_compatibility(root)
            self.assertEqual(plain.status, RenpyCompatibilityStatus.NOT_RENPY)

            (root / "renpy").mkdir()
            (root / "Mystery.exe").write_bytes(b"marker")
            uncertain = inspect_renpy_compatibility(root)
            self.assertEqual(uncertain.status, RenpyCompatibilityStatus.UNCERTAIN)
            self.assertFalse(uncertain.can_translate_now)

    def test_existing_translation_files_never_count_as_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            translation = root / "game" / "tl" / "schinese"
            translation.mkdir(parents=True)
            (translation / "script.rpy").write_text(
                "translate schinese strings:\n    old \"Hello\"\n    new \"你好\"\n",
                encoding="utf-8",
            )

            report = inspect_renpy_compatibility(root)

            self.assertEqual(report.status, RenpyCompatibilityStatus.UNCERTAIN)
            self.assertFalse(report.can_translate_now)
            self.assertEqual(report.source_scripts, ())
            self.assertEqual(
                report.translation_files,
                ("game/tl/schinese/script.rpy",),
            )

    def test_accepts_game_directory_selection_and_rejects_invalid_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            game = root / "game"
            game.mkdir()
            (game / "script.rpy").write_text("label start:\n    pass\n", encoding="utf-8")

            report = inspect_renpy_compatibility(game)

            self.assertEqual(report.project_root, root.resolve())
            self.assertEqual(report.game_directory, "game")
            with self.assertRaisesRegex(ValueError, "max_depth"):
                inspect_renpy_compatibility(root, max_depth=-1)
            with self.assertRaisesRegex(ValueError, "max_entries"):
                inspect_renpy_compatibility(root, max_entries=0)


if __name__ == "__main__":
    unittest.main()
