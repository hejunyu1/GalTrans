from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class SegmentKind(StrEnum):
    DIALOGUE = "dialogue"
    NARRATION = "narration"
    MENU_CHOICE = "menu_choice"


class ProtectedTokenKind(StrEnum):
    INTERPOLATION = "interpolation"
    TEXT_TAG = "text_tag"
    ESCAPE = "escape"


@dataclass(frozen=True, slots=True)
class ProtectedToken:
    index: int
    kind: ProtectedTokenKind
    value: str
    start: int
    end: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TextSegment:
    schema_version: int
    id: str
    engine: str
    source_file: str
    source_encoding: str
    source_sha256: str
    line_number: int
    scene: str
    kind: SegmentKind
    speaker: str | None
    speaker_display: str | None
    source_text: str
    protected_tokens: tuple[ProtectedToken, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CharacterDefinition:
    identifier: str
    display_name: str
    line_number: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExtractionWarning:
    line_number: int
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    engine: str
    source_file: str
    source_encoding: str
    source_sha256: str
    characters: tuple[CharacterDefinition, ...]
    segments: tuple[TextSegment, ...]
    warnings: tuple[ExtractionWarning, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "source_file": self.source_file,
            "source_encoding": self.source_encoding,
            "source_sha256": self.source_sha256,
            "characters": [item.to_dict() for item in self.characters],
            "segments": [item.to_dict() for item in self.segments],
            "warnings": [item.to_dict() for item in self.warnings],
        }
