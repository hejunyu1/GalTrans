from __future__ import annotations

from galtrans.storage import ProposalValidator, TranslationStorageError, TranslationStore
from galtrans.translation import (
    TranslationBackend,
    TranslationBatchStatus,
    TranslationProposal,
    TranslationTask,
    TranslationTaskCheckpoint,
    TranslationTaskStatus,
    complete_translation_batch,
    fail_translation_batch,
    pause_translation_task,
    recover_interrupted_translation_task,
    resume_translation_task,
    start_translation_batch,
    translation_request_id,
)


class TranslationExecutionError(RuntimeError):
    """Raised when a persisted translation batch cannot be safely executed."""


def _failure_message(error: Exception) -> str:
    detail = str(error).strip()
    message = type(error).__name__ if not detail else f"{type(error).__name__}: {detail}"
    return message[:1000]


class TranslationTaskRunner:
    """Execute at most one persisted batch through a filtered backend boundary."""

    def __init__(
        self,
        store: TranslationStore,
        backend: TranslationBackend,
        proposal_validator: ProposalValidator,
        *,
        backend_identity: str,
    ) -> None:
        self._store = store
        self._backend = backend
        self._proposal_validator = proposal_validator
        self._backend_identity = backend_identity

    def run_next_batch(self, task_id: str) -> TranslationTaskCheckpoint:
        """Run the first pending batch and atomically persist its accepted proposals."""
        stored = self._store.load_task(task_id)
        task = stored.task
        checkpoint = stored.checkpoint
        if checkpoint.status is TranslationTaskStatus.COMPLETED:
            return checkpoint
        if checkpoint.status in {
            TranslationTaskStatus.PAUSED,
            TranslationTaskStatus.FAILED,
        }:
            raise TranslationExecutionError(
                f"任务处于 {checkpoint.status.value}，必须显式恢复后才能执行"
            )
        running = tuple(
            item
            for item in checkpoint.batches
            if item.status is TranslationBatchStatus.RUNNING
        )
        if running:
            raise TranslationExecutionError(
                f"批次 {running[0].batch_id} 仍为 running；请先显式恢复中断任务"
            )
        pending = next(
            (
                item
                for item in checkpoint.batches
                if item.status is TranslationBatchStatus.PENDING
            ),
            None,
        )
        if pending is None:
            raise TranslationExecutionError("任务没有可执行批次，但尚未处于 completed")

        batch = next(item for item in task.batches if item.batch_id == pending.batch_id)
        translation_request_id(batch, self._backend_identity)
        started = start_translation_batch(task, checkpoint, batch.batch_id)
        self._store.commit_checkpoint(task, checkpoint, started)

        try:
            proposals = self._store.load_cached_proposals(
                task,
                batch,
                self._backend_identity,
                self._proposal_validator,
            )
            if proposals is None:
                proposals = self._backend.propose(batch)
                if not isinstance(proposals, tuple) or any(
                    not isinstance(item, TranslationProposal) for item in proposals
                ):
                    raise TypeError("翻译后端必须返回 TranslationProposal 元组")
            validated = tuple(
                self._proposal_validator(task, proposal) for proposal in proposals
            )
            completed = complete_translation_batch(
                task,
                started,
                batch.batch_id,
                validated,
            )
            self._store.store_cached_proposals(
                task,
                batch,
                self._backend_identity,
                proposals,
                self._proposal_validator,
            )
        except Exception as error:
            self._persist_failure(task, started, batch.batch_id, error)
            raise TranslationExecutionError(
                f"批次 {batch.batch_id} 执行失败：{_failure_message(error)}"
            ) from error

        self._store.commit_checkpoint(
            task,
            started,
            completed,
            proposals=proposals,
            proposal_validator=self._proposal_validator,
        )
        return completed

    def _persist_failure(
        self,
        task: TranslationTask,
        started: TranslationTaskCheckpoint,
        batch_id: str,
        error: Exception,
    ) -> None:
        failed = fail_translation_batch(
            task,
            started,
            batch_id,
            _failure_message(error),
        )
        try:
            self._store.commit_checkpoint(task, started, failed)
        except TranslationStorageError as storage_error:
            raise TranslationExecutionError(
                "批次执行失败，且失败状态无法持久化；数据库检查点可能已被其他执行者更新"
            ) from storage_error

    def pause_task(self, task_id: str) -> TranslationTaskCheckpoint:
        stored = self._store.load_task(task_id)
        paused = pause_translation_task(stored.task, stored.checkpoint)
        self._store.commit_checkpoint(stored.task, stored.checkpoint, paused)
        return paused

    def recover_interrupted_task(self, task_id: str) -> TranslationTaskCheckpoint:
        stored = self._store.load_task(task_id)
        recovered = recover_interrupted_translation_task(
            stored.task,
            stored.checkpoint,
        )
        self._store.commit_checkpoint(stored.task, stored.checkpoint, recovered)
        return recovered

    def resume_task(self, task_id: str) -> TranslationTaskCheckpoint:
        stored = self._store.load_task(task_id)
        resumed = resume_translation_task(stored.task, stored.checkpoint)
        self._store.commit_checkpoint(stored.task, stored.checkpoint, resumed)
        return resumed
