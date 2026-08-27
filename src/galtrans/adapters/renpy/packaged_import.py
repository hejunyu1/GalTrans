from __future__ import annotations

import hashlib
import io
import json
import os
import pickle
import re
import shutil
import tempfile
import unicodedata
import zlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from galtrans.adapters.renpy.compatibility import (
    RENPY_COMPATIBILITY_REPORT_SCHEMA_VERSION,
    RenpyCompatibilityStatus,
    inspect_renpy_compatibility,
)
from galtrans.encoding import detect_and_decode

RENPY_PACKAGED_IMPORT_MANIFEST_SCHEMA_VERSION = 1

_IMPORTER_ID = "renpy_plain_rpa3_sources_v1"
_MANIFEST_NAME = "galtrans-import.json"
_RPA3_HEADER_RE = re.compile(rb"\ARPA-3\.0 ([0-9a-fA-F]{16}) ([0-9a-fA-F]{8})\n")
_HEADER_READ_BYTES = 64
_DEFAULT_MAX_ARCHIVES = 128
_DEFAULT_MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
_DEFAULT_MAX_TOTAL_ARCHIVE_BYTES = 32 * 1024 * 1024 * 1024
_DEFAULT_MAX_INDEX_BYTES = 32 * 1024 * 1024
_DEFAULT_MAX_ARCHIVE_ENTRIES = 50_000
_DEFAULT_MAX_SOURCE_FILE_BYTES = 16 * 1024 * 1024
_DEFAULT_MAX_TOTAL_SOURCE_BYTES = 128 * 1024 * 1024
_SOURCE_SUFFIXES = {".rpy", ".rpym"}
_COMPILED_SUFFIXES = {".rpyc", ".rpymc"}
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"\\|?*')
_WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_ALLOWED_RENPY_PICKLE_CLASSES: dict[tuple[str, str], type[dict[Any, Any]] | type[list[Any]]] = {
    ("renpy.python", "RevertableDict"): dict,
    ("renpy.python", "RevertableList"): list,
    ("renpy.revertable", "RevertableDict"): dict,
    ("renpy.revertable", "RevertableList"): list,
}


class RenpyPackagedImportError(ValueError):
    """Raised when a packaged Ren'Py input cannot cross the import boundary safely."""


class RenpyImportAuthorization(StrEnum):
    """Closed caller assertion recorded in every packaged import manifest."""

    USER_CONFIRMED_LOCAL_PROCESSING = "user_confirmed_local_processing"


@dataclass(frozen=True, slots=True)
class RenpyImportedArchive:
    relative_path: str
    format: str
    size_bytes: int
    sha256: str
    index_offset: int
    index_sha256: str
    entry_count: int
    source_entry_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "format": self.format,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "index_offset": self.index_offset,
            "index_sha256": self.index_sha256,
            "entry_count": self.entry_count,
            "source_entry_count": self.source_entry_count,
        }


@dataclass(frozen=True, slots=True)
class RenpyImportedSource:
    relative_path: str
    archive_path: str
    archive_entry: str
    offset: int
    size_bytes: int
    sha256: str
    encoding: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "archive_path": self.archive_path,
            "archive_entry": self.archive_entry,
            "offset": self.offset,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "encoding": self.encoding,
        }


@dataclass(frozen=True, slots=True)
class RenpyPackagedImportManifest:
    schema_version: int
    import_id: str
    importer: str
    authorization: RenpyImportAuthorization
    source_project_root: Path
    compatibility_report_schema_version: int
    archives: tuple[RenpyImportedArchive, ...]
    source_files: tuple[RenpyImportedSource, ...]
    ignored_compiled_scripts: tuple[str, ...]
    ignored_translation_files: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "import_id": self.import_id,
            "importer": self.importer,
            "authorization": self.authorization.value,
            "source_project_root": str(self.source_project_root),
            "compatibility_report_schema_version": (
                self.compatibility_report_schema_version
            ),
            "archives": [archive.to_dict() for archive in self.archives],
            "source_files": [source.to_dict() for source in self.source_files],
            "ignored_compiled_scripts": list(self.ignored_compiled_scripts),
            "ignored_translation_files": list(self.ignored_translation_files),
        }


@dataclass(frozen=True, slots=True)
class RenpyPackagedImportResult:
    root: Path
    manifest_path: Path
    source_files: tuple[Path, ...]
    manifest: RenpyPackagedImportManifest

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "manifest_path": str(self.manifest_path),
            "source_files": [str(path) for path in self.source_files],
            "manifest": self.manifest.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class _ArchiveEntry:
    name: str
    offset: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _PlannedSource:
    record: RenpyImportedSource
    content: bytes


@dataclass(frozen=True, slots=True)
class _ArchiveSnapshot:
    path: Path
    signature: tuple[int, int, int]
    archive: RenpyImportedArchive
    sources: tuple[_PlannedSource, ...]
    ignored_compiled_scripts: tuple[str, ...]
    ignored_translation_files: tuple[str, ...]


class _RestrictedRpaIndexUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        allowed = _ALLOWED_RENPY_PICKLE_CLASSES.get((module, name))
        if allowed is None:
            raise pickle.UnpicklingError(
                f"RPA 索引包含不允许的 Python 类型: {module}.{name}"
            )
        return allowed

    def persistent_load(self, persistent_id: object) -> Any:
        del persistent_id
        raise pickle.UnpicklingError("RPA 索引包含不允许的持久化引用")


def _roots_overlap(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _validate_limit(name: str, value: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RenpyPackagedImportError(f"{name} 必须是大于 0 的整数")
    if value > maximum:
        raise RenpyPackagedImportError(
            f"{name} 只能进一步收紧, 不能超过当前支持上限 {maximum}"
        )


def _safe_archive_entry_path(name: object, *, archive_path: str) -> PurePosixPath:
    if not isinstance(name, str):
        raise RenpyPackagedImportError(
            f"{archive_path} 的 RPA 索引包含非字符串路径"
        )
    try:
        encoded_name = name.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RenpyPackagedImportError(
            f"{archive_path} 的 RPA 路径包含无效 Unicode"
        ) from error
    if not name or len(encoded_name) > 4096:
        raise RenpyPackagedImportError(f"{archive_path} 的 RPA 路径为空或过长")
    if name != unicodedata.normalize("NFC", name):
        raise RenpyPackagedImportError(
            f"{archive_path} 的 RPA 路径不是规范 Unicode NFC: {name!r}"
        )
    if "\\" in name or name.startswith("/"):
        raise RenpyPackagedImportError(
            f"{archive_path} 的 RPA 路径不是安全的正斜杠相对路径: {name!r}"
        )
    parts = name.split("/")
    if any(
        part in {"", ".", ".."}
        or ":" in part
        or any(ord(character) < 32 for character in part)
        for part in parts
    ):
        raise RenpyPackagedImportError(
            f"{archive_path} 的 RPA 路径不是安全相对路径: {name!r}"
        )
    return PurePosixPath(*parts)


def _safe_source_output_path(entry_path: PurePosixPath, *, archive_path: str) -> str:
    if entry_path.parts[0].casefold() == "game":
        raise RenpyPackagedImportError(
            f"{archive_path} 的源码条目重复包含 game 前缀: {entry_path.as_posix()}"
        )
    for part in entry_path.parts:
        if (
            part.endswith((" ", "."))
            or any(character in _WINDOWS_INVALID_CHARACTERS for character in part)
            or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
        ):
            raise RenpyPackagedImportError(
                f"{archive_path} 的源码路径不能安全写入 Windows: {entry_path.as_posix()}"
            )
    return PurePosixPath("game", entry_path).as_posix()


def _decompress_index(data: bytes, *, archive_path: str, max_index_bytes: int) -> bytes:
    try:
        decompressor = zlib.decompressobj()
        index_data = decompressor.decompress(data, max_index_bytes + 1)
        if len(index_data) > max_index_bytes or decompressor.unconsumed_tail:
            raise RenpyPackagedImportError(
                f"{archive_path} 的 RPA 索引超过安全上限 {max_index_bytes} 字节"
            )
        index_data += decompressor.flush(max_index_bytes + 1 - len(index_data))
    except zlib.error as error:
        raise RenpyPackagedImportError(
            f"{archive_path} 的 RPA 索引不是有效的 zlib 数据: {error}"
        ) from error
    if len(index_data) > max_index_bytes:
        raise RenpyPackagedImportError(
            f"{archive_path} 的 RPA 索引超过安全上限 {max_index_bytes} 字节"
        )
    if not decompressor.eof or decompressor.unused_data:
        raise RenpyPackagedImportError(
            f"{archive_path} 的 RPA 索引压缩流不完整或包含尾随数据"
        )
    return index_data


def _load_restricted_index(data: bytes, *, archive_path: str) -> dict[object, object]:
    stream = io.BytesIO(data)
    try:
        loaded = _RestrictedRpaIndexUnpickler(
            stream,
            fix_imports=False,
            encoding="utf-8",
            errors="strict",
        ).load()
    except (
        AttributeError,
        EOFError,
        ImportError,
        IndexError,
        OverflowError,
        RecursionError,
        UnicodeError,
        ValueError,
        TypeError,
        pickle.UnpicklingError,
    ) as error:
        raise RenpyPackagedImportError(
            f"{archive_path} 的 RPA 索引不符合受限标准结构: {error}"
        ) from error
    if stream.read(1):
        raise RenpyPackagedImportError(f"{archive_path} 的 RPA pickle 包含尾随数据")
    if type(loaded) is not dict:
        raise RenpyPackagedImportError(f"{archive_path} 的 RPA 索引根对象必须是字典")
    return loaded


def _decode_entry(
    name: str,
    value: object,
    *,
    archive_path: str,
    archive_size: int,
    data_start: int,
    index_offset: int,
    key: int,
) -> _ArchiveEntry:
    if type(value) is not list or len(value) != 1:
        raise RenpyPackagedImportError(
            f"{archive_path} 的条目不是当前支持的单段普通 RPA 记录: {name}"
        )
    raw_segment = value[0]
    if type(raw_segment) is not tuple or len(raw_segment) not in {2, 3}:
        raise RenpyPackagedImportError(
            f"{archive_path} 的条目记录结构不受支持: {name}"
        )
    encoded_offset, encoded_length = raw_segment[:2]
    if (
        isinstance(encoded_offset, bool)
        or not isinstance(encoded_offset, int)
        or isinstance(encoded_length, bool)
        or not isinstance(encoded_length, int)
    ):
        raise RenpyPackagedImportError(
            f"{archive_path} 的条目范围不是整数: {name}"
        )
    if len(raw_segment) == 3 and raw_segment[2] != b"":
        raise RenpyPackagedImportError(
            f"{archive_path} 的条目包含兼容前缀, 超出当前普通 RPA 子集: {name}"
        )
    offset = encoded_offset ^ key
    size_bytes = encoded_length ^ key
    if (
        offset < data_start
        or size_bytes < 0
        or offset > index_offset
        or size_bytes > index_offset - offset
        or offset + size_bytes > archive_size
    ):
        raise RenpyPackagedImportError(
            f"{archive_path} 的条目范围越过归档数据区: {name}"
        )
    return _ArchiveEntry(name=name, offset=offset, size_bytes=size_bytes)


def _stat_signature(stat_result: os.stat_result) -> tuple[int, int, int]:
    return (stat_result.st_size, stat_result.st_mtime_ns, stat_result.st_ino)


def _hash_stream(stream: io.BufferedReader) -> str:
    digest = hashlib.sha256()
    stream.seek(0)
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _read_archive_snapshot(
    path: Path,
    *,
    relative_path: str,
    max_index_bytes: int,
    max_archive_entries: int,
    max_archive_bytes: int,
    max_source_file_bytes: int,
    remaining_archive_bytes: int,
    remaining_source_bytes: int,
) -> _ArchiveSnapshot:
    if not path.is_file() or _is_link_or_junction(path):
        raise RenpyPackagedImportError(
            f"RPA 归档不存在、不是普通文件或是链接: {relative_path}"
        )
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            archive_size = before.st_size
            if archive_size > max_archive_bytes:
                raise RenpyPackagedImportError(
                    f"{relative_path} 超过单个 RPA 安全上限 {max_archive_bytes} 字节"
                )
            if archive_size > remaining_archive_bytes:
                raise RenpyPackagedImportError(
                    "RPA 归档总量超过安全上限, 已在索引读取前停止"
                )
            header = stream.read(_HEADER_READ_BYTES)
            match = _RPA3_HEADER_RE.match(header)
            if match is None:
                raise RenpyPackagedImportError(
                    f"{relative_path} 不是当前唯一支持的官方普通 RPA-3.0 格式"
                )
            index_offset = int(match.group(1), 16)
            key = int(match.group(2), 16)
            header_size = match.end()
            if index_offset < header_size or index_offset >= archive_size:
                raise RenpyPackagedImportError(
                    f"{relative_path} 的 RPA 索引偏移越过归档边界"
                )
            compressed_size = archive_size - index_offset
            if compressed_size > max_index_bytes:
                raise RenpyPackagedImportError(
                    f"{relative_path} 的压缩 RPA 索引超过安全上限 {max_index_bytes} 字节"
                )
            stream.seek(index_offset)
            compressed_index = stream.read(compressed_size)
            if len(compressed_index) != compressed_size:
                raise RenpyPackagedImportError(f"{relative_path} 的 RPA 索引读取不完整")
            index_data = _decompress_index(
                compressed_index,
                archive_path=relative_path,
                max_index_bytes=max_index_bytes,
            )
            index = _load_restricted_index(index_data, archive_path=relative_path)
            if len(index) > max_archive_entries:
                raise RenpyPackagedImportError(
                    f"{relative_path} 的 RPA 条目超过安全上限 {max_archive_entries}"
                )

            source_entries: list[tuple[_ArchiveEntry, PurePosixPath, str]] = []
            all_entries: list[_ArchiveEntry] = []
            ignored_compiled: list[str] = []
            ignored_translations: list[str] = []
            for raw_name, value in index.items():
                entry_path = _safe_archive_entry_path(
                    raw_name,
                    archive_path=relative_path,
                )
                name = entry_path.as_posix()
                entry = _decode_entry(
                    name,
                    value,
                    archive_path=relative_path,
                    archive_size=archive_size,
                    data_start=header_size,
                    index_offset=index_offset,
                    key=key,
                )
                all_entries.append(entry)
                suffix = entry_path.suffix.casefold()
                is_translation = entry_path.parts[0].casefold() == "tl"
                audit_name = f"{relative_path}!{name}"
                if is_translation and suffix in (_SOURCE_SUFFIXES | _COMPILED_SUFFIXES):
                    ignored_translations.append(audit_name)
                elif suffix in _COMPILED_SUFFIXES:
                    ignored_compiled.append(audit_name)
                elif suffix in _SOURCE_SUFFIXES:
                    if entry.size_bytes > max_source_file_bytes:
                        raise RenpyPackagedImportError(
                            f"{audit_name} 超过单个源码安全上限 {max_source_file_bytes} 字节"
                        )
                    output_path = _safe_source_output_path(
                        entry_path,
                        archive_path=relative_path,
                    )
                    source_entries.append((entry, entry_path, output_path))

            previous_end = header_size
            for entry in sorted(all_entries, key=lambda item: (item.offset, item.size_bytes)):
                if entry.offset < previous_end:
                    raise RenpyPackagedImportError(
                        f"{relative_path} 的 RPA 条目范围重叠: {entry.name}"
                    )
                previous_end = entry.offset + entry.size_bytes

            source_bytes = sum(entry.size_bytes for entry, _, _ in source_entries)
            if source_bytes > remaining_source_bytes:
                raise RenpyPackagedImportError(
                    "RPA 源脚本总量超过安全上限, 已在写入前停止"
                )
            archive_sha256 = _hash_stream(stream)
            planned_sources: list[_PlannedSource] = []
            for entry, entry_path, output_path in source_entries:
                stream.seek(entry.offset)
                content = stream.read(entry.size_bytes)
                if len(content) != entry.size_bytes:
                    raise RenpyPackagedImportError(
                        f"{relative_path}!{entry.name} 的源码数据读取不完整"
                    )
                decoded = detect_and_decode(content)
                if decoded is None:
                    raise RenpyPackagedImportError(
                        f"{relative_path}!{entry.name} 不是可安全识别编码的 Ren'Py 源脚本"
                    )
                planned_sources.append(
                    _PlannedSource(
                        record=RenpyImportedSource(
                            relative_path=output_path,
                            archive_path=relative_path,
                            archive_entry=entry_path.as_posix(),
                            offset=entry.offset,
                            size_bytes=entry.size_bytes,
                            sha256=hashlib.sha256(content).hexdigest(),
                            encoding=decoded.encoding,
                        ),
                        content=content,
                    )
                )
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise RenpyPackagedImportError(
            f"无法只读检查 RPA 归档 {relative_path}: {error}"
        ) from error

    signature = _stat_signature(before)
    if _stat_signature(after) != signature:
        raise RenpyPackagedImportError(
            f"导入期间 RPA 归档发生变化, 已停止: {relative_path}"
        )
    archive = RenpyImportedArchive(
        relative_path=relative_path,
        format="RPA-3.0",
        size_bytes=archive_size,
        sha256=archive_sha256,
        index_offset=index_offset,
        index_sha256=hashlib.sha256(index_data).hexdigest(),
        entry_count=len(index),
        source_entry_count=len(planned_sources),
    )
    return _ArchiveSnapshot(
        path=path,
        signature=signature,
        archive=archive,
        sources=tuple(planned_sources),
        ignored_compiled_scripts=tuple(sorted(ignored_compiled, key=str.casefold)),
        ignored_translation_files=tuple(sorted(ignored_translations, key=str.casefold)),
    )


def _verify_archive_snapshots(snapshots: tuple[_ArchiveSnapshot, ...]) -> None:
    for snapshot in snapshots:
        if not snapshot.path.is_file() or _is_link_or_junction(snapshot.path):
            raise RenpyPackagedImportError(
                f"发布前 RPA 归档已被替换或变成链接: {snapshot.archive.relative_path}"
            )
        try:
            current = _stat_signature(snapshot.path.stat())
        except OSError as error:
            raise RenpyPackagedImportError(
                f"发布前无法复核 RPA 归档: {snapshot.archive.relative_path}"
            ) from error
        if current != snapshot.signature:
            raise RenpyPackagedImportError(
                f"发布前 RPA 归档发生变化, 已停止: {snapshot.archive.relative_path}"
            )


def _import_id(
    archives: tuple[RenpyImportedArchive, ...],
    sources: tuple[RenpyImportedSource, ...],
) -> str:
    identity = {
        "schema_version": RENPY_PACKAGED_IMPORT_MANIFEST_SCHEMA_VERSION,
        "importer": _IMPORTER_ID,
        "archives": [archive.to_dict() for archive in archives],
        "source_files": [source.to_dict() for source in sources],
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"renpy_import_{hashlib.sha256(encoded).hexdigest()[:24]}"


def import_renpy_packaged_sources(
    project_path: Path,
    output_path: Path,
    *,
    authorization: RenpyImportAuthorization,
    max_archives: int = _DEFAULT_MAX_ARCHIVES,
    max_archive_bytes: int = _DEFAULT_MAX_ARCHIVE_BYTES,
    max_total_archive_bytes: int = _DEFAULT_MAX_TOTAL_ARCHIVE_BYTES,
    max_index_bytes: int = _DEFAULT_MAX_INDEX_BYTES,
    max_archive_entries: int = _DEFAULT_MAX_ARCHIVE_ENTRIES,
    max_source_file_bytes: int = _DEFAULT_MAX_SOURCE_FILE_BYTES,
    max_total_source_bytes: int = _DEFAULT_MAX_TOTAL_SOURCE_BYTES,
) -> RenpyPackagedImportResult:
    """Import plain RPA-3.0 source entries into a new audited source-only snapshot."""
    if authorization is not RenpyImportAuthorization.USER_CONFIRMED_LOCAL_PROCESSING:
        raise RenpyPackagedImportError(
            "调用方必须明确确认用户有权对该游戏做本地处理"
        )
    for name, value, maximum in (
        ("max_archives", max_archives, _DEFAULT_MAX_ARCHIVES),
        ("max_archive_bytes", max_archive_bytes, _DEFAULT_MAX_ARCHIVE_BYTES),
        (
            "max_total_archive_bytes",
            max_total_archive_bytes,
            _DEFAULT_MAX_TOTAL_ARCHIVE_BYTES,
        ),
        ("max_index_bytes", max_index_bytes, _DEFAULT_MAX_INDEX_BYTES),
        (
            "max_archive_entries",
            max_archive_entries,
            _DEFAULT_MAX_ARCHIVE_ENTRIES,
        ),
        (
            "max_source_file_bytes",
            max_source_file_bytes,
            _DEFAULT_MAX_SOURCE_FILE_BYTES,
        ),
        (
            "max_total_source_bytes",
            max_total_source_bytes,
            _DEFAULT_MAX_TOTAL_SOURCE_BYTES,
        ),
    ):
        _validate_limit(name, value, maximum)

    report = inspect_renpy_compatibility(project_path)
    if report.status is not RenpyCompatibilityStatus.PACKAGED_REQUIRES_IMPORT:
        raise RenpyPackagedImportError(
            "只接受兼容性报告明确识别的 packaged_requires_import 成品结构"
        )
    if not report.archives:
        raise RenpyPackagedImportError(
            "成品只包含编译脚本; V0.4.4 不反编译 .rpyc/.rpymc"
        )
    if len(report.archives) > max_archives:
        raise RenpyPackagedImportError(
            f"RPA 归档数量超过安全上限 {max_archives}"
        )

    project_root = report.project_root
    expanded_output = output_path.expanduser()
    if expanded_output.is_symlink():
        raise RenpyPackagedImportError(f"输出目录不能是符号链接: {expanded_output}")
    output_root = expanded_output.resolve()
    if output_root.exists():
        raise FileExistsError(f"输出目录已存在, 拒绝覆盖: {output_root}")
    if _roots_overlap(project_root, output_root):
        raise RenpyPackagedImportError(
            f"导入输出不得与原游戏目录重叠: {output_root} / {project_root}"
        )

    snapshots: list[_ArchiveSnapshot] = []
    remaining_archive_bytes = max_total_archive_bytes
    remaining_source_bytes = max_total_source_bytes
    for relative_path in report.archives:
        archive_relative = PurePosixPath(relative_path)
        archive_path = project_root.joinpath(*archive_relative.parts)
        snapshot = _read_archive_snapshot(
            archive_path,
            relative_path=relative_path,
            max_index_bytes=max_index_bytes,
            max_archive_entries=max_archive_entries,
            max_archive_bytes=max_archive_bytes,
            max_source_file_bytes=max_source_file_bytes,
            remaining_archive_bytes=remaining_archive_bytes,
            remaining_source_bytes=remaining_source_bytes,
        )
        snapshots.append(snapshot)
        remaining_archive_bytes -= snapshot.archive.size_bytes
        remaining_source_bytes -= sum(
            source.record.size_bytes for source in snapshot.sources
        )

    ordered_sources = tuple(
        sorted(
            (source for snapshot in snapshots for source in snapshot.sources),
            key=lambda source: (
                source.record.relative_path.casefold(),
                source.record.relative_path,
            ),
        )
    )
    if not ordered_sources:
        raise RenpyPackagedImportError(
            "普通 RPA 中没有可导入的 .rpy/.rpym; V0.4.4 不反编译编译脚本"
        )
    seen_output_paths: dict[str, str] = {}
    for source in ordered_sources:
        collision_key = unicodedata.normalize("NFC", source.record.relative_path).casefold()
        previous = seen_output_paths.get(collision_key)
        if previous is not None:
            raise RenpyPackagedImportError(
                "多个 RPA 源码条目会写入同一 Windows 路径: "
                f"{previous} / {source.record.relative_path}"
            )
        seen_output_paths[collision_key] = source.record.relative_path

    archive_records = tuple(snapshot.archive for snapshot in snapshots)
    source_records = tuple(source.record for source in ordered_sources)
    ignored_compiled = tuple(
        sorted(
            (
                *report.compiled_scripts,
                *(
                    item
                    for snapshot in snapshots
                    for item in snapshot.ignored_compiled_scripts
                ),
            ),
            key=lambda item: (item.casefold(), item),
        )
    )
    ignored_translations = tuple(
        sorted(
            (
                *report.translation_files,
                *(
                    item
                    for snapshot in snapshots
                    for item in snapshot.ignored_translation_files
                ),
            ),
            key=lambda item: (item.casefold(), item),
        )
    )
    manifest = RenpyPackagedImportManifest(
        schema_version=RENPY_PACKAGED_IMPORT_MANIFEST_SCHEMA_VERSION,
        import_id=_import_id(archive_records, source_records),
        importer=_IMPORTER_ID,
        authorization=authorization,
        source_project_root=project_root,
        compatibility_report_schema_version=RENPY_COMPATIBILITY_REPORT_SCHEMA_VERSION,
        archives=archive_records,
        source_files=source_records,
        ignored_compiled_scripts=ignored_compiled,
        ignored_translation_files=ignored_translations,
    )
    manifest_bytes = (
        json.dumps(
            manifest.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")

    output_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=".galtrans-renpy-import-", dir=output_root.parent)
    )
    published = False
    try:
        for planned in ordered_sources:
            relative = PurePosixPath(planned.record.relative_path)
            destination = staging_root.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(planned.content)
        manifest_path = staging_root / _MANIFEST_NAME
        with manifest_path.open("xb") as stream:
            stream.write(manifest_bytes)
        _verify_archive_snapshots(tuple(snapshots))
        if output_root.exists() or output_root.is_symlink():
            raise FileExistsError(f"输出目录已存在, 拒绝覆盖: {output_root}")
        staging_root.rename(output_root)
        published = True
    finally:
        if not published and staging_root.exists():
            shutil.rmtree(staging_root)

    return RenpyPackagedImportResult(
        root=output_root,
        manifest_path=output_root / _MANIFEST_NAME,
        source_files=tuple(
            output_root.joinpath(*PurePosixPath(source.relative_path).parts)
            for source in source_records
        ),
        manifest=manifest,
    )
