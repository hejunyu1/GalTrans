from __future__ import annotations

from galtrans.storage import ProposalValidator, TranslationStorageError, TranslationStore
from galtrans.translation import (
    PROVIDER_RECEIPT_SCHEMA_VERSION,
    ProviderRequestReceipt,
    ProviderRequestStatus,
    TranslationBackend,
    TranslationBatch,
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
        """Submit at most one pending batch through the durable Provider boundary."""
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
                f"批次 {running[0].batch_id} 仍为 running；请先查询恢复 Provider 请求"
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
        request_id = translation_request_id(batch, self._backend_identity)
        cached = self._store.load_cached_proposals(
            task,
            batch,
            self._backend_identity,
            self._proposal_validator,
        )
        existing_receipt = self._store.load_provider_receipt(
            task,
            batch,
            self._backend_identity,
        )
        if existing_receipt is not None and (
            existing_receipt.status is not ProviderRequestStatus.FAILED
        ):
            raise TranslationExecutionError(
                f"Provider 请求 {request_id} 已处于 {existing_receipt.status.value}；"
                "必须先查询恢复，不能自动重提"
            )

        started = start_translation_batch(task, checkpoint, batch.batch_id)
        self._store.commit_checkpoint(task, checkpoint, started)
        if cached is not None:
            return self._complete_proposals(task, batch, started, cached)

        unknown = ProviderRequestReceipt(
            schema_version=PROVIDER_RECEIPT_SCHEMA_VERSION,
            request_id=request_id,
            provider_request_id=(
                None
                if existing_receipt is None
                else existing_receipt.provider_request_id
            ),
            status=ProviderRequestStatus.UNKNOWN,
            proposals=(),
            error="Provider 提交尚未返回可确认回执",
        )
        if existing_receipt is None:
            self._store.store_provider_receipt(
                task, batch, self._backend_identity, unknown
            )
        else:
            self._store.store_provider_receipt(
                task,
                batch,
                self._backend_identity,
                unknown,
                expected_receipt=existing_receipt,
                allow_retry=True,
            )

        try:
            receipt = self._backend.submit(batch, request_id)
            if not isinstance(receipt, ProviderRequestReceipt):
                raise TypeError("Provider 必须返回 ProviderRequestReceipt")
            receipt = self._store.store_provider_receipt(
                task,
                batch,
                self._backend_identity,
                receipt,
                expected_receipt=unknown,
            )
        except Exception as error:
            self._persist_unknown_receipt(task, batch, unknown, error)
            raise TranslationExecutionError(
                f"Provider 请求 {request_id} 结果未知；必须查询，"
                f"不能自动重试：{_failure_message(error)}"
            ) from error
        return self._apply_receipt(task, batch, started, receipt)

    def _complete_proposals(
        self,
        task: TranslationTask,
        batch: TranslationBatch,
        started: TranslationTaskCheckpoint,
        proposals: tuple[TranslationProposal, ...],
    ) -> TranslationTaskCheckpoint:
        try:
            validated = tuple(
                self._proposal_validator(task, proposal) for proposal in proposals
            )
            completed = complete_translation_batch(
                task, started, batch.batch_id, validated
            )
            self._store.store_cached_proposals(
                task,
                batch,
                self._backend_identity,
                proposals,
                self._proposal_validator,
            )
            self._store.commit_checkpoint(
                task,
                started,
                completed,
                proposals=proposals,
                proposal_validator=self._proposal_validator,
            )
        except Exception as error:
            self._persist_failure(task, started, batch.batch_id, error)
            raise TranslationExecutionError(
                f"批次 {batch.batch_id} 的 Provider 结果验证失败："
                f"{_failure_message(error)}"
            ) from error
        return completed

    def _apply_receipt(
        self,
        task: TranslationTask,
        batch: TranslationBatch,
        started: TranslationTaskCheckpoint,
        receipt: ProviderRequestReceipt,
    ) -> TranslationTaskCheckpoint:
        if receipt.status is ProviderRequestStatus.SUCCEEDED:
            return self._complete_proposals(task, batch, started, receipt.proposals)
        if receipt.status is ProviderRequestStatus.IN_FLIGHT:
            return started
        if receipt.status is ProviderRequestStatus.FAILED:
            error = RuntimeError(receipt.error or "Provider 确定失败")
            self._persist_failure(task, started, batch.batch_id, error)
            raise TranslationExecutionError(
                f"Provider 请求 {receipt.request_id} 确定失败；"
                "显式恢复后才可受控重试"
            )
        raise TranslationExecutionError(
            f"Provider 请求 {receipt.request_id} 结果未知；必须查询，"
            "若仍无法消歧则要求人工处理"
        )

    def _persist_unknown_receipt(
        self,
        task: TranslationTask,
        batch: TranslationBatch,
        previous: ProviderRequestReceipt,
        error: Exception,
    ) -> None:
        unknown = ProviderRequestReceipt(
            schema_version=PROVIDER_RECEIPT_SCHEMA_VERSION,
            request_id=previous.request_id,
            provider_request_id=previous.provider_request_id,
            status=ProviderRequestStatus.UNKNOWN,
            proposals=(),
            error=_failure_message(error),
        )
        try:
            self._store.store_provider_receipt(
                task,
                batch,
                self._backend_identity,
                unknown,
                expected_receipt=previous,
            )
        except TranslationStorageError as storage_error:
            raise TranslationExecutionError(
                "Provider 结果未知，且未知回执无法持久化；"
                "数据库回执可能已被其他执行者更新"
            ) from storage_error

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
        running = next(
            (
                item
                for item in stored.checkpoint.batches
                if item.status is TranslationBatchStatus.RUNNING
            ),
            None,
        )
        if running is not None:
            batch = next(
                item for item in stored.task.batches if item.batch_id == running.batch_id
            )
            receipt = self._store.load_provider_receipt(
                stored.task,
                batch,
                self._backend_identity,
            )
            if receipt is not None:
                raise TranslationExecutionError(
                    f"Provider 请求 {receipt.request_id} 已提交；"
                    "必须查询恢复，不能通过暂停退回待提交状态"
                )
        paused = pause_translation_task(stored.task, stored.checkpoint)
        self._store.commit_checkpoint(stored.task, stored.checkpoint, paused)
        return paused

    def recover_interrupted_task(self, task_id: str) -> TranslationTaskCheckpoint:
        stored = self._store.load_task(task_id)
        running = next(
            (
                item
                for item in stored.checkpoint.batches
                if item.status is TranslationBatchStatus.RUNNING
            ),
            None,
        )
        if running is None:
            raise TranslationExecutionError("任务没有可查询恢复的在途批次")
        task = stored.task
        batch = next(item for item in task.batches if item.batch_id == running.batch_id)
        cached = self._store.load_cached_proposals(
            task,
            batch,
            self._backend_identity,
            self._proposal_validator,
        )
        if cached is not None:
            return self._complete_proposals(task, batch, stored.checkpoint, cached)

        receipt = self._store.load_provider_receipt(
            task,
            batch,
            self._backend_identity,
        )
        if receipt is None:
            recovered = recover_interrupted_translation_task(
                task,
                stored.checkpoint,
            )
            self._store.commit_checkpoint(task, stored.checkpoint, recovered)
            return recovered
        if receipt.status in {
            ProviderRequestStatus.SUCCEEDED,
            ProviderRequestStatus.FAILED,
        }:
            return self._apply_receipt(task, batch, stored.checkpoint, receipt)

        try:
            queried = self._backend.query(
                receipt.request_id,
                receipt.provider_request_id,
            )
            if not isinstance(queried, ProviderRequestReceipt):
                raise TypeError("Provider 查询必须返回 ProviderRequestReceipt")
            queried = self._store.store_provider_receipt(
                task,
                batch,
                self._backend_identity,
                queried,
                expected_receipt=receipt,
            )
        except Exception as error:
            self._persist_unknown_receipt(task, batch, receipt, error)
            raise TranslationExecutionError(
                f"Provider 请求 {receipt.request_id} 查询后仍无法消歧；"
                "禁止自动重试，要求人工处理"
            ) from error
        return self._apply_receipt(task, batch, stored.checkpoint, queried)

    def resume_task(self, task_id: str) -> TranslationTaskCheckpoint:
        stored = self._store.load_task(task_id)
        failed = next(
            (
                item
                for item in stored.checkpoint.batches
                if item.status is TranslationBatchStatus.FAILED
            ),
            None,
        )
        if failed is not None:
            batch = next(
                item for item in stored.task.batches if item.batch_id == failed.batch_id
            )
            receipt = self._store.load_provider_receipt(
                stored.task,
                batch,
                self._backend_identity,
            )
            if (
                receipt is not None
                and receipt.status is ProviderRequestStatus.SUCCEEDED
            ):
                raise TranslationExecutionError(
                    "Provider 已成功返回，但提案未通过本地验证；"
                    "同一幂等键不能重试，必须人工处理或更换 backend identity"
                )
        resumed = resume_translation_task(stored.task, stored.checkpoint)
        self._store.commit_checkpoint(stored.task, stored.checkpoint, resumed)
        return resumed
