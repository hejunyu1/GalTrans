from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from galtrans.adapters.renpy import RenpySdkCrosscheck
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



if __name__ == "__main__":
    unittest.main()
