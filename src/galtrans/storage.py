from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from galtrans.translation import (
    TranslationBatchStatus,
    TranslationProposal,
    TranslationSchemaError,
    TranslationTask,
    TranslationTaskCheckpoint,
    TranslationTaskStatus,
    TranslationValidationError,
    ValidatedTranslation,
    new_translation_checkpoint,
    translation_checkpoint_from_dict,
    translation_proposal_id,
)


TRANSLATION_STORAGE_SCHEMA_VERSION = 1
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


def _validated_task(task: TranslationTask) -> TranslationTask:
    try:
        restored = TranslationTask.from_dict(task.to_dict())
    except TranslationSchemaError as error:
        raise TranslationStorageError(f"翻译任务不能持久化：{error}") from error
    if restored != task:
        raise TranslationStorageError("翻译任务序列化往返不一致")
    return restored


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
        expected = {"translation_tasks", "translation_proposals"}
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
