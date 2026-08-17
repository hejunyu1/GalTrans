from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from galtrans.scanner import scan_project


class ScannerTests(unittest.TestCase):
    def test_scans_supported_text_without_modifying_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            script = root / "chapter1.rpy"
            original = 'label start:\n    e "こんにちは"\n'.encode()
            script.write_bytes(original)
            (root / "archive.rpa").write_bytes(b"not a text script")

            result = scan_project(root)

            self.assertEqual(script.read_bytes(), original)
            self.assertEqual(len(result.files), 1)
            self.assertEqual(result.files[0].relative_path, "chapter1.rpy")
            self.assertEqual(result.files[0].engine_hint, "renpy")
            self.assertEqual(result.files[0].line_count, 2)
            self.assertEqual(result.warnings, ())

    def test_reports_unsupported_encoding_as_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "broken.txt").write_bytes(bytes(range(32)))

            result = scan_project(root)

            self.assertEqual(result.files, ())
            self.assertEqual(len(result.warnings), 1)

    def test_ignores_virtual_environment_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            ignored = root / ".venv"
            ignored.mkdir()
            (ignored / "ignored.txt").write_text("ignore me", encoding="utf-8")

            result = scan_project(root)

            self.assertEqual(result.files, ())


if __name__ == "__main__":
    unittest.main()

