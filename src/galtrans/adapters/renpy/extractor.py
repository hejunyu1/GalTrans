from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from galtrans.encoding import detect_and_decode
from galtrans.ir import (
    CharacterDefinition,
    ExtractionResult,
    ExtractionWarning,
    ProtectedToken,
    ProtectedTokenKind,
    SegmentKind,
    TextSegment,
)


_LABEL_RE = re.compile(
    r"^\s*label\s+(?P<name>\.?[^\W\d]\w*(?:\.[^\W\d]\w*)*)"
    r"(?:\s*\([^)]*\))?\s*:\s*(?:#.*)?$"
)
_CHARACTER_RE = re.compile(
    r"^\s*(?:define\s+)?(?P<identifier>[^\W\d]\w*)\s*=\s*Character\s*\("
)
_PYTHON_BLOCK_RE = re.compile(r"^(?:init(?:\s+-?\d+)?\s+)?python(?:\s+early)?\s*:")
_MENU_RE = re.compile(r"^menu(?:\s+[^:]+)?\s*:\s*(?:#.*)?$")
_SAY_PREFIX_RE = re.compile(
    r"^[^\W\d]\w*(?:\s+(?:[^\W\d]\w*|@[^\W\d]\w*|-[^\W\d]\w*))*$"
)

_NON_SPEAKER_COMMANDS = {
    "call",
    "camera",
    "default",
    "define",
    "elif",
    "else",
    "for",
    "hide",
    "if",
    "image",
    "init",
    "jump",
    "menu",
    "pause",
    "play",
    "python",
    "queue",
    "return",
    "scene",
    "show",
    "stop",
    "style",
    "transform",
    "voice",
    "while",
    "window",
    "with",
}

_IGNORED_DIRECTORIES = {".git", ".venv", "__pycache__", "galtrans-output", "node_modules"}


@dataclass(frozen=True, slots=True)
class _StringLiteral:
    start: int
    end: int
    body: str


def _indentation(line: str) -> int:
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip(" "))


def _find_string_literals(line: str) -> tuple[_StringLiteral, ...]:
    literals: list[_StringLiteral] = []
    index = 0
    while index < len(line):
        character = line[index]
        if character == "#":
            break
        if character not in {'"', "'"}:
            index += 1
            continue

        quote = character
        start = index
        index += 1
        body_start = index
        while index < len(line):
            if line[index] == "\\":
                index += 2
                continue
            if index < len(line) and line[index] == quote:
                literals.append(_StringLiteral(start=start, end=index + 1, body=line[body_start:index]))
                index += 1
                break
            index += 1
        else:
            break
    return tuple(literals)


def _find_protected_tokens(text: str) -> tuple[ProtectedToken, ...]:
    tokens: list[ProtectedToken] = []
    index = 0
    while index < len(text):
        start = index
        character = text[index]

        if character == "\\" and index + 1 < len(text):
            end = index + 2
            kind = ProtectedTokenKind.ESCAPE
        elif text.startswith("[[", index) or text.startswith("{{", index):
            end = index + 2
            kind = ProtectedTokenKind.ESCAPE
        elif character == "[":
            closing = text.find("]", index + 1)
            if closing == -1:
                index += 1
                continue
            end = closing + 1
            kind = ProtectedTokenKind.INTERPOLATION
        elif character == "{":
            closing = text.find("}", index + 1)
            if closing == -1:
                index += 1
                continue
            end = closing + 1
            kind = ProtectedTokenKind.TEXT_TAG
        else:
            index += 1
            continue

        tokens.append(
            ProtectedToken(
                index=len(tokens),
                kind=kind,
                value=text[start:end],
                start=start,
                end=end,
            )
        )
        index = end

    return tuple(tokens)


def _stable_segment_id(
    *,
    source_file: str,
    scene: str,
    kind: SegmentKind,
    speaker: str | None,
    source_text: str,
    occurrence: int,
) -> str:
    identity = "\0".join(
        (
            source_file.replace("\\", "/"),
            scene,
            kind.value,
            speaker or "",
            source_text,
            str(occurrence),
        )
    )
    return f"seg_{sha256(identity.encode('utf-8')).hexdigest()[:24]}"


def _character_definitions(lines: list[str]) -> tuple[CharacterDefinition, ...]:
    definitions: list[CharacterDefinition] = []
    for line_number, line in enumerate(lines, start=1):
        match = _CHARACTER_RE.match(line)
        if match is None:
            continue
        literals = _find_string_literals(line[match.end() :])
        if not literals:
            continue
        definitions.append(
            CharacterDefinition(
                identifier=match.group("identifier"),
                display_name=literals[0].body,
                line_number=line_number,
            )
        )
    return tuple(definitions)


def extract_renpy_file(path: Path, *, source_name: str | None = None) -> ExtractionResult:
    resolved_path = path.expanduser().resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"文件不存在：{resolved_path}")
    if not resolved_path.is_file():
        raise IsADirectoryError(f"不是文件：{resolved_path}")
    if resolved_path.suffix.lower() not in {".rpy", ".rpym"}:
        raise ValueError(f"不是 Ren'Py 源脚本：{resolved_path}")

    data = resolved_path.read_bytes()
    decoded = detect_and_decode(data)
    if decoded is None:
        raise UnicodeError(f"无法安全识别文本编码：{resolved_path}")

    normalized_source_name = (source_name or resolved_path.name).replace("\\", "/")
    source_digest = sha256(data).hexdigest()
    lines = decoded.text.splitlines()
    characters = _character_definitions(lines)
    character_names = {item.identifier: item.display_name for item in characters}
    warnings: list[ExtractionWarning] = []
    segments: list[TextSegment] = []
    occurrences: defaultdict[tuple[str, SegmentKind, str | None, str], int] = defaultdict(int)

    current_label: str | None = None
    label_indent = -1
    python_block_indent: int | None = None
    menu_indents: list[int] = []
    multiline_delimiter: str | None = None

    def add_segment(
        *,
        line_number: int,
        kind: SegmentKind,
        source_text: str,
        speaker: str | None,
    ) -> None:
        assert current_label is not None
        duplicate_key = (current_label, kind, speaker, source_text)
        occurrence = occurrences[duplicate_key]
        occurrences[duplicate_key] += 1
        segments.append(
            TextSegment(
                schema_version=1,
                id=_stable_segment_id(
                    source_file=normalized_source_name,
                    scene=current_label,
                    kind=kind,
                    speaker=speaker,
                    source_text=source_text,
                    occurrence=occurrence,
                ),
                engine="renpy",
                source_file=normalized_source_name,
                source_encoding=decoded.encoding,
                source_sha256=source_digest,
                line_number=line_number,
                scene=current_label,
                kind=kind,
                speaker=speaker,
                speaker_display=character_names.get(speaker) if speaker else None,
                source_text=source_text,
                protected_tokens=_find_protected_tokens(source_text),
            )
        )

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        indent = _indentation(line)

        if not stripped or stripped.startswith("#"):
            continue

        label_match = _LABEL_RE.match(line)
        if label_match is not None:
            current_label = label_match.group("name")
            label_indent = indent
            python_block_indent = None
            menu_indents.clear()
            multiline_delimiter = None
            continue

        if current_label is None:
            continue

        if indent <= label_indent:
            current_label = None
            python_block_indent = None
            menu_indents.clear()
            multiline_delimiter = None
            continue

        if multiline_delimiter is not None:
            if multiline_delimiter in stripped:
                multiline_delimiter = None
            continue

        triple_delimiters = [item for item in ('"""', "'''") if item in stripped]
        if triple_delimiters:
            delimiter = triple_delimiters[0]
            if stripped.count(delimiter) % 2 == 1:
                multiline_delimiter = delimiter
            warnings.append(
                ExtractionWarning(line_number, "暂不支持三引号多行字符串，已保守跳过")
            )
            continue

        if python_block_indent is not None:
            if indent > python_block_indent:
                continue
            python_block_indent = None

        while menu_indents and indent <= menu_indents[-1]:
            menu_indents.pop()

        if _PYTHON_BLOCK_RE.match(stripped):
            python_block_indent = indent
            continue
        if stripped.startswith("$"):
            continue
        if _MENU_RE.match(stripped):
            menu_indents.append(indent)
            continue

        literals = _find_string_literals(stripped)
        if not literals:
            continue

        literal = literals[0]
        suffix_without_comment = stripped[literal.end :].split("#", 1)[0].strip()
        if menu_indents and literal.start == 0 and re.fullmatch(
            r"(?:if\b.+)?\s*:", suffix_without_comment
        ):
            add_segment(
                line_number=line_number,
                kind=SegmentKind.MENU_CHOICE,
                source_text=literal.body,
                speaker=None,
            )
            continue

        if len(literals) > 1:
            first_word_match = re.match(r"[^\W\d]\w*", stripped)
            first_word = first_word_match.group(0) if first_word_match else ""
            if first_word and first_word not in _NON_SPEAKER_COMMANDS:
                warnings.append(
                    ExtractionWarning(line_number, "同一语句包含多个字符串，无法可靠判断台词主体")
                )
            continue

        if literal.start == 0:
            add_segment(
                line_number=line_number,
                kind=SegmentKind.NARRATION,
                source_text=literal.body,
                speaker=None,
            )
            continue

        prefix = stripped[: literal.start].strip()
        if not _SAY_PREFIX_RE.fullmatch(prefix):
            continue
        speaker = prefix.split()[0]
        if speaker in _NON_SPEAKER_COMMANDS:
            continue
        add_segment(
            line_number=line_number,
            kind=SegmentKind.DIALOGUE,
            source_text=literal.body,
            speaker=speaker,
        )

    return ExtractionResult(
        engine="renpy",
        source_file=normalized_source_name,
        source_encoding=decoded.encoding,
        source_sha256=source_digest,
        characters=characters,
        segments=tuple(segments),
        warnings=tuple(warnings),
    )


def extract_renpy_path(path: Path) -> tuple[ExtractionResult, ...]:
    """Extract one source file or all Ren'Py source files below a project directory."""
    resolved_path = path.expanduser().resolve()
    if not resolved_path.exists():
        raise FileNotFoundError(f"路径不存在：{resolved_path}")
    if resolved_path.is_file():
        return (extract_renpy_file(resolved_path),)

    source_paths = sorted(
        candidate
        for candidate in resolved_path.rglob("*")
        if candidate.is_file()
        and candidate.suffix.lower() in {".rpy", ".rpym"}
        and not any(part in _IGNORED_DIRECTORIES for part in candidate.relative_to(resolved_path).parts)
    )
    if not source_paths:
        raise ValueError(f"目录中没有找到 .rpy 或 .rpym 源脚本：{resolved_path}")

    return tuple(
        extract_renpy_file(
            source_path,
            source_name=source_path.relative_to(resolved_path).as_posix(),
        )
        for source_path in source_paths
    )
