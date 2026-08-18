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
from galtrans.ir import SegmentKind


_VERSION_RE = re.compile(r"Ren'Py\s+(?P<version>\d+(?:\.\d+)+(?:\.\d+)*)")
_LANGUAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_FATAL_OUTPUT_MARKERS = (
    "Traceback (most recent call last):",
    "PermissionError:",
    "FileNotFoundError:",
)


class RenpySdkError(RuntimeError):
    """Raised when the SDK boundary cannot complete safely."""


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
    lint_report: str

    @property
    def matches(self) -> bool:
        return (
            self.galtrans_dialogue_count == self.official_dialogue_count
            and self.galtrans_string_count == self.official_string_count
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["sdk_root"] = str(self.sdk_root)
        result["executable"] = str(self.executable)
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


def _official_template_counts(
    staged_root: Path,
    language: str,
) -> tuple[int, int, int]:
    translation_root = staged_root / "game" / "tl" / language
    if not translation_root.is_dir():
        raise RenpySdkError(f"Ren'Py 未生成翻译目录：{translation_root}")

    template_paths = tuple(
        path
        for path in sorted(translation_root.rglob("*.rpy"))
        if path.relative_to(translation_root).as_posix() != "common.rpy"
    )
    if not template_paths:
        raise RenpySdkError(f"Ren'Py 未生成项目翻译模板：{translation_root}")

    dialogue_re = re.compile(
        rf"^\s*translate\s+{re.escape(language)}\s+(?!strings\b)\S+\s*:\s*$",
        re.MULTILINE,
    )
    string_re = re.compile(r"^\s*old\s+['\"]", re.MULTILINE)
    dialogue_count = 0
    string_count = 0
    for path in template_paths:
        text = path.read_text(encoding="utf-8-sig", errors="strict")
        dialogue_count += len(dialogue_re.findall(text))
        string_count += len(string_re.findall(text))
    return len(template_paths), dialogue_count, string_count


def crosscheck_renpy_sdk(
    sdk_path: Path,
    project_path: Path,
    *,
    language: str = "schinese",
    timeout_seconds: float = 60.0,
) -> RenpySdkCrosscheck:
    """Cross-check GalTrans extraction against official output on a source-only mirror."""
    if not _LANGUAGE_RE.fullmatch(language):
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
        galtrans_dialogue_count = sum(
            segment.kind in {SegmentKind.DIALOGUE, SegmentKind.NARRATION}
            for result in extraction_results
            for segment in result.segments
        )
        galtrans_string_count = sum(
            segment.kind is SegmentKind.MENU_CHOICE
            for result in extraction_results
            for segment in result.segments
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
        template_file_count, official_dialogue_count, official_string_count = (
            _official_template_counts(staged_root, language)
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
        template_file_count=template_file_count,
        galtrans_dialogue_count=galtrans_dialogue_count,
        official_dialogue_count=official_dialogue_count,
        galtrans_string_count=galtrans_string_count,
        official_string_count=official_string_count,
        lint_report=lint_report,
    )
