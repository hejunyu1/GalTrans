from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from galtrans.adapters.renpy import (
    RenderedRenpyFragment,
    RenpyExportError,
    assemble_official_translation_files,
    write_official_translation_directory,
)
from galtrans.ir import SegmentKind


def _dialogue_fragment(
    *,
    segment_id: str = "seg_dialogue",
    template_file: str = "script.rpy",
    identifier: str = "start_example",
    language: str = "schinese",
) -> RenderedRenpyFragment:
    return RenderedRenpyFragment(
        template_file=template_file,
        segment_id=segment_id,
        language=language,
        kind=SegmentKind.DIALOGUE,
        translation_identifier=identifier,
        requires_strings_header=False,
        content=(
            "# game/script.rpy:2\n"
            f"translate {language} {identifier}:\n\n"
            '    # aoi "Original"\n'
            '    aoi "译文"\n\n'
        ),
    )


def _menu_fragment(
    *,
    segment_id: str = "seg_menu",
    template_file: str = "script.rpy",
    language: str = "schinese",
) -> RenderedRenpyFragment:
    return RenderedRenpyFragment(
        template_file=template_file,
        segment_id=segment_id,
        language=language,
        kind=SegmentKind.MENU_CHOICE,
        translation_identifier=None,
        requires_strings_header=True,
        content=(
            "    # game/script.rpy:4\n"
            '    old "Yes"\n'
            '    new "是"\n\n'
        ),
    )


class RenpyExporterTests(unittest.TestCase):
    def test_groups_fragments_with_one_strings_header_per_template(self) -> None:
        menu = _menu_fragment()
        dialogue = _dialogue_fragment()

        result = assemble_official_translation_files(
            [menu, dialogue],
            language="schinese",
        )

        self.assertEqual(len(result), 1)
        rendered_file = result[0]
        self.assertEqual(rendered_file.relative_path, "game/tl/schinese/script.rpy")
        self.assertEqual(rendered_file.segment_ids, ("seg_dialogue", "seg_menu"))
        self.assertEqual(rendered_file.content.count("translate schinese strings:"), 1)
        self.assertLess(
            rendered_file.content.index("translate schinese start_example:"),
            rendered_file.content.index("translate schinese strings:"),
        )

    def test_preserves_nested_template_paths_and_separates_files(self) -> None:
        result = assemble_official_translation_files(
            [
                _dialogue_fragment(),
                _dialogue_fragment(
                    segment_id="seg_nested",
                    template_file="chapter/one.rpy",
                    identifier="chapter_one",
                ),
            ],
            language="schinese",
        )

        self.assertEqual(
            [file.relative_path for file in result],
            [
                "game/tl/schinese/script.rpy",
                "game/tl/schinese/chapter/one.rpy",
            ],
        )

    def test_rejects_unsafe_paths_duplicates_and_language_mismatch(self) -> None:
        with self.assertRaisesRegex(RenpyExportError, "安全的相对路径"):
            assemble_official_translation_files(
                [_dialogue_fragment(template_file="../escape.rpy")],
                language="schinese",
            )

        duplicate = _dialogue_fragment()
        with self.assertRaisesRegex(RenpyExportError, "重复的文本段 ID"):
            assemble_official_translation_files(
                [duplicate, duplicate],
                language="schinese",
            )

        with self.assertRaisesRegex(RenpyExportError, "片段语言"):
            assemble_official_translation_files(
                [_dialogue_fragment(language="japanese")],
                language="schinese",
            )

    def test_writes_new_patch_directory_with_utf8_bom_and_lf(self) -> None:
        files = assemble_official_translation_files(
            [_dialogue_fragment(), _menu_fragment()],
            language="schinese",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project_root = root / "input"
            project_root.mkdir()
            source = project_root / "script.rpy"
            source.write_bytes(b'label start:\n    "Original"\n')
            original_source = source.read_bytes()
            output_root = root / "output"

            result = write_official_translation_directory(
                files,
                output_root,
                input_project_root=project_root,
            )

            output_file = output_root / "game" / "tl" / "schinese" / "script.rpy"
            output_bytes = output_file.read_bytes()
            staging_paths = tuple(root.glob(".galtrans-renpy-*"))
            final_source = source.read_bytes()

        self.assertEqual(result.root, output_root)
        self.assertEqual(result.files, (output_file,))
        self.assertTrue(output_bytes.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r\n", output_bytes)
        self.assertIn("translate schinese strings:", output_bytes.decode("utf-8-sig"))
        self.assertEqual(final_source, original_source)
        self.assertEqual(staging_paths, ())

    def test_refuses_existing_or_input_overlapping_output_directory(self) -> None:
        files = assemble_official_translation_files(
            [_dialogue_fragment()],
            language="schinese",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project_root = root / "input"
            project_root.mkdir()
            existing_output = root / "existing"
            existing_output.mkdir()
            sentinel = existing_output / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(FileExistsError, "拒绝覆盖"):
                write_official_translation_directory(
                    files,
                    existing_output,
                    input_project_root=project_root,
                )
            with self.assertRaisesRegex(RenpyExportError, "不得与输入项目重叠"):
                write_official_translation_directory(
                    files,
                    project_root / "output",
                    input_project_root=project_root,
                )

            sentinel_contents = sentinel.read_text(encoding="utf-8")
            overlapping_output_exists = (project_root / "output").exists()

        self.assertEqual(sentinel_contents, "keep")
        self.assertFalse(overlapping_output_exists)

    def test_writer_revalidates_assembled_output_path(self) -> None:
        files = assemble_official_translation_files(
            [_dialogue_fragment()],
            language="schinese",
        )
        tampered_file = replace(
            files[0],
            relative_path="game/tl/schinese/other.rpy",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project_root = root / "input"
            project_root.mkdir()

            with self.assertRaisesRegex(RenpyExportError, "不在 Ren'Py 翻译目录"):
                write_official_translation_directory(
                    [tampered_file],
                    root / "output",
                    input_project_root=project_root,
                )

            output_exists = (root / "output").exists()

        self.assertFalse(output_exists)


if __name__ == "__main__":
    unittest.main()
