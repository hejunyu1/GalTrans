from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

RENPY_COMPATIBILITY_REPORT_SCHEMA_VERSION = 1

_DEFAULT_MAX_DEPTH = 12
_DEFAULT_MAX_ENTRIES = 50_000
_MAX_VERSION_FILE_BYTES = 256 * 1024
_SCRIPT_SUFFIXES = {".rpy", ".rpym"}
_COMPILED_SUFFIXES = {".rpyc", ".rpymc"}
_TRANSLATION_SUFFIXES = _SCRIPT_SUFFIXES | _COMPILED_SUFFIXES
_VERSION_TUPLE_RE = re.compile(
    rb"\bversion_tuple\s*=\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)"
)
_IGNORED_LAUNCHERS = {
    "python.exe",
    "pythonw.exe",
    "renpy.exe",
    "renpy-console.exe",
}


class RenpyCompatibilityStatus(StrEnum):
    SOURCE_READY = "source_ready"
    PACKAGED_REQUIRES_IMPORT = "packaged_requires_import"
    UNCERTAIN = "uncertain"
    NOT_RENPY = "not_renpy"


class RenpyCompatibilityIssueCode(StrEnum):
    SYMLINK_SKIPPED = "symlink_skipped"
    DEPTH_LIMIT_REACHED = "depth_limit_reached"
    ENTRY_LIMIT_REACHED = "entry_limit_reached"
    DIRECTORY_UNREADABLE = "directory_unreadable"
    MIXED_SOURCE_AND_PACKAGED = "mixed_source_and_packaged"
    WEAK_ARCHIVE_EVIDENCE = "weak_archive_evidence"
    VERSION_HINT_UNREADABLE = "version_hint_unreadable"


@dataclass(frozen=True, slots=True)
class RenpyCompatibilityIssue:
    code: RenpyCompatibilityIssueCode
    relative_path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code.value,
            "relative_path": self.relative_path,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class RenpyVersionHint:
    version: str
    relative_path: str

    def to_dict(self) -> dict[str, str]:
        return {
            "version": self.version,
            "relative_path": self.relative_path,
        }


@dataclass(frozen=True, slots=True)
class RenpyCompatibilityReport:
    schema_version: int
    selected_root: Path
    project_root: Path
    game_directory: str | None
    status: RenpyCompatibilityStatus
    summary: str
    source_scripts: tuple[str, ...]
    compiled_scripts: tuple[str, ...]
    archives: tuple[str, ...]
    translation_files: tuple[str, ...]
    launchers: tuple[str, ...]
    runtime_markers: tuple[str, ...]
    version_hints: tuple[RenpyVersionHint, ...]
    issues: tuple[RenpyCompatibilityIssue, ...]

    @property
    def can_translate_now(self) -> bool:
        return self.status is RenpyCompatibilityStatus.SOURCE_READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "selected_root": str(self.selected_root),
            "project_root": str(self.project_root),
            "game_directory": self.game_directory,
            "status": self.status.value,
            "summary": self.summary,
            "can_translate_now": self.can_translate_now,
            "counts": {
                "source_scripts": len(self.source_scripts),
                "compiled_scripts": len(self.compiled_scripts),
                "archives": len(self.archives),
                "translation_files": len(self.translation_files),
                "launchers": len(self.launchers),
            },
            "source_scripts": list(self.source_scripts),
            "compiled_scripts": list(self.compiled_scripts),
            "archives": list(self.archives),
            "translation_files": list(self.translation_files),
            "launchers": list(self.launchers),
            "runtime_markers": list(self.runtime_markers),
            "version_hints": [hint.to_dict() for hint in self.version_hints],
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(slots=True)
class _ScanResult:
    source_scripts: list[str]
    compiled_scripts: list[str]
    archives: list[str]
    translation_files: list[str]
    issues: list[RenpyCompatibilityIssue]


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _sorted_paths(paths: list[str]) -> tuple[str, ...]:
    return tuple(sorted(paths, key=lambda path: (path.casefold(), path)))


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _find_game_directory(selected_root: Path) -> tuple[Path, Path] | None:
    if selected_root.name.casefold() == "game":
        return selected_root.parent, selected_root
    try:
        children = sorted(
            selected_root.iterdir(),
            key=lambda path: (path.name.casefold(), path.name),
        )
    except OSError:
        return None
    matches = [child for child in children if child.name.casefold() == "game" and child.is_dir()]
    if len(matches) != 1:
        return None
    return selected_root, matches[0]


def _scan_game_directory(
    game_directory: Path,
    project_root: Path,
    *,
    max_depth: int,
    max_entries: int,
) -> _ScanResult:
    result = _ScanResult([], [], [], [], [])
    pending: list[tuple[Path, int]] = [(game_directory, 0)]
    entry_count = 0

    while pending:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: (entry.name.casefold(), entry.name))
        except OSError as error:
            result.issues.append(
                RenpyCompatibilityIssue(
                    RenpyCompatibilityIssueCode.DIRECTORY_UNREADABLE,
                    _relative(directory, project_root),
                    f"无法读取目录：{error}",
                )
            )
            continue

        child_directories: list[Path] = []
        for entry in entries:
            entry_count += 1
            path = Path(entry.path)
            relative_path = _relative(path, project_root)
            if entry_count > max_entries:
                result.issues.append(
                    RenpyCompatibilityIssue(
                        RenpyCompatibilityIssueCode.ENTRY_LIMIT_REACHED,
                        _relative(game_directory, project_root),
                        f"目录项目超过安全上限 {max_entries}，扫描未完成",
                    )
                )
                return result
            try:
                if entry.is_symlink() or path.is_junction():
                    result.issues.append(
                        RenpyCompatibilityIssue(
                            RenpyCompatibilityIssueCode.SYMLINK_SKIPPED,
                            relative_path,
                            "为避免越过所选目录，已跳过链接或目录联接",
                        )
                    )
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if depth >= max_depth:
                        result.issues.append(
                            RenpyCompatibilityIssue(
                                RenpyCompatibilityIssueCode.DEPTH_LIMIT_REACHED,
                                relative_path,
                                f"目录深度超过安全上限 {max_depth}，扫描未完成",
                            )
                        )
                    else:
                        child_directories.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError as error:
                result.issues.append(
                    RenpyCompatibilityIssue(
                        RenpyCompatibilityIssueCode.DIRECTORY_UNREADABLE,
                        relative_path,
                        f"无法检查目录项目：{error}",
                    )
                )
                continue

            suffix = path.suffix.casefold()
            game_relative = path.relative_to(game_directory)
            is_translation = (
                bool(game_relative.parts)
                and game_relative.parts[0].casefold() == "tl"
                and suffix in _TRANSLATION_SUFFIXES
            )
            if is_translation:
                result.translation_files.append(relative_path)
            elif suffix in _SCRIPT_SUFFIXES:
                result.source_scripts.append(relative_path)
            elif suffix in _COMPILED_SUFFIXES:
                result.compiled_scripts.append(relative_path)
            elif suffix == ".rpa":
                result.archives.append(relative_path)

        for child in reversed(child_directories):
            pending.append((child, depth + 1))

    return result


def _find_launchers(project_root: Path) -> tuple[str, ...]:
    launchers: list[str] = []
    try:
        children = sorted(
            project_root.iterdir(),
            key=lambda path: (path.name.casefold(), path.name),
        )
    except OSError:
        return ()
    for path in children:
        name = path.name.casefold()
        if (
            path.suffix.casefold() == ".exe"
            and name not in _IGNORED_LAUNCHERS
            and not name.startswith("unins")
            and path.is_file()
            and not _is_link_or_junction(path)
        ):
            launchers.append(_relative(path, project_root))
    return tuple(launchers)


def _find_runtime_markers(project_root: Path) -> tuple[str, ...]:
    markers: list[str] = []
    for name in ("renpy", "lib"):
        path = project_root / name
        if path.is_dir() and not _is_link_or_junction(path):
            markers.append(name)
    return tuple(markers)


def _read_version_hint(
    project_root: Path,
) -> tuple[tuple[RenpyVersionHint, ...], tuple[RenpyCompatibilityIssue, ...]]:
    version_file = project_root / "renpy" / "__init__.py"
    if not version_file.is_file() or _is_link_or_junction(version_file):
        return (), ()
    relative_path = _relative(version_file, project_root)
    try:
        with version_file.open("rb") as stream:
            data = stream.read(_MAX_VERSION_FILE_BYTES + 1)
    except OSError as error:
        return (), (
            RenpyCompatibilityIssue(
                RenpyCompatibilityIssueCode.VERSION_HINT_UNREADABLE,
                relative_path,
                f"无法读取可选版本线索：{error}",
            ),
        )
    if len(data) > _MAX_VERSION_FILE_BYTES:
        return (), (
            RenpyCompatibilityIssue(
                RenpyCompatibilityIssueCode.VERSION_HINT_UNREADABLE,
                relative_path,
                "可选版本线索文件超过安全读取上限，已忽略",
            ),
        )
    match = _VERSION_TUPLE_RE.search(data)
    if match is None:
        return (), ()
    version = ".".join(part.decode("ascii") for part in match.groups())
    return (RenpyVersionHint(version, relative_path),), ()


def _summary_for(status: RenpyCompatibilityStatus) -> str:
    return {
        RenpyCompatibilityStatus.SOURCE_READY: (
            "发现可读的 Ren'Py 源脚本；当前 source-only 流程可以继续。"
        ),
        RenpyCompatibilityStatus.PACKAGED_REQUIRES_IMPORT: (
            "已识别为 Ren'Py 成品结构，但只有编译脚本或归档；当前版本不会解包或反编译。"
        ),
        RenpyCompatibilityStatus.UNCERTAIN: (
            "发现部分 Ren'Py 线索，但证据不足或扫描不完整；已安全停止。"
        ),
        RenpyCompatibilityStatus.NOT_RENPY: "没有发现足以确认 Ren'Py 项目的证据。",
    }[status]


def inspect_renpy_compatibility(
    root: Path,
    *,
    max_depth: int = _DEFAULT_MAX_DEPTH,
    max_entries: int = _DEFAULT_MAX_ENTRIES,
) -> RenpyCompatibilityReport:
    """Inspect a selected directory without reading archives or modifying any input file."""
    if max_depth < 0:
        raise ValueError("max_depth 不能小于 0")
    if max_entries < 1:
        raise ValueError("max_entries 必须大于 0")

    selected_root = root.expanduser()
    if not selected_root.exists():
        raise FileNotFoundError(f"目录不存在：{selected_root}")
    if not selected_root.is_dir():
        raise NotADirectoryError(f"不是目录：{selected_root}")
    if _is_link_or_junction(selected_root):
        raise ValueError("所选目录不能是符号链接或目录联接")
    selected_root = selected_root.resolve()

    located = _find_game_directory(selected_root)
    if located is None:
        project_root = selected_root
        runtime_markers = _find_runtime_markers(project_root)
        launchers = _find_launchers(project_root)
        status = (
            RenpyCompatibilityStatus.UNCERTAIN
            if "renpy" in runtime_markers and launchers
            else RenpyCompatibilityStatus.NOT_RENPY
        )
        return RenpyCompatibilityReport(
            schema_version=RENPY_COMPATIBILITY_REPORT_SCHEMA_VERSION,
            selected_root=selected_root,
            project_root=project_root,
            game_directory=None,
            status=status,
            summary=_summary_for(status),
            source_scripts=(),
            compiled_scripts=(),
            archives=(),
            translation_files=(),
            launchers=launchers,
            runtime_markers=runtime_markers,
            version_hints=(),
            issues=(),
        )

    project_root, game_directory = located
    runtime_markers = _find_runtime_markers(project_root)
    launchers = _find_launchers(project_root)
    if _is_link_or_junction(game_directory):
        scan = _ScanResult(
            [],
            [],
            [],
            [],
            [
                RenpyCompatibilityIssue(
                    RenpyCompatibilityIssueCode.SYMLINK_SKIPPED,
                    _relative(game_directory, project_root),
                    "game 目录是链接或目录联接，未进入扫描",
                )
            ],
        )
    else:
        scan = _scan_game_directory(
            game_directory,
            project_root,
            max_depth=max_depth,
            max_entries=max_entries,
        )
    version_hints, version_issues = _read_version_hint(project_root)
    scan.issues.extend(version_issues)

    if scan.source_scripts and (scan.compiled_scripts or scan.archives):
        scan.issues.append(
            RenpyCompatibilityIssue(
                RenpyCompatibilityIssueCode.MIXED_SOURCE_AND_PACKAGED,
                _relative(game_directory, project_root),
                "同时发现源脚本与成品文件；当前流程只使用松散源脚本",
            )
        )

    incomplete_codes = {
        RenpyCompatibilityIssueCode.SYMLINK_SKIPPED,
        RenpyCompatibilityIssueCode.DEPTH_LIMIT_REACHED,
        RenpyCompatibilityIssueCode.ENTRY_LIMIT_REACHED,
        RenpyCompatibilityIssueCode.DIRECTORY_UNREADABLE,
    }
    scan_incomplete = any(issue.code in incomplete_codes for issue in scan.issues)
    has_packaged_content = bool(scan.compiled_scripts or scan.archives)
    has_translation_content = bool(scan.translation_files)
    has_strong_packaged_evidence = bool(
        scan.compiled_scripts or (scan.archives and (launchers or "renpy" in runtime_markers))
    )

    if scan_incomplete:
        status = RenpyCompatibilityStatus.UNCERTAIN
    elif scan.source_scripts:
        status = RenpyCompatibilityStatus.SOURCE_READY
    elif has_packaged_content and has_strong_packaged_evidence:
        status = RenpyCompatibilityStatus.PACKAGED_REQUIRES_IMPORT
    elif has_packaged_content:
        status = RenpyCompatibilityStatus.UNCERTAIN
        scan.issues.append(
            RenpyCompatibilityIssue(
                RenpyCompatibilityIssueCode.WEAK_ARCHIVE_EVIDENCE,
                _relative(game_directory, project_root),
                "只有归档扩展名，缺少启动器、运行时或编译脚本佐证",
            )
        )
    elif has_translation_content:
        status = RenpyCompatibilityStatus.UNCERTAIN
    elif "renpy" in runtime_markers and launchers:
        status = RenpyCompatibilityStatus.UNCERTAIN
    else:
        status = RenpyCompatibilityStatus.NOT_RENPY

    return RenpyCompatibilityReport(
        schema_version=RENPY_COMPATIBILITY_REPORT_SCHEMA_VERSION,
        selected_root=selected_root,
        project_root=project_root,
        game_directory=_relative(game_directory, project_root),
        status=status,
        summary=_summary_for(status),
        source_scripts=_sorted_paths(scan.source_scripts),
        compiled_scripts=_sorted_paths(scan.compiled_scripts),
        archives=_sorted_paths(scan.archives),
        translation_files=_sorted_paths(scan.translation_files),
        launchers=launchers,
        runtime_markers=runtime_markers,
        version_hints=version_hints,
        issues=tuple(scan.issues),
    )
