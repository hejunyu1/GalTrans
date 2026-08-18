from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from galtrans.adapters.renpy.renderer import RenderedRenpyFragment
from galtrans.adapters.renpy.template import is_valid_renpy_language
from galtrans.ir import SegmentKind


class RenpyExportError(ValueError):
    """Raised when translation files cannot be assembled or published safely."""


@dataclass(frozen=True, slots=True)
class RenderedRenpyFile:
    relative_path: str
    template_file: str
    language: str
    segment_ids: tuple[str, ...]
    content: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WrittenRenpyTranslationDirectory:
    root: Path
    files: tuple[Path, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "files": [str(path) for path in self.files],
        }


def _safe_template_path(template_file: str) -> PurePosixPath:
    normalized = template_file.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        raise RenpyExportError(f"官方模板路径不是安全的相对路径：{template_file}")

    raw_parts = normalized.split("/")
    if any(part in {"", ".", ".."} or ":" in part for part in raw_parts):
        raise RenpyExportError(f"官方模板路径不是安全的相对路径：{template_file}")

    path = PurePosixPath(*raw_parts)
    if path.suffix.lower() != ".rpy":
        raise RenpyExportError(f"官方模板必须是 .rpy 文件：{template_file}")
    return path


def _validate_fragment(fragment: RenderedRenpyFragment, language: str) -> None:
    if fragment.language != language:
        raise RenpyExportError(
            f"{fragment.segment_id} 的片段语言 {fragment.language} 与输出语言 {language} 不一致"
        )
    if fragment.kind is SegmentKind.MENU_CHOICE:
        if not fragment.requires_strings_header or fragment.translation_identifier is not None:
            raise RenpyExportError(f"{fragment.segment_id} 的菜单片段结构不一致")
    elif fragment.kind in {SegmentKind.DIALOGUE, SegmentKind.NARRATION}:
        if fragment.requires_strings_header or fragment.translation_identifier is None:
            raise RenpyExportError(f"{fragment.segment_id} 的对话片段结构不一致")
    else:
        raise RenpyExportError(f"{fragment.segment_id} 的文本类型不受支持：{fragment.kind}")
    if not fragment.content:
        raise RenpyExportError(f"{fragment.segment_id} 的翻译片段为空")


def assemble_official_translation_files(
    fragments: Iterable[RenderedRenpyFragment],
    *,
    language: str,
) -> tuple[RenderedRenpyFile, ...]:
    """Group checked fragments into deterministic Ren'Py translation files in memory."""
    if not is_valid_renpy_language(language):
        raise RenpyExportError(
            "语言名只能包含英文字母、数字、下划线和连字符，且必须以字母开头"
        )

    grouped: dict[str, list[RenderedRenpyFragment]] = {}
    seen_segment_ids: set[str] = set()
    seen_translation_ids: set[str] = set()
    for fragment in fragments:
        _validate_fragment(fragment, language)
        template_path = _safe_template_path(fragment.template_file)
        template_file = template_path.as_posix()
        if fragment.segment_id in seen_segment_ids:
            raise RenpyExportError(f"重复的文本段 ID：{fragment.segment_id}")
        seen_segment_ids.add(fragment.segment_id)

        if fragment.translation_identifier is not None:
            if fragment.translation_identifier in seen_translation_ids:
                raise RenpyExportError(
                    f"重复的官方翻译 ID：{fragment.translation_identifier}"
                )
            seen_translation_ids.add(fragment.translation_identifier)
        grouped.setdefault(template_file, []).append(fragment)

    if not grouped:
        raise RenpyExportError("没有可组装的 Ren'Py 翻译片段")

    files: list[RenderedRenpyFile] = []
    for template_file, items in grouped.items():
        dialogue_items = [item for item in items if not item.requires_strings_header]
        string_items = [item for item in items if item.requires_strings_header]
        ordered_items = dialogue_items + string_items
        content = "".join(item.content for item in dialogue_items)
        if string_items:
            content += f"translate {language} strings:\n\n"
            content += "".join(item.content for item in string_items)

        relative_path = PurePosixPath(
            "game",
            "tl",
            language,
            template_file,
        ).as_posix()
        files.append(
            RenderedRenpyFile(
                relative_path=relative_path,
                template_file=template_file,
                language=language,
                segment_ids=tuple(item.segment_id for item in ordered_items),
                content=content,
            )
        )
    return tuple(files)


def _safe_output_relative_path(file: RenderedRenpyFile) -> PurePosixPath:
    path = _safe_template_path(file.relative_path)
    if not is_valid_renpy_language(file.language):
        raise RenpyExportError(f"输出文件语言名不受支持：{file.language}")
    template_path = _safe_template_path(file.template_file)
    expected_path = PurePosixPath("game", "tl", file.language, template_path)
    if path != expected_path:
        raise RenpyExportError(f"输出文件路径不在 Ren'Py 翻译目录中：{file.relative_path}")
    return path


def write_official_translation_directory(
    files: Iterable[RenderedRenpyFile],
    output_root: Path,
    *,
    input_project_root: Path,
) -> WrittenRenpyTranslationDirectory:
    """Publish assembled files to a new directory without touching the input project."""
    rendered_files = tuple(files)
    if not rendered_files:
        raise RenpyExportError("没有可写入的 Ren'Py 翻译文件")

    project_root = input_project_root.expanduser().resolve()
    if not project_root.is_dir():
        raise RenpyExportError(f"输入项目目录不存在：{project_root}")

    resolved_output = output_root.expanduser().resolve()
    if resolved_output.exists():
        raise FileExistsError(f"输出目录已存在，拒绝覆盖：{resolved_output}")
    if (
        resolved_output == project_root
        or resolved_output.is_relative_to(project_root)
        or project_root.is_relative_to(resolved_output)
    ):
        raise RenpyExportError(
            f"输出目录不得与输入项目重叠：{resolved_output} / {project_root}"
        )

    relative_paths: list[PurePosixPath] = []
    seen_paths: set[str] = set()
    seen_segment_ids: set[str] = set()
    output_language: str | None = None
    for file in rendered_files:
        if not file.content or not file.segment_ids:
            raise RenpyExportError(f"输出文件缺少内容或文本段记录：{file.relative_path}")
        if output_language is None:
            output_language = file.language
        elif file.language != output_language:
            raise RenpyExportError("一次目录发布只能包含一种 Ren'Py 翻译语言")
        for segment_id in file.segment_ids:
            if segment_id in seen_segment_ids:
                raise RenpyExportError(f"重复的文本段 ID：{segment_id}")
            seen_segment_ids.add(segment_id)

        relative_path = _safe_output_relative_path(file)
        normalized = relative_path.as_posix()
        if normalized in seen_paths:
            raise RenpyExportError(f"重复的输出文件路径：{normalized}")
        seen_paths.add(normalized)
        relative_paths.append(relative_path)

    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=".galtrans-renpy-", dir=resolved_output.parent)
    )
    published = False
    try:
        for file, relative_path in zip(rendered_files, relative_paths, strict=True):
            destination = staging_root.joinpath(*relative_path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("x", encoding="utf-8-sig", newline="\n") as stream:
                stream.write(file.content)

        if resolved_output.exists():
            raise FileExistsError(f"输出目录已存在，拒绝覆盖：{resolved_output}")
        staging_root.rename(resolved_output)
        published = True
    finally:
        if not published and staging_root.exists():
            shutil.rmtree(staging_root)

    written_paths = tuple(
        resolved_output.joinpath(*relative_path.parts)
        for relative_path in relative_paths
    )
    return WrittenRenpyTranslationDirectory(
        root=resolved_output,
        files=written_paths,
    )
