from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from galtrans.cli import main


class CliTests(unittest.TestCase):
    def test_doctor_succeeds(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main(["doctor"])

        self.assertEqual(exit_code, 0)
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



if __name__ == "__main__":
    unittest.main()
