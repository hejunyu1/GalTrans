from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from galtrans.adapters.renpy.extractor import find_renpy_string_literals
from galtrans.ir import SegmentKind


_TOP_SOURCE_RE = re.compile(r"^# (?P<file>.+):(?P<line>\d+)\s*$")
_INDENTED_SOURCE_RE = re.compile(r"^\s+# (?P<file>.+):(?P<line>\d+)\s*$")
_ORIGINAL_CODE_RE = re.compile(r"^\s+# (?P<code>.+)$")
_LANGUAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class RenpyTemplateError(ValueError):
    """Raised when official translation templates cannot be read safely."""


def is_valid_renpy_language(language: str) -> bool:
    return _LANGUAGE_RE.fullmatch(language) is not None


@dataclass(frozen=True, slots=True)
class OfficialTemplateEntry:
    template_file: str
    source_file: str
    line_number: int
    kind: SegmentKind
    source_text: str
    translation_identifier: str | None
    source_code: str
    literal_start: int
    literal_end: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OfficialTemplate:
    template_files: tuple[str, ...]
    entries: tuple[OfficialTemplateEntry, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_files": list(self.template_files),
            "entries": [entry.to_dict() for entry in self.entries],
            "warnings": list(self.warnings),
        }


def _block_end(lines: list[str], start: int) -> int:
    index = start
    while index < len(lines):
        line = lines[index]
        if line and not line[0].isspace():
            break
        index += 1
    return index


def _source_reference(match: re.Match[str]) -> tuple[str, int]:
    return match.group("file").replace("\\", "/"), int(match.group("line"))


def _parse_dialogue_block(
    *,
    lines: list[str],
    start: int,
    end: int,
    template_file: str,
    source_reference: tuple[str, int] | None,
    identifier: str,
) -> tuple[OfficialTemplateEntry | None, str | None]:
    location = f"{template_file}:{start + 1}"
    if source_reference is None:
        return None, f"{location} 的翻译块缺少顶层来源注释，已跳过"

    original_codes = [
        match.group("code")
        for line in lines[start + 1 : end]
        if (match := _ORIGINAL_CODE_RE.match(line)) is not None
    ]
    if len(original_codes) != 1:
        return None, f"{location} 的翻译块包含 {len(original_codes)} 条原语句，暂不支持逐条映射"

    code = original_codes[0]
    literals = find_renpy_string_literals(code)
    if len(literals) != 1:
        return None, f"{location} 的原语句包含 {len(literals)} 个字符串，暂不支持逐条映射"

    literal = literals[0]
    prefix = code[: literal.start].strip()
    kind = SegmentKind.NARRATION if not prefix else SegmentKind.DIALOGUE
    source_file, line_number = source_reference
    return (
        OfficialTemplateEntry(
            template_file=template_file,
            source_file=source_file,
            line_number=line_number,
            kind=kind,
            source_text=literal.body,
            translation_identifier=identifier,
            source_code=code,
            literal_start=literal.start,
            literal_end=literal.end,
        ),
        None,
    )


def _parse_string_block(
    *,
    lines: list[str],
    start: int,
    end: int,
    template_file: str,
) -> tuple[list[OfficialTemplateEntry], list[str]]:
    entries: list[OfficialTemplateEntry] = []
    warnings: list[str] = []
    source_reference: tuple[str, int] | None = None

    for index in range(start + 1, end):
        line = lines[index]
        source_match = _INDENTED_SOURCE_RE.match(line)
        if source_match is not None:
            source_reference = _source_reference(source_match)
            continue

        stripped = line.strip()
        if not stripped.startswith("old "):
            continue

        location = f"{template_file}:{index + 1}"
        if source_reference is None:
            warnings.append(f"{location} 的 old 字符串缺少来源注释，已跳过")
            continue

        literal_code = stripped.removeprefix("old ")
        literals = find_renpy_string_literals(literal_code)
        if len(literals) != 1 or literals[0].start != 0 or literals[0].end != len(literal_code):
            warnings.append(f"{location} 的 old 字符串格式不受支持，已跳过")
            source_reference = None
            continue

        source_file, line_number = source_reference
        entries.append(
            OfficialTemplateEntry(
                template_file=template_file,
                source_file=source_file,
                line_number=line_number,
                kind=SegmentKind.MENU_CHOICE,
                source_text=literals[0].body,
                translation_identifier=None,
                source_code=literal_code,
                literal_start=literals[0].start,
                literal_end=literals[0].end,
            )
        )
        source_reference = None

    return entries, warnings


def read_official_translation_templates(
    translation_root: Path,
    language: str,
) -> OfficialTemplate:
    """Read the conservative subset of templates emitted by Ren'Py's translate command."""
    resolved_root = translation_root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise RenpyTemplateError(f"Ren'Py 翻译模板目录不存在：{resolved_root}")

    template_paths = tuple(
        path
        for path in sorted(resolved_root.rglob("*.rpy"))
        if path.relative_to(resolved_root).as_posix() != "common.rpy"
    )
    if not template_paths:
        raise RenpyTemplateError(f"Ren'Py 未生成项目翻译模板：{resolved_root}")

    dialogue_header_re = re.compile(
        rf"^translate\s+{re.escape(language)}\s+(?P<identifier>\S+)\s*:\s*$"
    )
    strings_header_re = re.compile(
        rf"^translate\s+{re.escape(language)}\s+strings\s*:\s*$"
    )
    entries: list[OfficialTemplateEntry] = []
    warnings: list[str] = []

    for path in template_paths:
        template_file = path.relative_to(resolved_root).as_posix()
        lines = path.read_text(encoding="utf-8-sig", errors="strict").splitlines()
        source_reference: tuple[str, int] | None = None
        index = 0
        while index < len(lines):
            line = lines[index]
            source_match = _TOP_SOURCE_RE.match(line)
            if source_match is not None:
                source_reference = _source_reference(source_match)
                index += 1
                continue

            dialogue_match = dialogue_header_re.match(line)
            if dialogue_match is not None and dialogue_match.group("identifier") != "strings":
                end = _block_end(lines, index + 1)
                entry, warning = _parse_dialogue_block(
                    lines=lines,
                    start=index,
                    end=end,
                    template_file=template_file,
                    source_reference=source_reference,
                    identifier=dialogue_match.group("identifier"),
                )
                if entry is not None:
                    entries.append(entry)
                if warning is not None:
                    warnings.append(warning)
                source_reference = None
                index = end
                continue

            if strings_header_re.match(line) is not None:
                end = _block_end(lines, index + 1)
                string_entries, string_warnings = _parse_string_block(
                    lines=lines,
                    start=index,
                    end=end,
                    template_file=template_file,
                )
                entries.extend(string_entries)
                warnings.extend(string_warnings)
                source_reference = None
                index = end
                continue

            index += 1

    return OfficialTemplate(
        template_files=tuple(
            path.relative_to(resolved_root).as_posix() for path in template_paths
        ),
        entries=tuple(entries),
        warnings=tuple(warnings),
    )
