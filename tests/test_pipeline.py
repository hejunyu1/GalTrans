from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from galtrans.adapters.renpy import validate_renpy_translation_proposal
from galtrans.adapters.renpy.extractor import find_renpy_protected_tokens
from galtrans.ir import SegmentKind, TextSegment
from galtrans.pipeline import TranslationExecutionError, TranslationTaskRunner
from galtrans.storage import TranslationStore
from galtrans.translation import (
    PROVIDER_RECEIPT_SCHEMA_VERSION,
    TRANSLATION_PROPOSAL_SCHEMA_VERSION,
    ProviderRequestReceipt,
    ProviderRequestStatus,
    TranslationBackend,
    TranslationBatch,
    TranslationBatchStatus,
    TranslationProposal,
    TranslationSchemaError,
    TranslationTask,
    TranslationTaskStatus,
    create_translation_task,
    new_translation_checkpoint,
    start_translation_batch,
    translation_request_id,
)


def _task(*, batch_size: int = 1, segment_count: int = 2) -> TranslationTask:
    source_texts = ("こんにちは、[player_name]", "またね")[:segment_count]
    segments = tuple(
        TextSegment(
            schema_version=1,
            id=f"seg_{index}",
            engine="renpy",
            source_file="game/script.rpy",
            source_encoding="utf-8",
            source_sha256="a" * 64,
            line_number=index + 1,
            scene="start",
            kind=SegmentKind.DIALOGUE,
            speaker="aoi",
            speaker_display="葵",
            source_text=source_text,
            protected_tokens=find_renpy_protected_tokens(source_text),
        )
        for index, source_text in enumerate(source_texts)
    )
    return create_translation_task(
        segments,
        source_language="ja",
        target_language="zh-Hans",
        batch_size=batch_size,
    )


def _proposal(batch: TranslationBatch, *, invalid_tokens: bool = False) -> TranslationProposal:
    segment = batch.segments[0]
    target_texts = {
        "seg_0": "你好，[player_name]",
        "seg_1": "再见",
    }
    target_text = "你好" if invalid_tokens else target_texts[segment.segment_id]
    return TranslationProposal.from_dict(
        {
            "schema_version": TRANSLATION_PROPOSAL_SCHEMA_VERSION,
            "task_id": batch.task_id,
            "batch_id": batch.batch_id,
            "segment_id": segment.segment_id,
            "source_schema_version": segment.source_schema_version,
            "source_sha256": segment.source_sha256,
            "target_language": batch.target_language,
            "protected_tokens": [token.to_dict() for token in segment.protected_tokens],
            "target_text": target_text,
        }
    )


def _receipt(
    batch: TranslationBatch,
    idempotency_key: str,
    *,
    status: ProviderRequestStatus,
    proposals: tuple[TranslationProposal, ...] = (),
    error: str | None = None,
) -> ProviderRequestReceipt:
    provider_request_id = (
        None
        if status is ProviderRequestStatus.UNKNOWN
        else f"provider:{batch.batch_id}"
    )
    return ProviderRequestReceipt.from_dict(
        {
            "schema_version": PROVIDER_RECEIPT_SCHEMA_VERSION,
            "request_id": idempotency_key,
            "provider_request_id": provider_request_id,
            "status": status.value,
            "proposals": [proposal.to_dict() for proposal in proposals],
            "error": error,
        }
    )


class _RecordingBackend(TranslationBackend):
    def __init__(self, *, invalid_tokens: bool = False) -> None:
        self.invalid_tokens = invalid_tokens
        self.calls: list[TranslationBatch] = []
        self.idempotency_keys: list[str] = []
        self.receipts: dict[str, ProviderRequestReceipt] = {}

    def submit(
        self,
        batch: TranslationBatch,
        idempotency_key: str,
    ) -> ProviderRequestReceipt:
        self.calls.append(batch)
        self.idempotency_keys.append(idempotency_key)
        proposals = tuple(
            _proposal(
                TranslationBatch(
                    schema_version=batch.schema_version,
                    task_id=batch.task_id,
                    batch_id=batch.batch_id,
                    index=batch.index,
                    source_language=batch.source_language,
                    target_language=batch.target_language,
                    segments=(segment,),
                ),
                invalid_tokens=self.invalid_tokens,
            )
            for segment in batch.segments
        )
        receipt = _receipt(
            batch,
            idempotency_key,
            status=ProviderRequestStatus.SUCCEEDED,
            proposals=proposals,
        )
        self.receipts[idempotency_key] = receipt
        return receipt

    def query(
        self,
        idempotency_key: str,
        provider_request_id: str | None,
    ) -> ProviderRequestReceipt:
        receipt = self.receipts[idempotency_key]
        if provider_request_id != receipt.provider_request_id:
            raise RuntimeError("Provider 请求引用不一致")
        return receipt


class _FailingBackend(TranslationBackend):
    def __init__(self) -> None:
        self.calls = 0

    def submit(
        self,
        batch: TranslationBatch,
        idempotency_key: str,
    ) -> ProviderRequestReceipt:
        self.calls += 1
        return _receipt(
            batch,
            idempotency_key,
            status=ProviderRequestStatus.FAILED,
            error=f"definitive failure for {batch.batch_id}",
        )

    def query(
        self,
        idempotency_key: str,
        provider_request_id: str | None,
    ) -> ProviderRequestReceipt:
        raise AssertionError("确定失败不应自动查询")


class _PartialBackend(TranslationBackend):
    def submit(
        self,
        batch: TranslationBatch,
        idempotency_key: str,
    ) -> ProviderRequestReceipt:
        return _receipt(
            batch,
            idempotency_key,
            status=ProviderRequestStatus.SUCCEEDED,
            proposals=(_proposal(batch),),
        )

    def query(
        self,
        idempotency_key: str,
        provider_request_id: str | None,
    ) -> ProviderRequestReceipt:
        raise AssertionError("不完整成功响应不应自动查询")


class _AmbiguousBackend(TranslationBackend):
    def __init__(self) -> None:
        self.submits = 0
        self.queries = 0

    def submit(
        self,
        batch: TranslationBatch,
        idempotency_key: str,
    ) -> ProviderRequestReceipt:
        self.submits += 1
        raise TimeoutError("response lost after submission")

    def query(
        self,
        idempotency_key: str,
        provider_request_id: str | None,
    ) -> ProviderRequestReceipt:
        self.queries += 1
        raise TimeoutError("provider cannot disambiguate")


class _InFlightBackend(_RecordingBackend):
    def __init__(self) -> None:
        super().__init__()
        self.queries = 0

    def submit(
        self,
        batch: TranslationBatch,
        idempotency_key: str,
    ) -> ProviderRequestReceipt:
        self.calls.append(batch)
        self.idempotency_keys.append(idempotency_key)
        receipt = _receipt(
            batch,
            idempotency_key,
            status=ProviderRequestStatus.IN_FLIGHT,
        )
        self.receipts[idempotency_key] = receipt
        return receipt

    def query(
        self,
        idempotency_key: str,
        provider_request_id: str | None,
    ) -> ProviderRequestReceipt:
        self.queries += 1
        batch = self.calls[0]
        if provider_request_id != f"provider:{batch.batch_id}":
            raise RuntimeError("Provider 请求引用不一致")
        return _receipt(
            batch,
            idempotency_key,
            status=ProviderRequestStatus.SUCCEEDED,
            proposals=tuple(
                _proposal(
                    TranslationBatch(
                        schema_version=batch.schema_version,
                        task_id=batch.task_id,
                        batch_id=batch.batch_id,
                        index=batch.index,
                        source_language=batch.source_language,
                        target_language=batch.target_language,
                        segments=(segment,),
                    )
                )
                for segment in batch.segments
            ),
        )


class TranslationPipelineTests(unittest.TestCase):
    def test_invalid_backend_identity_fails_before_starting_batch(self) -> None:
        task = _task(segment_count=1)
        backend = _RecordingBackend()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            database = root / "translation.sqlite3"

            with TranslationStore(database, input_project_root=input_project) as store:
                initial = store.initialize_task(task)
                runner = TranslationTaskRunner(
                    store,
                    backend,
                    validate_renpy_translation_proposal,
                    backend_identity="",
                )
                with self.assertRaisesRegex(TranslationSchemaError, "backend identity"):
                    runner.run_next_batch(task.task_id)
                current = store.load_task(task.task_id).checkpoint

        self.assertEqual(current, initial)
        self.assertEqual(backend.calls, [])

    def test_runs_one_batch_at_a_time_and_resumes_after_database_reopen(self) -> None:
        task = _task()
        first_backend = _RecordingBackend()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            source = input_project / "script.rpy"
            source.write_bytes(b'label start:\n    "Original"\n')
            original = source.read_bytes()
            database = root / "translation.sqlite3"

            with TranslationStore(database, input_project_root=input_project) as store:
                store.initialize_task(task)
                runner = TranslationTaskRunner(
                    store,
                    first_backend,
                    validate_renpy_translation_proposal,
                    backend_identity="deterministic:test-v1",
                )
                after_first = runner.run_next_batch(task.task_id)
                cached = store.load_cached_proposals(
                    task,
                    task.batches[0],
                    "deterministic:test-v1",
                    validate_renpy_translation_proposal,
                )

            self.assertEqual(after_first.status, TranslationTaskStatus.RUNNING)
            self.assertEqual(
                tuple(batch.status for batch in after_first.batches),
                (TranslationBatchStatus.COMPLETED, TranslationBatchStatus.PENDING),
            )
            self.assertEqual(len(first_backend.calls), 1)
            self.assertEqual(tuple(item.segment_id for item in cached or ()), ("seg_0",))
            filtered = first_backend.calls[0].segments[0]
            self.assertFalse(hasattr(filtered, "source_file"))
            self.assertFalse(hasattr(filtered, "line_number"))

            second_backend = _RecordingBackend()
            with TranslationStore(database, input_project_root=input_project) as store:
                runner = TranslationTaskRunner(
                    store,
                    second_backend,
                    validate_renpy_translation_proposal,
                    backend_identity="deterministic:test-v1",
                )
                completed = runner.run_next_batch(task.task_id)
                repeated = runner.run_next_batch(task.task_id)
                proposals = store.load_accepted_proposals(task.task_id)

            final_source = source.read_bytes()

        self.assertEqual(completed.status, TranslationTaskStatus.COMPLETED)
        self.assertEqual(repeated, completed)
        self.assertEqual(len(second_backend.calls), 1)
        self.assertEqual(tuple(item.segment_id for item in proposals), ("seg_0", "seg_1"))
        self.assertEqual(final_source, original)

    def test_backend_failure_is_persisted_without_proposals_and_can_retry(self) -> None:
        task = _task(segment_count=1)
        failing_backend = _FailingBackend()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            database = root / "translation.sqlite3"

            with TranslationStore(database, input_project_root=input_project) as store:
                store.initialize_task(task)
                runner = TranslationTaskRunner(
                    store,
                    failing_backend,
                    validate_renpy_translation_proposal,
                    backend_identity="deterministic:test-v1",
                )
                with self.assertRaisesRegex(TranslationExecutionError, "确定失败"):
                    runner.run_next_batch(task.task_id)
                failed = store.load_task(task.task_id).checkpoint
                self.assertEqual(store.load_accepted_proposals(task.task_id), ())

                resumed = runner.resume_task(task.task_id)
                successful_backend = _RecordingBackend()
                completed = TranslationTaskRunner(
                    store,
                    successful_backend,
                    validate_renpy_translation_proposal,
                    backend_identity="deterministic:test-v1",
                ).run_next_batch(task.task_id)

        self.assertEqual(failing_backend.calls, 1)
        self.assertEqual(failed.status, TranslationTaskStatus.FAILED)
        self.assertEqual(failed.batches[0].status, TranslationBatchStatus.FAILED)
        self.assertIn("definitive failure", failed.batches[0].last_error or "")
        self.assertEqual(resumed.status, TranslationTaskStatus.RUNNING)
        self.assertEqual(completed.status, TranslationTaskStatus.COMPLETED)
        self.assertEqual(completed.batches[0].attempts, 2)
        self.assertEqual(
            successful_backend.idempotency_keys,
            [translation_request_id(task.batches[0], "deterministic:test-v1")],
        )

    def test_invalid_proposal_fails_closed_without_saving_body(self) -> None:
        task = _task(segment_count=1)
        backend = _RecordingBackend(invalid_tokens=True)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            database = root / "translation.sqlite3"

            with TranslationStore(database, input_project_root=input_project) as store:
                store.initialize_task(task)
                runner = TranslationTaskRunner(
                    store,
                    backend,
                    validate_renpy_translation_proposal,
                    backend_identity="deterministic:test-v1",
                )
                with self.assertRaisesRegex(TranslationExecutionError, "受保护标记"):
                    runner.run_next_batch(task.task_id)
                with self.assertRaisesRegex(TranslationExecutionError, "人工处理"):
                    runner.resume_task(task.task_id)
                failed = store.load_task(task.task_id).checkpoint
                proposals = store.load_accepted_proposals(task.task_id)

        self.assertEqual(failed.status, TranslationTaskStatus.FAILED)
        self.assertEqual(proposals, ())

    def test_missing_proposal_marks_batch_failed_without_saving_body(self) -> None:
        task = _task(batch_size=2, segment_count=2)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            database = root / "translation.sqlite3"

            with TranslationStore(database, input_project_root=input_project) as store:
                store.initialize_task(task)
                runner = TranslationTaskRunner(
                    store,
                    _PartialBackend(),
                    validate_renpy_translation_proposal,
                    backend_identity="deterministic:test-v1",
                )
                with self.assertRaisesRegex(TranslationExecutionError, "全部文本段"):
                    runner.run_next_batch(task.task_id)
                failed = store.load_task(task.task_id).checkpoint
                proposals = store.load_accepted_proposals(task.task_id)

        self.assertEqual(failed.status, TranslationTaskStatus.FAILED)
        self.assertEqual(proposals, ())

    def test_interrupted_running_batch_requires_explicit_recovery(self) -> None:
        task = _task(segment_count=1)
        backend = _RecordingBackend()
        initial = new_translation_checkpoint(task)
        started = start_translation_batch(task, initial, task.batches[0].batch_id)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            database = root / "translation.sqlite3"

            with TranslationStore(database, input_project_root=input_project) as store:
                store.initialize_task(task)
                store.commit_checkpoint(task, initial, started)

            with TranslationStore(database, input_project_root=input_project) as store:
                runner = TranslationTaskRunner(
                    store,
                    backend,
                    validate_renpy_translation_proposal,
                    backend_identity="deterministic:test-v1",
                )
                with self.assertRaisesRegex(TranslationExecutionError, "查询恢复"):
                    runner.run_next_batch(task.task_id)
                recovered = runner.recover_interrupted_task(task.task_id)
                resumed = runner.resume_task(task.task_id)
                completed = runner.run_next_batch(task.task_id)

        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(recovered.status, TranslationTaskStatus.PAUSED)
        self.assertEqual(recovered.batches[0].status, TranslationBatchStatus.PENDING)
        self.assertEqual(resumed.status, TranslationTaskStatus.RUNNING)
        self.assertEqual(completed.batches[0].attempts, 2)

    def test_in_flight_request_is_queried_after_reopen_without_resubmission(self) -> None:
        task = _task(segment_count=1)
        backend = _InFlightBackend()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            database = root / "translation.sqlite3"

            with TranslationStore(database, input_project_root=input_project) as store:
                store.initialize_task(task)
                runner = TranslationTaskRunner(
                    store,
                    backend,
                    validate_renpy_translation_proposal,
                    backend_identity="deterministic:test-v1",
                )
                in_flight = runner.run_next_batch(task.task_id)

            with TranslationStore(database, input_project_root=input_project) as store:
                completed = TranslationTaskRunner(
                    store,
                    backend,
                    validate_renpy_translation_proposal,
                    backend_identity="deterministic:test-v1",
                ).recover_interrupted_task(task.task_id)
                accepted = store.load_accepted_proposals(task.task_id)

        self.assertEqual(in_flight.status, TranslationTaskStatus.RUNNING)
        self.assertEqual(in_flight.batches[0].status, TranslationBatchStatus.RUNNING)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(backend.queries, 1)
        self.assertEqual(completed.status, TranslationTaskStatus.COMPLETED)
        self.assertEqual(accepted[0].segment_id, "seg_0")

    def test_ambiguous_failure_requires_query_then_manual_handling(self) -> None:
        task = _task(segment_count=1)
        backend = _AmbiguousBackend()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            database = root / "translation.sqlite3"

            with TranslationStore(database, input_project_root=input_project) as store:
                store.initialize_task(task)
                runner = TranslationTaskRunner(
                    store,
                    backend,
                    validate_renpy_translation_proposal,
                    backend_identity="deterministic:test-v1",
                )
                with self.assertRaisesRegex(TranslationExecutionError, "结果未知"):
                    runner.run_next_batch(task.task_id)
                with self.assertRaisesRegex(TranslationExecutionError, "查询恢复"):
                    runner.run_next_batch(task.task_id)
                with self.assertRaisesRegex(TranslationExecutionError, "人工处理"):
                    runner.recover_interrupted_task(task.task_id)
                current = store.load_task(task.task_id).checkpoint
                receipt = store.load_provider_receipt(
                    task,
                    task.batches[0],
                    "deterministic:test-v1",
                )

        self.assertEqual(backend.submits, 1)
        self.assertEqual(backend.queries, 1)
        self.assertEqual(current.status, TranslationTaskStatus.RUNNING)
        self.assertEqual(receipt.status if receipt else None, ProviderRequestStatus.UNKNOWN)

    def test_pause_blocks_backend_until_explicit_resume(self) -> None:
        task = _task()
        backend = _RecordingBackend()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            database = root / "translation.sqlite3"

            with TranslationStore(database, input_project_root=input_project) as store:
                store.initialize_task(task)
                runner = TranslationTaskRunner(
                    store,
                    backend,
                    validate_renpy_translation_proposal,
                    backend_identity="deterministic:test-v1",
                )
                runner.run_next_batch(task.task_id)
                paused = runner.pause_task(task.task_id)
                with self.assertRaisesRegex(TranslationExecutionError, "paused"):
                    runner.run_next_batch(task.task_id)
                self.assertEqual(len(backend.calls), 1)
                runner.resume_task(task.task_id)
                completed = runner.run_next_batch(task.task_id)

        self.assertEqual(paused.status, TranslationTaskStatus.PAUSED)
        self.assertEqual(completed.status, TranslationTaskStatus.COMPLETED)
        self.assertEqual(len(backend.calls), 2)

    def test_cached_response_completes_after_crash_without_backend_call(self) -> None:
        task = _task(segment_count=1)
        batch = task.batches[0]
        initial = new_translation_checkpoint(task)
        started = start_translation_batch(task, initial, batch.batch_id)
        proposal = _proposal(batch)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            database = root / "translation.sqlite3"

            with TranslationStore(database, input_project_root=input_project) as store:
                store.initialize_task(task)
                store.commit_checkpoint(task, initial, started)
                store.store_cached_proposals(
                    task,
                    batch,
                    "deterministic:test-v1",
                    (proposal,),
                    validate_renpy_translation_proposal,
                )

            backend = _RecordingBackend()
            with TranslationStore(database, input_project_root=input_project) as store:
                runner = TranslationTaskRunner(
                    store,
                    backend,
                    validate_renpy_translation_proposal,
                    backend_identity="deterministic:test-v1",
                )
                completed = runner.recover_interrupted_task(task.task_id)
                accepted = store.load_accepted_proposals(task.task_id)

        self.assertEqual(backend.calls, [])
        self.assertEqual(completed.status, TranslationTaskStatus.COMPLETED)
        self.assertEqual(accepted, (proposal,))


if __name__ == "__main__":
    unittest.main()
