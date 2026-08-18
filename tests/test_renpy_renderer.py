from __future__ import annotations

import unittest

from galtrans.adapters.renpy import (
    RenpyRenderError,
    RenpyTemplateMapping,
    render_official_translation_fragment,
)
from galtrans.adapters.renpy.extractor import find_renpy_string_literals
from galtrans.ir import SegmentKind


def _mapping(
    *,
    kind: SegmentKind,
    source_code: str,
    source_text: str,
    protected_tokens: tuple[str, ...] = (),
    identifier: str | None = "start_example",
) -> RenpyTemplateMapping:
    literal = find_renpy_string_literals(source_code)[0]
    return RenpyTemplateMapping(
        segment_id="seg_example",
        source_file="game/script.rpy",
        line_number=8,
        kind=kind,
        source_text=source_text,
        template_file="script.rpy",
        translation_identifier=identifier,
        source_code=source_code,
        literal_start=literal.start,
        literal_end=literal.end,
        protected_tokens=protected_tokens,
    )


class RenpyRendererTests(unittest.TestCase):
    def test_renders_dialogue_block_while_preserving_statement_structure(self) -> None:
        mapping = _mapping(
            kind=SegmentKind.DIALOGUE,
            source_code='aoi happy "{color=#7f7}よかった。{/color}" nointeract',
            source_text="{color=#7f7}よかった。{/color}",
            protected_tokens=("{color=#7f7}", "{/color}"),
        )

        result = render_official_translation_fragment(
            mapping,
            "{color=#7f7}太好了。{/color}",
            language="schinese",
        )

        self.assertFalse(result.requires_strings_header)
        self.assertEqual(
            result.content,
            '# game/script.rpy:8\n'
            "translate schinese start_example:\n\n"
            '    # aoi happy "{color=#7f7}よかった。{/color}" nointeract\n'
            '    aoi happy "{color=#7f7}太好了。{/color}" nointeract\n\n',
        )

    def test_renders_menu_entry_for_later_strings_container(self) -> None:
        mapping = _mapping(
            kind=SegmentKind.MENU_CHOICE,
            source_code='"返事をする"',
            source_text="返事をする",
            identifier=None,
        )

        result = render_official_translation_fragment(
            mapping,
            "回答",
            language="schinese",
        )

        self.assertTrue(result.requires_strings_header)
        self.assertEqual(
            result.content,
            '    # game/script.rpy:8\n'
            '    old "返事をする"\n'
            '    new "回答"\n\n',
        )

    def test_preserves_explicit_escape_tokens_without_double_escaping(self) -> None:
        mapping = _mapping(
            kind=SegmentKind.NARRATION,
            source_code='"一行目\\n二行目"',
            source_text="一行目\\n二行目",
            protected_tokens=("\\n",),
        )

        result = render_official_translation_fragment(
            mapping,
            "第一行\\n第二行",
            language="schinese",
        )

        self.assertIn('    "第一行\\n第二行"\n', result.content)
        self.assertNotIn("第一行\\\\n第二行", result.content)

    def test_rejects_missing_protected_token(self) -> None:
        mapping = _mapping(
            kind=SegmentKind.DIALOGUE,
            source_code='aoi "こんにちは、[player_name]"',
            source_text="こんにちは、[player_name]",
            protected_tokens=("[player_name]",),
        )

        with self.assertRaisesRegex(RenpyRenderError, "受保护标记"):
            render_official_translation_fragment(
                mapping,
                "你好",
                language="schinese",
            )

    def test_rejects_inconsistent_protected_token_record(self) -> None:
        mapping = _mapping(
            kind=SegmentKind.DIALOGUE,
            source_code='aoi "こんにちは、[player_name]"',
            source_text="こんにちは、[player_name]",
            protected_tokens=(),
        )

        with self.assertRaisesRegex(RenpyRenderError, "记录与官方原文不一致"):
            render_official_translation_fragment(
                mapping,
                "你好，[player_name]",
                language="schinese",
            )

    def test_rejects_raw_newline_and_unescaped_quote(self) -> None:
        mapping = _mapping(
            kind=SegmentKind.NARRATION,
            source_code='"原文"',
            source_text="原文",
        )

        with self.assertRaisesRegex(RenpyRenderError, "原始换行"):
            render_official_translation_fragment(
                mapping,
                "第一行\n第二行",
                language="schinese",
            )
        with self.assertRaisesRegex(RenpyRenderError, "显式转义引号"):
            render_official_translation_fragment(
                mapping,
                '他说"你好"',
                language="schinese",
            )


if __name__ == "__main__":
    unittest.main()
