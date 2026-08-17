from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from galtrans.encoding import detect_and_decode
from galtrans.models import ProjectScan, ScanWarning, SourceFile, SourceKind


_SUPPORTED_SUFFIXES: dict[str, tuple[SourceKind, str | None]] = {
    ".rpy": (SourceKind.SCRIPT, "renpy"),
    ".rpym": (SourceKind.SCRIPT, "renpy"),
    ".ks": (SourceKind.SCRIPT, "kirikiri"),
    ".txt": (SourceKind.TEXT, None),
    ".json": (SourceKind.STRUCTURED_TEXT, None),
    ".csv": (SourceKind.STRUCTURED_TEXT, None),
    ".tsv": (SourceKind.STRUCTURED_TEXT, None),
}

_IGNORED_DIRECTORIES = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "galtrans-output",
}


def _is_ignored(path: Path, root: Path) -> bool:
    relative_parts = path.relative_to(root).parts
    return any(part in _IGNORED_DIRECTORIES for part in relative_parts)


def scan_project(root: Path) -> ProjectScan:
    resolved_root = root.expanduser().resolve()
    if not resolved_root.exists():
        raise FileNotFoundError(f"目录不存在：{resolved_root}")
    if not resolved_root.is_dir():
        raise NotADirectoryError(f"不是目录：{resolved_root}")

    files: list[SourceFile] = []
    warnings: list[ScanWarning] = []

    for path in sorted(resolved_root.rglob("*")):
        if not path.is_file() or _is_ignored(path, resolved_root):
            continue

        suffix = path.suffix.lower()
        descriptor = _SUPPORTED_SUFFIXES.get(suffix)
        if descriptor is None:
            continue

        relative_path = path.relative_to(resolved_root).as_posix()
        try:
            data = path.read_bytes()
        except OSError as error:
            warnings.append(ScanWarning(relative_path, f"无法读取：{error}"))
            continue

        decoded = detect_and_decode(data)
        if decoded is None:
            warnings.append(ScanWarning(relative_path, "无法安全识别文本编码，已跳过"))
            continue

        kind, engine_hint = descriptor
        files.append(
            SourceFile(
                relative_path=relative_path,
                kind=kind,
                engine_hint=engine_hint,
                size_bytes=len(data),
                sha256=sha256(data).hexdigest(),
                encoding=decoded.encoding,
                has_bom=decoded.has_bom,
                line_count=len(decoded.text.splitlines()),
            )
        )

    return ProjectScan(root=resolved_root, files=tuple(files), warnings=tuple(warnings))

