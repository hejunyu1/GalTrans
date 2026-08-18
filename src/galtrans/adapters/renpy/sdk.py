from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from galtrans.adapters.renpy.extractor import extract_renpy_path
from galtrans.adapters.renpy.template import (
    OfficialTemplateEntry,
    RenpyTemplateError,
    is_valid_renpy_language,
    read_official_translation_templates,
)
from galtrans.ir import SegmentKind, TextSegment


_VERSION_RE = re.compile(r"Ren'Py\s+(?P<version>\d+(?:\.\d+)+(?:\.\d+)*)")
_FATAL_OUTPUT_MARKERS = (
    "Traceback (most recent call last):",
    "PermissionError:",
    "FileNotFoundError:",
)


class RenpySdkError(RuntimeError):
    """Raised when the SDK boundary cannot complete safely."""


@dataclass(frozen=True, slots=True)
class RenpyTemplateMapping:
    segment_id: str
    source_file: str
    line_number: int
    kind: SegmentKind
    source_text: str
    template_file: str
    translation_identifier: str | None
    source_code: str
    literal_start: int
    literal_end: int
    protected_tokens: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RenpySdkCrosscheck:
    sdk_root: Path
    executable: Path
    version: str
    language: str
    source_file_count: int
    template_file_count: int
    galtrans_dialogue_count: int
    official_dialogue_count: int
    galtrans_string_count: int
    official_string_count: int
    mappings: tuple[RenpyTemplateMapping, ...]
    unmatched_segment_ids: tuple[str, ...]
    unmatched_template_entries: tuple[str, ...]
    template_warnings: tuple[str, ...]
    lint_report: str

    @property
    def mapped_segment_count(self) -> int:
        return len(self.mappings)

    @property
    def matches(self) -> bool:
        return (
            self.galtrans_dialogue_count == self.official_dialogue_count
            and self.galtrans_string_count == self.official_string_count
            and not self.unmatched_segment_ids
            and not self.unmatched_template_entries
            and not self.template_warnings
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["sdk_root"] = str(self.sdk_root)
        result["executable"] = str(self.executable)
        result["mapped_segment_count"] = self.mapped_segment_count
        result["matches"] = self.matches
        return result


def resolve_renpy_sdk(path: Path) -> tuple[Path, Path]:
    """Resolve an SDK root without relying on the archive's directory name."""
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Ren'Py SDK 路径不存在：{resolved}")

    if resolved.is_file():
        if resolved.name.lower() != "renpy.exe":
            raise RenpySdkError(f"不是 Ren'Py 可执行文件：{resolved}")
        sdk_root = resolved.parent
        if not (sdk_root / "renpy.py").is_file() or not (sdk_root / "renpy").is_dir():
            raise RenpySdkError(
                f"renpy.exe 所在目录缺少 renpy.py 或 renpy 目录：{sdk_root}"
            )
        candidates = (sdk_root,)
    else:
        candidates = tuple(
            candidate.parent
            for candidate in resolved.rglob("renpy.exe")
            if (candidate.parent / "renpy.py").is_file()
            and (candidate.parent / "renpy").is_dir()
        )
        if (resolved / "renpy.exe").is_file() and resolved not in candidates:
            candidates = (resolved, *candidates)

    unique_candidates = tuple(dict.fromkeys(candidates))
    if not unique_candidates:
        raise RenpySdkError(
            f"未找到同时包含 renpy.exe、renpy.py 和 renpy 目录的 SDK 根目录：{resolved}"
        )
    if len(unique_candidates) > 1:
        listed = "、".join(str(candidate) for candidate in unique_candidates)
        raise RenpySdkError(f"找到多个 Ren'Py SDK 根目录，无法安全选择：{listed}")

    sdk_root = unique_candidates[0]
    return sdk_root, sdk_root / "renpy.exe"


def _run_sdk(
    executable: Path,
    sdk_root: Path,
    arguments: list[str],
    *,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    command = [str(executable), *arguments]
    try:
        completed = subprocess.run(
            command,
            cwd=sdk_root,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RenpySdkError(f"Ren'Py SDK 命令无法完成：{error}") from error

    combined_output = completed.stdout + completed.stderr
    fatal_marker = next(
        (marker for marker in _FATAL_OUTPUT_MARKERS if marker in combined_output),
        None,
    )
    if completed.returncode != 0 or fatal_marker is not None:
        detail = combined_output.strip() or "命令没有输出错误详情"
        raise RenpySdkError(
            f"Ren'Py SDK 命令失败（退出码 {completed.returncode}）："
            f"{' '.join(command)}\n{detail}"
        )
    return completed


def _source_paths(project_root: Path) -> tuple[Path, ...]:
    game_root = project_root / "game"
    if not game_root.is_dir():
        raise RenpySdkError(f"Ren'Py 项目缺少 game 目录：{project_root}")

    paths = tuple(
        candidate
        for candidate in sorted(game_root.rglob("*"))
        if candidate.is_file()
        and candidate.suffix.lower() in {".rpy", ".rpym"}
        and "tl" not in candidate.relative_to(game_root).parts
    )
    if not paths:
        raise RenpySdkError(f"Ren'Py 项目的 game 目录中没有源脚本：{game_root}")
    return paths


def _source_hashes(project_root: Path) -> dict[str, str]:
    return {
        path.relative_to(project_root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in _source_paths(project_root)
    }


def _mirror_sources(project_root: Path, staged_root: Path) -> int:
    source_paths = _source_paths(project_root)
    for source_path in source_paths:
        relative_path = source_path.relative_to(project_root)
        destination = staged_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination)
    return len(source_paths)


def _mapping_key(
    *,
    source_file: str,
    line_number: int,
    kind: SegmentKind,
    source_text: str,
) -> tuple[str, int, SegmentKind, str]:
    return source_file.replace("\\", "/"), line_number, kind, source_text


def _map_template_entries(
    segments: tuple[TextSegment, ...],
    entries: tuple[OfficialTemplateEntry, ...],
) -> tuple[
    tuple[RenpyTemplateMapping, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    segments_by_key: dict[tuple[str, int, SegmentKind, str], list[TextSegment]] = {}
    for segment in segments:
        key = _mapping_key(
            source_file=segment.source_file,
            line_number=segment.line_number,
            kind=segment.kind,
            source_text=segment.source_text,
        )
        segments_by_key.setdefault(key, []).append(segment)

    mappings: list[RenpyTemplateMapping] = []
    mapped_segment_ids: set[str] = set()
    unmatched_entries: list[str] = []
    for entry in entries:
        key = _mapping_key(
            source_file=entry.source_file,
            line_number=entry.line_number,
            kind=entry.kind,
            source_text=entry.source_text,
        )
        candidates = [
            segment
            for segment in segments_by_key.get(key, [])
            if segment.id not in mapped_segment_ids
        ]
        if len(candidates) != 1:
            unmatched_entries.append(
                f"{entry.template_file}:{entry.source_file}:{entry.line_number}:"
                f"{entry.kind.value}:{entry.source_text}"
            )
            continue

        segment = candidates[0]
        mapped_segment_ids.add(segment.id)
        mappings.append(
            RenpyTemplateMapping(
                segment_id=segment.id,
                source_file=segment.source_file,
                line_number=segment.line_number,
                kind=segment.kind,
                source_text=segment.source_text,
                template_file=entry.template_file,
                translation_identifier=entry.translation_identifier,
                source_code=entry.source_code,
                literal_start=entry.literal_start,
                literal_end=entry.literal_end,
                protected_tokens=tuple(token.value for token in segment.protected_tokens),
            )
        )

    unmatched_segment_ids = tuple(
        segment.id for segment in segments if segment.id not in mapped_segment_ids
    )
    return tuple(mappings), unmatched_segment_ids, tuple(unmatched_entries)


def crosscheck_renpy_sdk(
    sdk_path: Path,
    project_path: Path,
    *,
    language: str = "schinese",
    timeout_seconds: float = 60.0,
) -> RenpySdkCrosscheck:
    """Cross-check GalTrans extraction against official output on a source-only mirror."""
    if not is_valid_renpy_language(language):
        raise RenpySdkError(
            "语言名只能包含英文字母、数字、下划线和连字符，且必须以字母开头"
        )

    project_root = project_path.expanduser().resolve()
    if not project_root.exists():
        raise FileNotFoundError(f"Ren'Py 项目路径不存在：{project_root}")
    if not project_root.is_dir():
        raise NotADirectoryError(f"Ren'Py 项目路径不是目录：{project_root}")

    sdk_root, executable = resolve_renpy_sdk(sdk_path)
    version_result = _run_sdk(
        executable,
        sdk_root,
        ["--version"],
        timeout_seconds=timeout_seconds,
    )
    version_output = (version_result.stdout + version_result.stderr).strip()
    version_match = _VERSION_RE.search(version_output)
    if version_match is None:
        raise RenpySdkError(f"无法识别 Ren'Py SDK 版本：{version_output or '无输出'}")

    input_hashes = _source_hashes(project_root)
    with tempfile.TemporaryDirectory(prefix="galtrans-renpy-") as temporary_directory:
        staged_root = Path(temporary_directory) / "project"
        source_file_count = _mirror_sources(project_root, staged_root)
        save_root = staged_root / ".renpy-saves"

        extraction_results = extract_renpy_path(staged_root)
        segments = tuple(
            segment for result in extraction_results for segment in result.segments
        )
        galtrans_dialogue_count = sum(
            segment.kind in {SegmentKind.DIALOGUE, SegmentKind.NARRATION}
            for segment in segments
        )
        galtrans_string_count = sum(
            segment.kind is SegmentKind.MENU_CHOICE
            for segment in segments
        )

        _run_sdk(
            executable,
            sdk_root,
            [
                str(staged_root),
                "translate",
                language,
                "--savedir",
                str(save_root),
            ],
            timeout_seconds=timeout_seconds,
        )
        try:
            official_template = read_official_translation_templates(
                staged_root / "game" / "tl" / language,
                language,
            )
        except RenpyTemplateError as error:
            raise RenpySdkError(str(error)) from error
        official_dialogue_count = sum(
            entry.kind in {SegmentKind.DIALOGUE, SegmentKind.NARRATION}
            for entry in official_template.entries
        )
        official_string_count = sum(
            entry.kind is SegmentKind.MENU_CHOICE for entry in official_template.entries
        )
        mappings, unmatched_segment_ids, unmatched_template_entries = (
            _map_template_entries(segments, official_template.entries)
        )

        lint_result = _run_sdk(
            executable,
            sdk_root,
            [str(staged_root), "lint", "--savedir", str(save_root)],
            timeout_seconds=timeout_seconds,
        )
        lint_report = (lint_result.stdout + lint_result.stderr).lstrip("\ufeff").strip()
        if not lint_report:
            raise RenpySdkError("Ren'Py lint 命令没有生成可检查的报告")

    if _source_hashes(project_root) != input_hashes:
        raise RenpySdkError("交叉验证期间输入项目源脚本发生变化，结果已拒绝")

    return RenpySdkCrosscheck(
        sdk_root=sdk_root,
        executable=executable,
        version=version_match.group("version"),
        language=language,
        source_file_count=source_file_count,
        template_file_count=len(official_template.template_files),
        galtrans_dialogue_count=galtrans_dialogue_count,
        official_dialogue_count=official_dialogue_count,
        galtrans_string_count=galtrans_string_count,
        official_string_count=official_string_count,
        mappings=mappings,
        unmatched_segment_ids=unmatched_segment_ids,
        unmatched_template_entries=unmatched_template_entries,
        template_warnings=official_template.warnings,
        lint_report=lint_report,
    )
