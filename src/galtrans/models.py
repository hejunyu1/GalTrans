from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class SourceKind(StrEnum):
    SCRIPT = "script"
    TEXT = "text"
    STRUCTURED_TEXT = "structured_text"


@dataclass(frozen=True, slots=True)
class SourceFile:
    relative_path: str
    kind: SourceKind
    engine_hint: str | None
    size_bytes: int
    sha256: str
    encoding: str
    has_bom: bool
    line_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScanWarning:
    relative_path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProjectScan:
    root: Path
    files: tuple[SourceFile, ...]
    warnings: tuple[ScanWarning, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "file_count": len(self.files),
            "warning_count": len(self.warnings),
            "files": [item.to_dict() for item in self.files],
            "warnings": [item.to_dict() for item in self.warnings],
        }

