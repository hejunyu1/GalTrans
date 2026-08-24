from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from galtrans.translation import (
    ProviderRequestReceipt,
    ProviderRequestStatus,
    TranslationBatch,
    TranslationBatchStatus,
    TranslationProposal,
    TranslationSchemaError,
    TranslationStateError,
    TranslationTask,
    TranslationTaskCheckpoint,
    TranslationTaskStatus,
    TranslationValidationError,
    ValidatedTranslation,
    complete_translation_batch,
    new_translation_checkpoint,
    start_translation_batch,
    translation_checkpoint_from_dict,
    translation_proposal_id,
    translation_request_id,
)


TRANSLATION_STORAGE_SCHEMA_VERSION = 3
_APPLICATION_ID = 0x4754524E  # ASCII "GTRN".


class TranslationStorageError(RuntimeError):
    """Raised when durable translation state cannot be trusted or committed."""


@dataclass(frozen=True, slots=True)
class StoredTranslationTask:
    task: TranslationTask
    checkpoint: TranslationTaskCheckpoint


ProposalValidator = Callable[
    [TranslationTask, TranslationProposal],
    ValidatedTranslation,
]


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_object(value: str, *, location: str) -> Mapping[str, Any]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise TranslationStorageError(f"{location} 不是有效 JSON：{error}") from error
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        raise TranslationStorageError(f"{location} 必须是字符串键对象")
    return decoded


def _json_array(value: str, *, location: str) -> list[Any]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise TranslationStorageError(f"{location} 不是有效 JSON：{error}") from error
    if not isinstance(decoded, list):
        raise TranslationStorageError(f"{location} 必须是数组")
    return decoded


def _validated_task(task: TranslationTask) -> TranslationTask:
    try:
        restored = TranslationTask.from_dict(task.to_dict())
    except TranslationSchemaError as error:
        raise TranslationStorageError(f"翻译任务不能持久化：{error}") from error
    if restored != task:
        raise TranslationStorageError("翻译任务序列化往返不一致")
    return restored


def _validated_task_batch(
    task: TranslationTask,
    batch: TranslationBatch,
) -> TranslationBatch:
    expected = next(
        (item for item in task.batches if item.batch_id == batch.batch_id),
        None,
    )
    if expected is None or expected != batch:
        raise TranslationStorageError("缓存批次不属于当前翻译任务或内容不一致")
    return expected


def _validated_cached_proposals(
    task: TranslationTask,
    batch: TranslationBatch,
    proposals: Iterable[TranslationProposal],
    proposal_validator: ProposalValidator,
) -> tuple[TranslationProposal, ...]:
    proposal_items = tuple(proposals)
    validated_items: list[ValidatedTranslation] = []
    by_segment: dict[str, TranslationProposal] = {}
    try:
        for proposal in proposal_items:
            if not isinstance(proposal, TranslationProposal):
                raise TranslationSchemaError("缓存结果必须只包含 TranslationProposal")
            validated = proposal_validator(task, proposal)
            if validated.proposal_id != translation_proposal_id(proposal):
                raise TranslationValidationError("缓存提案验证结果的内容摘要不一致")
            if proposal.segment_id in by_segment:
                raise TranslationValidationError("缓存结果包含重复文本段")
            by_segment[proposal.segment_id] = proposal
            validated_items.append(validated)

        initial = new_translation_checkpoint(task)
        started = start_translation_batch(task, initial, batch.batch_id)
        complete_translation_batch(
            task,
            started,
            batch.batch_id,
            validated_items,
        )
    except (TranslationSchemaError, TranslationStateError, TranslationValidationError) as error:
        raise TranslationStorageError(
            f"缓存结果不完整或验证失败：{error}"
        ) from error

    return tuple(by_segment[segment.segment_id] for segment in batch.segments)


def _validated_provider_receipt(
    batch: TranslationBatch,
    backend_identity: str,
    receipt: ProviderRequestReceipt,
) -> ProviderRequestReceipt:
    if not isinstance(receipt, ProviderRequestReceipt):
        raise TranslationStorageError("Provider 回执类型无效")
    try:
        restored = ProviderRequestReceipt.from_dict(receipt.to_dict())
    except TranslationSchemaError as error:
        raise TranslationStorageError(f"Provider 回执不能持久化：{error}") from error
    if restored != receipt:
        raise TranslationStorageError("Provider 回执序列化往返不一致")
    request_id = translation_request_id(batch, backend_identity)
    if receipt.request_id != request_id:
        raise TranslationStorageError("Provider 回执的幂等键与任务批次不一致")
    return restored


def _provider_receipt_transition_allowed(
    previous: ProviderRequestReceipt,
    updated: ProviderRequestReceipt,
    *,
    allow_retry: bool,
) -> bool:
    if previous == updated:
        return True
    if previous.status is ProviderRequestStatus.SUCCEEDED:
        return False
    if previous.status is ProviderRequestStatus.FAILED:
        return allow_retry and updated.status is ProviderRequestStatus.UNKNOWN
    return True


def _validated_checkpoint(
    task: TranslationTask,
    checkpoint: TranslationTaskCheckpoint,
) -> TranslationTaskCheckpoint:
    try:
        restored = translation_checkpoint_from_dict(task, checkpoint.to_dict())
    except TranslationSchemaError as error:
        raise TranslationStorageError(f"翻译检查点不能持久化：{error}") from error
    if restored != checkpoint:
        raise TranslationStorageError("翻译检查点序列化往返不一致")
    return restored


def _proposal_references(checkpoint: TranslationTaskCheckpoint) -> tuple[str, ...]:
    return tuple(
        proposal_id
        for batch in checkpoint.batches
        for proposal_id in batch.validated_proposal_ids
    )


def _validate_transition(
    previous: TranslationTaskCheckpoint,
    updated: TranslationTaskCheckpoint,
) -> None:
    task_transitions = {
        TranslationTaskStatus.PENDING: {
            TranslationTaskStatus.PENDING,
            TranslationTaskStatus.RUNNING,
            TranslationTaskStatus.PAUSED,
        },
        TranslationTaskStatus.RUNNING: {
            TranslationTaskStatus.RUNNING,
            TranslationTaskStatus.PAUSED,
            TranslationTaskStatus.FAILED,
            TranslationTaskStatus.COMPLETED,
        },
        TranslationTaskStatus.PAUSED: {
            TranslationTaskStatus.PAUSED,
            TranslationTaskStatus.RUNNING,
        },
        TranslationTaskStatus.FAILED: {
            TranslationTaskStatus.FAILED,
            TranslationTaskStatus.RUNNING,
        },
        TranslationTaskStatus.COMPLETED: {TranslationTaskStatus.COMPLETED},
    }
    if updated.status not in task_transitions[previous.status]:
        raise TranslationStorageError(
            f"任务状态不能从 {previous.status.value} 转为 {updated.status.value}"
        )

    batch_transitions = {
        TranslationBatchStatus.PENDING: {
            TranslationBatchStatus.PENDING,
            TranslationBatchStatus.RUNNING,
        },
        TranslationBatchStatus.RUNNING: {
            TranslationBatchStatus.RUNNING,
            TranslationBatchStatus.PENDING,
            TranslationBatchStatus.FAILED,
            TranslationBatchStatus.COMPLETED,
        },
        TranslationBatchStatus.FAILED: {
            TranslationBatchStatus.FAILED,
            TranslationBatchStatus.PENDING,
        },
        TranslationBatchStatus.COMPLETED: {TranslationBatchStatus.COMPLETED},
    }
    for before, after in zip(previous.batches, updated.batches, strict=True):
        if after.status not in batch_transitions[before.status]:
            raise TranslationStorageError(
                f"批次 {before.batch_id} 不能从 {before.status.value} 转为 {after.status.value}"
            )
        if after.attempts < before.attempts:
            raise TranslationStorageError(f"批次 {before.batch_id} 的尝试次数不能减少")
        starts_attempt = (
            before.status is TranslationBatchStatus.PENDING
            and after.status is TranslationBatchStatus.RUNNING
        )
        expected_attempts = before.attempts + 1 if starts_attempt else before.attempts
        if after.attempts != expected_attempts:
            raise TranslationStorageError(
                f"批次 {before.batch_id} 的尝试次数与状态转换不一致"
            )
        if before.status is TranslationBatchStatus.COMPLETED and after != before:
            raise TranslationStorageError(f"已完成批次 {before.batch_id} 不能再改变")

    previous_references = _proposal_references(previous)
    updated_references = _proposal_references(updated)
    if len(set(updated_references)) != len(updated_references):
        raise TranslationStorageError("检查点包含重复的已验证提案 ID")
    if not set(previous_references).issubset(updated_references):
        raise TranslationStorageError("检查点不能移除已经接受的提案")


class TranslationStore:
    """Own atomic task checkpoints and accepted proposal bodies in one SQLite file."""

    def __init__(self, database_path: Path, *, input_project_root: Path) -> None:
        project_root = input_project_root.expanduser().resolve()
        if not project_root.is_dir():
            raise TranslationStorageError(f"输入项目目录不存在：{project_root}")

        requested_path = database_path.expanduser()
        if requested_path.is_symlink():
            raise TranslationStorageError(f"翻译数据库不能是符号链接：{requested_path}")
        resolved_path = requested_path.resolve()
        if resolved_path == project_root or resolved_path.is_relative_to(project_root):
            raise TranslationStorageError(
                f"翻译数据库不得位于只读输入项目中：{resolved_path}"
            )
        if not resolved_path.parent.is_dir():
            raise TranslationStorageError(
                f"翻译数据库父目录不存在：{resolved_path.parent}"
            )
        if resolved_path.exists() and not resolved_path.is_file():
            raise TranslationStorageError(f"翻译数据库路径不是文件：{resolved_path}")

        self.database_path = resolved_path
        self.input_project_root = project_root
        new_database = not resolved_path.exists()
        try:
            self._connection = sqlite3.connect(resolved_path, timeout=5.0)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA foreign_keys = ON")
            if new_database:
                self._initialize_schema()
            else:
                self._validate_schema()
        except sqlite3.Error as error:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise TranslationStorageError(f"无法打开翻译数据库：{error}") from error
        except Exception:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise
        self._closed = False

    def __enter__(self) -> TranslationStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self._closed:
            raise TranslationStorageError("翻译数据库已经关闭")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            yield
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _initialize_schema(self) -> None:
        self._closed = False
        with self._transaction():
            self._connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            self._connection.execute(
                f"PRAGMA user_version = {TRANSLATION_STORAGE_SCHEMA_VERSION}"
            )
            self._connection.execute(
                """
                CREATE TABLE translation_tasks (
                    task_id TEXT PRIMARY KEY,
                    task_json TEXT NOT NULL,
                    checkpoint_json TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE translation_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    segment_id TEXT NOT NULL,
                    proposal_json TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES translation_tasks(task_id)
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX translation_proposals_task
                ON translation_proposals(task_id, batch_id, segment_id)
                """
            )
            self._connection.execute(
                """
                CREATE TABLE translation_request_cache (
                    request_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    backend_identity TEXT NOT NULL,
                    batch_json TEXT NOT NULL,
                    proposals_json TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES translation_tasks(task_id)
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE translation_provider_requests (
                    request_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    backend_identity TEXT NOT NULL,
                    batch_json TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES translation_tasks(task_id)
                )
                """
            )

    def _validate_schema(self) -> None:
        application_id = self._connection.execute("PRAGMA application_id").fetchone()[0]
        if application_id != _APPLICATION_ID:
            raise TranslationStorageError(
                f"现有文件不是 GalTrans 翻译数据库：{self.database_path}"
            )
        user_version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if user_version != TRANSLATION_STORAGE_SCHEMA_VERSION:
            raise TranslationStorageError(
                f"不支持的翻译数据库 schema 版本：{user_version}"
            )
        tables = {
            row[0]
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if not row[0].startswith("sqlite_")
        }
        expected = {
            "translation_tasks",
            "translation_proposals",
            "translation_request_cache",
            "translation_provider_requests",
        }
        if tables != expected:
            raise TranslationStorageError("GalTrans 翻译数据库表结构不完整或包含未知表")
        expected_columns = {
            "translation_tasks": ("task_id", "task_json", "checkpoint_json"),
            "translation_proposals": (
                "proposal_id",
                "task_id",
                "batch_id",
                "segment_id",
                "proposal_json",
            ),
            "translation_request_cache": (
                "request_id",
                "task_id",
                "batch_id",
                "backend_identity",
                "batch_json",
                "proposals_json",
            ),
            "translation_provider_requests": (
                "request_id",
                "task_id",
                "batch_id",
                "backend_identity",
                "batch_json",
                "receipt_json",
            ),
        }
        for table, columns in expected_columns.items():
            actual = tuple(
                row[1]
                for row in self._connection.execute(f"PRAGMA table_info({table})")
            )
            if actual != columns:
                raise TranslationStorageError(
                    f"GalTrans 翻译数据库表字段不兼容：{table}"
                )

    def initialize_task(self, task: TranslationTask) -> TranslationTaskCheckpoint:
        checked_task = _validated_task(task)
        initial = new_translation_checkpoint(checked_task)
        task_json = _canonical_json(checked_task.to_dict())
        checkpoint_json = _canonical_json(initial.to_dict())
        with self._transaction():
            row = self._connection.execute(
                "SELECT task_json, checkpoint_json FROM translation_tasks WHERE task_id = ?",
                (checked_task.task_id,),
            ).fetchone()
            if row is None:
                self._connection.execute(
                    """
                    INSERT INTO translation_tasks(task_id, task_json, checkpoint_json)
                    VALUES (?, ?, ?)
                    """,
                    (checked_task.task_id, task_json, checkpoint_json),
                )
                return initial
            if row["task_json"] != task_json:
                raise TranslationStorageError(
                    f"任务 ID 对应不同的任务规范：{checked_task.task_id}"
                )
            return self._decode_stored_task(
                checked_task.task_id,
                row["task_json"],
                row["checkpoint_json"],
            ).checkpoint

    def _decode_stored_task(
        self,
        task_id: str,
        task_json: str,
        checkpoint_json: str,
    ) -> StoredTranslationTask:
        try:
            task = TranslationTask.from_dict(
                _json_object(task_json, location=f"任务 {task_id}")
            )
            checkpoint = translation_checkpoint_from_dict(
                task,
                _json_object(checkpoint_json, location=f"任务 {task_id} 的检查点"),
            )
        except TranslationSchemaError as error:
            raise TranslationStorageError(f"数据库中的翻译任务损坏：{error}") from error
        if task.task_id != task_id:
            raise TranslationStorageError("数据库任务主键与任务内容不一致")
        return StoredTranslationTask(task=task, checkpoint=checkpoint)

    def load_task(self, task_id: str) -> StoredTranslationTask:
        if self._closed:
            raise TranslationStorageError("翻译数据库已经关闭")
        row = self._connection.execute(
            "SELECT task_json, checkpoint_json FROM translation_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise TranslationStorageError(f"翻译任务不存在：{task_id}")
        return self._decode_stored_task(task_id, row["task_json"], row["checkpoint_json"])

    def commit_checkpoint(
        self,
        task: TranslationTask,
        previous: TranslationTaskCheckpoint,
        updated: TranslationTaskCheckpoint,
        *,
        proposals: Iterable[TranslationProposal] = (),
        proposal_validator: ProposalValidator | None = None,
    ) -> None:
        checked_task = _validated_task(task)
        checked_previous = _validated_checkpoint(checked_task, previous)
        checked_updated = _validated_checkpoint(checked_task, updated)
        _validate_transition(checked_previous, checked_updated)

        proposal_items = tuple(proposals)
        if proposal_items and proposal_validator is None:
            raise TranslationStorageError("提交提案时必须提供确定性提案验证器")
        validated_items: dict[str, tuple[TranslationProposal, ValidatedTranslation]] = {}
        for proposal in proposal_items:
            if proposal_validator is None:
                raise TranslationStorageError("提交提案时必须提供确定性提案验证器")
            try:
                validated = proposal_validator(checked_task, proposal)
            except (TranslationSchemaError, TranslationValidationError) as error:
                raise TranslationStorageError(f"提案验证失败：{error}") from error
            proposal_id = translation_proposal_id(proposal)
            if validated.proposal_id != proposal_id:
                raise TranslationStorageError("提案验证结果的内容摘要不一致")
            if proposal_id in validated_items:
                raise TranslationStorageError(f"一次提交包含重复提案：{proposal_id}")
            validated_items[proposal_id] = (proposal, validated)

        previous_references = set(_proposal_references(checked_previous))
        updated_references = set(_proposal_references(checked_updated))
        new_references = updated_references - previous_references
        if set(validated_items) != new_references:
            raise TranslationStorageError(
                "检查点新增提案 ID 必须与本次经过验证的提案正文完全一致"
            )

        task_json = _canonical_json(checked_task.to_dict())
        previous_json = _canonical_json(checked_previous.to_dict())
        updated_json = _canonical_json(checked_updated.to_dict())
        with self._transaction():
            row = self._connection.execute(
                "SELECT task_json, checkpoint_json FROM translation_tasks WHERE task_id = ?",
                (checked_task.task_id,),
            ).fetchone()
            if row is None:
                raise TranslationStorageError(f"翻译任务尚未初始化：{checked_task.task_id}")
            if row["task_json"] != task_json:
                raise TranslationStorageError("数据库任务规范与当前任务不一致")
            if row["checkpoint_json"] != previous_json:
                raise TranslationStorageError("翻译检查点已被其他执行更新，拒绝覆盖较新状态")

            for proposal_id, (proposal, validated) in validated_items.items():
                proposal_json = _canonical_json(proposal.to_dict())
                existing = self._connection.execute(
                    """
                    SELECT task_id, batch_id, segment_id, proposal_json
                    FROM translation_proposals WHERE proposal_id = ?
                    """,
                    (proposal_id,),
                ).fetchone()
                values = (
                    validated.task_id,
                    validated.batch_id,
                    validated.segment_id,
                    proposal_json,
                )
                if existing is None:
                    self._connection.execute(
                        """
                        INSERT INTO translation_proposals(
                            proposal_id, task_id, batch_id, segment_id, proposal_json
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (proposal_id, *values),
                    )
                elif tuple(existing) != values:
                    raise TranslationStorageError(
                        f"提案 ID 对应不同的持久化内容：{proposal_id}"
                    )

            self._verify_proposal_references(checked_task, checked_updated)
            cursor = self._connection.execute(
                """
                UPDATE translation_tasks SET checkpoint_json = ?
                WHERE task_id = ? AND checkpoint_json = ?
                """,
                (updated_json, checked_task.task_id, previous_json),
            )
            if cursor.rowcount != 1:
                raise TranslationStorageError("翻译检查点并发更新失败")

    def _verify_proposal_references(
        self,
        task: TranslationTask,
        checkpoint: TranslationTaskCheckpoint,
    ) -> None:
        for task_batch, batch_checkpoint in zip(
            task.batches,
            checkpoint.batches,
            strict=True,
        ):
            if not batch_checkpoint.validated_proposal_ids:
                continue
            if len(batch_checkpoint.validated_proposal_ids) != len(task_batch.segments):
                raise TranslationStorageError(
                    f"批次 {task_batch.batch_id} 的提案引用数量与文本段不一致"
                )
            for segment, proposal_id in zip(
                task_batch.segments,
                batch_checkpoint.validated_proposal_ids,
                strict=True,
            ):
                row = self._connection.execute(
                    """
                    SELECT task_id, batch_id, segment_id, proposal_json
                    FROM translation_proposals WHERE proposal_id = ?
                    """,
                    (proposal_id,),
                ).fetchone()
                if row is None:
                    raise TranslationStorageError(f"检查点引用缺失的提案：{proposal_id}")
                try:
                    proposal = TranslationProposal.from_dict(
                        _json_object(row["proposal_json"], location=f"提案 {proposal_id}")
                    )
                except TranslationSchemaError as error:
                    raise TranslationStorageError(
                        f"数据库中的翻译提案损坏：{error}"
                    ) from error
                if translation_proposal_id(proposal) != proposal_id:
                    raise TranslationStorageError(f"提案内容摘要不一致：{proposal_id}")
                if (
                    row["task_id"] != task.task_id
                    or proposal.task_id != task.task_id
                    or row["batch_id"] != task_batch.batch_id
                    or proposal.batch_id != task_batch.batch_id
                    or row["segment_id"] != segment.segment_id
                    or proposal.segment_id != segment.segment_id
                    or proposal.source_schema_version != segment.source_schema_version
                    or proposal.source_sha256 != segment.source_sha256
                    or proposal.target_language != task.target_language
                    or proposal.protected_tokens != segment.protected_tokens
                ):
                    raise TranslationStorageError(
                        f"数据库提案与任务来源记录不一致：{proposal_id}"
                    )

    def store_cached_proposals(
        self,
        task: TranslationTask,
        batch: TranslationBatch,
        backend_identity: str,
        proposals: Iterable[TranslationProposal],
        proposal_validator: ProposalValidator,
    ) -> str:
        checked_task = _validated_task(task)
        checked_batch = _validated_task_batch(checked_task, batch)
        request_id = translation_request_id(checked_batch, backend_identity)
        checked_proposals = _validated_cached_proposals(
            checked_task,
            checked_batch,
            proposals,
            proposal_validator,
        )
        task_json = _canonical_json(checked_task.to_dict())
        batch_json = _canonical_json(checked_batch.to_dict())
        proposals_json = json.dumps(
            [proposal.to_dict() for proposal in checked_proposals],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        values = (
            checked_task.task_id,
            checked_batch.batch_id,
            backend_identity,
            batch_json,
            proposals_json,
        )
        with self._transaction():
            task_row = self._connection.execute(
                "SELECT task_json FROM translation_tasks WHERE task_id = ?",
                (checked_task.task_id,),
            ).fetchone()
            if task_row is None:
                raise TranslationStorageError(
                    f"翻译任务尚未初始化：{checked_task.task_id}"
                )
            if task_row["task_json"] != task_json:
                raise TranslationStorageError("数据库任务规范与缓存任务不一致")

            existing = self._connection.execute(
                """
                SELECT task_id, batch_id, backend_identity, batch_json, proposals_json
                FROM translation_request_cache WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if existing is None:
                self._connection.execute(
                    """
                    INSERT INTO translation_request_cache(
                        request_id, task_id, batch_id, backend_identity,
                        batch_json, proposals_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (request_id, *values),
                )
            elif tuple(existing) != values:
                raise TranslationStorageError(
                    f"请求 {request_id} 已对应不同的缓存结果"
                )
        return request_id

    def load_cached_proposals(
        self,
        task: TranslationTask,
        batch: TranslationBatch,
        backend_identity: str,
        proposal_validator: ProposalValidator,
    ) -> tuple[TranslationProposal, ...] | None:
        if self._closed:
            raise TranslationStorageError("翻译数据库已经关闭")
        checked_task = _validated_task(task)
        checked_batch = _validated_task_batch(checked_task, batch)
        request_id = translation_request_id(checked_batch, backend_identity)
        row = self._connection.execute(
            """
            SELECT task_id, batch_id, backend_identity, batch_json, proposals_json
            FROM translation_request_cache WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        batch_json = _canonical_json(checked_batch.to_dict())
        if (
            row["task_id"] != checked_task.task_id
            or row["batch_id"] != checked_batch.batch_id
            or row["backend_identity"] != backend_identity
            or row["batch_json"] != batch_json
        ):
            raise TranslationStorageError(
                f"请求缓存与任务、批次或 backend identity 不一致：{request_id}"
            )

        raw_proposals = _json_array(
            row["proposals_json"],
            location=f"请求缓存 {request_id} 的提案",
        )
        proposals: list[TranslationProposal] = []
        try:
            for index, raw_proposal in enumerate(raw_proposals):
                if not isinstance(raw_proposal, dict) or any(
                    not isinstance(key, str) for key in raw_proposal
                ):
                    raise TranslationSchemaError(
                        f"请求缓存 {request_id} 的提案[{index}] 必须是字符串键对象"
                    )
                proposals.append(TranslationProposal.from_dict(raw_proposal))
        except TranslationSchemaError as error:
            raise TranslationStorageError(f"请求缓存提案 schema 损坏：{error}") from error
        return _validated_cached_proposals(
            checked_task,
            checked_batch,
            proposals,
            proposal_validator,
        )

    def store_provider_receipt(
        self,
        task: TranslationTask,
        batch: TranslationBatch,
        backend_identity: str,
        receipt: ProviderRequestReceipt,
        *,
        expected_receipt: ProviderRequestReceipt | None = None,
        allow_retry: bool = False,
    ) -> ProviderRequestReceipt:
        checked_task = _validated_task(task)
        checked_batch = _validated_task_batch(checked_task, batch)
        checked_receipt = _validated_provider_receipt(
            checked_batch,
            backend_identity,
            receipt,
        )
        checked_expected = (
            None
            if expected_receipt is None
            else _validated_provider_receipt(
                checked_batch,
                backend_identity,
                expected_receipt,
            )
        )
        request_id = translation_request_id(checked_batch, backend_identity)
        task_json = _canonical_json(checked_task.to_dict())
        batch_json = _canonical_json(checked_batch.to_dict())
        receipt_json = _canonical_json(checked_receipt.to_dict())

        with self._transaction():
            task_row = self._connection.execute(
                "SELECT task_json FROM translation_tasks WHERE task_id = ?",
                (checked_task.task_id,),
            ).fetchone()
            if task_row is None:
                raise TranslationStorageError(
                    f"翻译任务尚未初始化：{checked_task.task_id}"
                )
            if task_row["task_json"] != task_json:
                raise TranslationStorageError("数据库任务规范与 Provider 请求不一致")

            row = self._connection.execute(
                """
                SELECT task_id, batch_id, backend_identity, batch_json, receipt_json
                FROM translation_provider_requests WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if row is None:
                if checked_expected is not None:
                    raise TranslationStorageError("Provider 请求回执不存在，不能比较更新")
                self._connection.execute(
                    """
                    INSERT INTO translation_provider_requests(
                        request_id, task_id, batch_id, backend_identity,
                        batch_json, receipt_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        checked_task.task_id,
                        checked_batch.batch_id,
                        backend_identity,
                        batch_json,
                        receipt_json,
                    ),
                )
                return checked_receipt

            if (
                row["task_id"] != checked_task.task_id
                or row["batch_id"] != checked_batch.batch_id
                or row["backend_identity"] != backend_identity
                or row["batch_json"] != batch_json
            ):
                raise TranslationStorageError(
                    f"Provider 请求与任务、批次或 backend identity 不一致：{request_id}"
                )
            try:
                current_raw = ProviderRequestReceipt.from_dict(
                    _json_object(
                        row["receipt_json"],
                        location=f"Provider 请求 {request_id} 的回执",
                    )
                )
            except TranslationSchemaError as error:
                raise TranslationStorageError(
                    f"Provider 请求回执 schema 损坏：{error}"
                ) from error
            current = _validated_provider_receipt(
                checked_batch,
                backend_identity,
                current_raw,
            )
            if checked_expected is None or current != checked_expected:
                raise TranslationStorageError("Provider 请求回执已被其他执行者更新")
            if (
                current.provider_request_id is not None
                and checked_receipt.provider_request_id != current.provider_request_id
            ):
                raise TranslationStorageError("Provider 请求引用不能被移除或替换")
            if not _provider_receipt_transition_allowed(
                current,
                checked_receipt,
                allow_retry=allow_retry,
            ):
                raise TranslationStorageError(
                    f"Provider 请求不能从 {current.status.value} 转为 "
                    f"{checked_receipt.status.value}"
                )
            self._connection.execute(
                """
                UPDATE translation_provider_requests SET receipt_json = ?
                WHERE request_id = ?
                """,
                (receipt_json, request_id),
            )
        return checked_receipt

    def load_provider_receipt(
        self,
        task: TranslationTask,
        batch: TranslationBatch,
        backend_identity: str,
    ) -> ProviderRequestReceipt | None:
        checked_task = _validated_task(task)
        checked_batch = _validated_task_batch(checked_task, batch)
        request_id = translation_request_id(checked_batch, backend_identity)
        row = self._connection.execute(
            """
            SELECT task_id, batch_id, backend_identity, batch_json, receipt_json
            FROM translation_provider_requests WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        if (
            row["task_id"] != checked_task.task_id
            or row["batch_id"] != checked_batch.batch_id
            or row["backend_identity"] != backend_identity
            or row["batch_json"] != _canonical_json(checked_batch.to_dict())
        ):
            raise TranslationStorageError(
                f"Provider 请求与任务、批次或 backend identity 不一致：{request_id}"
            )
        try:
            receipt = ProviderRequestReceipt.from_dict(
                _json_object(
                    row["receipt_json"],
                    location=f"Provider 请求 {request_id} 的回执",
                )
            )
        except TranslationSchemaError as error:
            raise TranslationStorageError(
                f"Provider 请求回执 schema 损坏：{error}"
            ) from error
        return _validated_provider_receipt(
            checked_batch,
            backend_identity,
            receipt,
        )

    def load_accepted_proposals(self, task_id: str) -> tuple[TranslationProposal, ...]:
        stored = self.load_task(task_id)
        self._verify_proposal_references(stored.task, stored.checkpoint)
        proposals: list[TranslationProposal] = []
        for proposal_id in _proposal_references(stored.checkpoint):
            row = self._connection.execute(
                "SELECT proposal_json FROM translation_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise TranslationStorageError(f"检查点引用缺失的提案：{proposal_id}")
            try:
                proposal = TranslationProposal.from_dict(
                    _json_object(row["proposal_json"], location=f"提案 {proposal_id}")
                )
            except TranslationSchemaError as error:
                raise TranslationStorageError(f"数据库中的翻译提案损坏：{error}") from error
            if translation_proposal_id(proposal) != proposal_id:
                raise TranslationStorageError(f"提案内容摘要不一致：{proposal_id}")
            proposals.append(proposal)
        return tuple(proposals)
