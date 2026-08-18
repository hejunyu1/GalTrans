from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from galtrans.adapters.renpy import read_official_translation_templates
from galtrans.ir import SegmentKind


class RenpyTemplateTests(unittest.TestCase):
    def test_reads_dialogue_narration_and_menu_entries(self) -> None:
        template = '''\
# game/script.rpy:8
translate schinese start_first:

    # aoi "こんにちは"
    aoi "こんにちは"

# game/script.rpy:9
translate schinese start_second:

    # "暗闇だった。"
    "暗闇だった。"

translate schinese strings:

    # game/script.rpy:12
    old "返事をする"
    new "返事をする"
'''
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "script.rpy").write_text(template, encoding="utf-8")

            result = read_official_translation_templates(root, "schinese")

        self.assertEqual(result.template_files, ("script.rpy",))
        self.assertEqual(result.warnings, ())
        self.assertEqual(
            [entry.kind for entry in result.entries],
            [SegmentKind.DIALOGUE, SegmentKind.NARRATION, SegmentKind.MENU_CHOICE],
        )
        self.assertEqual(
            [entry.source_text for entry in result.entries],
            ["こんにちは", "暗闇だった。", "返事をする"],
        )
        self.assertEqual(
            [entry.translation_identifier for entry in result.entries],
            ["start_first", "start_second", None],
        )
        self.assertEqual(
            [entry.source_code for entry in result.entries],
            ['aoi "こんにちは"', '"暗闇だった。"', '"返事をする"'],
        )
        self.assertEqual(
            [
                entry.source_code[entry.literal_start : entry.literal_end]
                for entry in result.entries
            ],
            ['"こんにちは"', '"暗闇だった。"', '"返事をする"'],
        )

    def test_warns_and_skips_multi_statement_dialogue_block(self) -> None:
        template = '''\
# game/script.rpy:8
translate schinese start_complex:

    # aoi "一行目"
    # extend "二行目"
    aoi "一行目"
    extend "二行目"
'''
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "script.rpy").write_text(template, encoding="utf-8")

            result = read_official_translation_templates(root, "schinese")

        self.assertEqual(result.entries, ())
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("包含 2 条原语句", result.warnings[0])


if __name__ == "__main__":
    unittest.main()
