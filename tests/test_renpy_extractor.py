from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from galtrans.adapters.renpy import extract_renpy_file, extract_renpy_path
from galtrans.ir import ProtectedTokenKind, SegmentKind


class RenpyExtractorTests(unittest.TestCase):
    def test_extracts_dialogue_narration_and_menu_choices(self) -> None:
        source = '''\
define aoi = Character("葵")

label start:
    aoi "ねえ、[player_name]。{color=#f00}聞こえる？{/color}"
    "暗闇の中で声がした。"
    menu:
        "返事をする":
            aoi happy "よかった。"
        "黙っている" if player_name != "":
            aoi "……。"
    return
'''
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "script.rpy"
            path.write_text(source, encoding="utf-8")

            result = extract_renpy_file(path, source_name="game/script.rpy")

        self.assertEqual(result.source_file, "game/script.rpy")
        self.assertEqual(len(result.characters), 1)
        self.assertEqual(result.characters[0].identifier, "aoi")
        self.assertEqual(result.characters[0].display_name, "葵")
        self.assertEqual(
            [segment.kind for segment in result.segments],
            [
                SegmentKind.DIALOGUE,
                SegmentKind.NARRATION,
                SegmentKind.MENU_CHOICE,
                SegmentKind.DIALOGUE,
                SegmentKind.MENU_CHOICE,
                SegmentKind.DIALOGUE,
            ],
        )
        first = result.segments[0]
        self.assertEqual(first.schema_version, 1)
        self.assertEqual(first.source_encoding, "utf-8")
        self.assertEqual(first.source_sha256, result.source_sha256)
        self.assertEqual(first.speaker, "aoi")
        self.assertEqual(first.speaker_display, "葵")
        self.assertEqual(
            [token.value for token in first.protected_tokens],
            ["[player_name]", "{color=#f00}", "{/color}"],
        )
        self.assertEqual(
            [token.kind for token in first.protected_tokens],
            [
                ProtectedTokenKind.INTERPOLATION,
                ProtectedTokenKind.TEXT_TAG,
                ProtectedTokenKind.TEXT_TAG,
            ],
        )
        for token in first.protected_tokens:
            self.assertEqual(first.source_text[token.start : token.end], token.value)
        self.assertEqual(result.warnings, ())

    def test_skips_python_assignments_and_non_dialogue_commands(self) -> None:
        source = '''\
label start:
    $ debug_text = "not dialogue"
    python:
        another = "also not dialogue"
    voice "voice/test.ogg"
    scene expression "background.png"
    "real narration"
'''
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "script.rpy"
            path.write_text(source, encoding="utf-8")

            result = extract_renpy_file(path)

        self.assertEqual(len(result.segments), 1)
        self.assertEqual(result.segments[0].source_text, "real narration")

    def test_stable_id_does_not_depend_on_line_number(self) -> None:
        first_source = 'label start:\n    aoi "同じ台詞"\n'
        shifted_source = 'label start:\n\n\n    aoi "同じ台詞"\n'
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first_path = root / "first.rpy"
            shifted_path = root / "shifted.rpy"
            first_path.write_text(first_source, encoding="utf-8")
            shifted_path.write_text(shifted_source, encoding="utf-8")

            first = extract_renpy_file(first_path, source_name="game/script.rpy")
            shifted = extract_renpy_file(shifted_path, source_name="game/script.rpy")

        self.assertNotEqual(first.segments[0].line_number, shifted.segments[0].line_number)
        self.assertEqual(first.segments[0].id, shifted.segments[0].id)

    def test_duplicate_lines_receive_distinct_repeatable_ids(self) -> None:
        source = 'label start:\n    aoi "はい"\n    aoi "はい"\n'
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "script.rpy"
            path.write_text(source, encoding="utf-8")

            first = extract_renpy_file(path)
            second = extract_renpy_file(path)

        self.assertNotEqual(first.segments[0].id, first.segments[1].id)
        self.assertEqual(
            [segment.id for segment in first.segments],
            [segment.id for segment in second.segments],
        )

    def test_extracts_cp932_source(self) -> None:
        source = 'label start:\r\n    "彼女は笑った。"\r\n'
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "script.rpy"
            path.write_bytes(source.encode("cp932"))

            result = extract_renpy_file(path)

        self.assertEqual(result.source_encoding, "cp932")
        self.assertEqual(result.segments[0].source_text, "彼女は笑った。")

    def test_warns_and_skips_triple_quoted_text(self) -> None:
        source = 'label start:\n    aoi """long text"""\n'
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "script.rpy"
            path.write_text(source, encoding="utf-8")

            result = extract_renpy_file(path)

        self.assertEqual(result.segments, ())
        self.assertEqual(len(result.warnings), 1)

    def test_extracts_project_using_relative_source_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            game = root / "game"
            chapter = game / "chapter"
            chapter.mkdir(parents=True)
            (game / "script.rpy").write_text(
                'label start:\n    "開始"\n', encoding="utf-8"
            )
            (chapter / "one.rpy").write_text(
                'label chapter_one:\n    "第一章"\n', encoding="utf-8"
            )
            ignored = root / ".venv"
            ignored.mkdir()
            (ignored / "ignored.rpy").write_text(
                'label ignored:\n    "ignore"\n', encoding="utf-8"
            )

            results = extract_renpy_path(root)

        self.assertEqual(
            [result.source_file for result in results],
            ["game/chapter/one.rpy", "game/script.rpy"],
        )
        self.assertEqual(
            [result.segments[0].source_text for result in results],
            ["第一章", "開始"],
        )


if __name__ == "__main__":
    unittest.main()
