from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from galtrans.adapters.renpy.extractor import (
    RenpyStringLiteral,
    find_renpy_protected_tokens,
    find_renpy_string_literals,
)
from galtrans.adapters.renpy.sdk import RenpyTemplateMapping
from galtrans.adapters.renpy.template import is_valid_renpy_language
from galtrans.ir import SegmentKind


class RenpyRenderError(ValueError):
    """Raised when a translation cannot be rendered without guessing."""


@dataclass(frozen=True, slots=True)
class RenderedRenpyFragment:
    template_file: str
    segment_id: str
    kind: SegmentKind
    translation_identifier: str | None
    requires_strings_header: bool
    content: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _source_literal(mapping: RenpyTemplateMapping) -> RenpyStringLiteral:
    literals = find_renpy_string_literals(mapping.source_code)
    if len(literals) != 1:
        raise RenpyRenderError(
            f"{mapping.segment_id} 的官方原语句不再包含唯一字符串，拒绝生成翻译片段"
        )

    literal = literals[0]
    if (
        literal.start != mapping.literal_start
        or literal.end != mapping.literal_end
        or literal.body != mapping.source_text
    ):
        raise RenpyRenderError(
            f"{mapping.segment_id} 的官方原语句与交叉验证记录不一致，拒绝生成翻译片段"
        )
    return literal


def _translated_literal(
    mapping: RenpyTemplateMapping,
    *,
    target_text: str,
    quote: str,
) -> str:
    if "\x00" in target_text or "\r" in target_text or "\n" in target_text:
        raise RenpyRenderError(f"{mapping.segment_id} 的译文包含原始换行或 NUL 字符")

    expected_tokens = tuple(
        token.value for token in find_renpy_protected_tokens(mapping.source_text)
    )
    if expected_tokens != mapping.protected_tokens:
        raise RenpyRenderError(
            f"{mapping.segment_id} 的受保护标记记录与官方原文不一致"
        )

    target_tokens = tuple(
        token.value for token in find_renpy_protected_tokens(target_text)
    )
    if target_tokens != expected_tokens:
        raise RenpyRenderError(
            f"{mapping.segment_id} 的译文没有按原顺序保留全部受保护标记"
        )

    candidate = f"{quote}{target_text}{quote}"
    literals = find_renpy_string_literals(candidate)
    if (
        len(literals) != 1
        or literals[0].start != 0
        or literals[0].end != len(candidate)
        or literals[0].body != target_text
    ):
        raise RenpyRenderError(
            f"{mapping.segment_id} 的译文不能安全放入 {quote} 引号字符串；请显式转义引号"
        )
    return candidate


def render_official_translation_fragment(
    mapping: RenpyTemplateMapping,
    target_text: str,
    *,
    language: str,
) -> RenderedRenpyFragment:
    """Render one checked mapping without writing an output file.

    Dialogue and narration produce a complete translate block. Menu choices produce an
    indented old/new entry that a later file writer must place below one strings header.
    """
    if not is_valid_renpy_language(language):
        raise RenpyRenderError(
            "语言名只能包含英文字母、数字、下划线和连字符，且必须以字母开头"
        )

    literal = _source_literal(mapping)
    quote = mapping.source_code[literal.start]
    target_literal = _translated_literal(
        mapping,
        target_text=target_text,
        quote=quote,
    )
    translated_code = (
        mapping.source_code[: literal.start]
        + target_literal
        + mapping.source_code[literal.end :]
    )

    if mapping.kind in {SegmentKind.DIALOGUE, SegmentKind.NARRATION}:
        if mapping.translation_identifier is None:
            raise RenpyRenderError(f"{mapping.segment_id} 缺少官方翻译 ID")
        content = (
            f"# {mapping.source_file}:{mapping.line_number}\n"
            f"translate {language} {mapping.translation_identifier}:\n\n"
            f"    # {mapping.source_code}\n"
            f"    {translated_code}\n\n"
        )
        requires_strings_header = False
    elif mapping.kind is SegmentKind.MENU_CHOICE:
        if literal.start != 0 or literal.end != len(mapping.source_code):
            raise RenpyRenderError(
                f"{mapping.segment_id} 的官方 old 字符串结构不受支持"
            )
        content = (
            f"    # {mapping.source_file}:{mapping.line_number}\n"
            f"    old {mapping.source_code}\n"
            f"    new {target_literal}\n\n"
        )
        requires_strings_header = True
    else:
        raise RenpyRenderError(f"{mapping.segment_id} 的文本类型不受支持：{mapping.kind}")

    return RenderedRenpyFragment(
        template_file=mapping.template_file,
        segment_id=mapping.segment_id,
        kind=mapping.kind,
        translation_identifier=mapping.translation_identifier,
        requires_strings_header=requires_strings_header,
        content=content,
    )
