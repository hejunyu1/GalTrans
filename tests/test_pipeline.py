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
    TRANSLATION_PROPOSAL_SCHEMA_VERSION,
    TranslationBackend,
    TranslationBatch,
    TranslationBatchStatus,
    TranslationProposal,
    TranslationTask,
    TranslationTaskStatus,
    create_translation_task,
    new_translation_checkpoint,
    start_translation_batch,
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


class _RecordingBackend(TranslationBackend):
    def __init__(self, *, invalid_tokens: bool = False) -> None:
        self.invalid_tokens = invalid_tokens
        self.calls: list[TranslationBatch] = []

    def propose(self, batch: TranslationBatch) -> tuple[TranslationProposal, ...]:
        self.calls.append(batch)
        return tuple(
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


class _FailingBackend(TranslationBackend):
    def __init__(self) -> None:
        self.calls = 0

    def propose(self, batch: TranslationBatch) -> tuple[TranslationProposal, ...]:
        self.calls += 1
        raise RuntimeError(f"temporary failure for {batch.batch_id}")


class _EmptyBackend(TranslationBackend):
    def propose(self, batch: TranslationBatch) -> tuple[TranslationProposal, ...]:
        return ()


class TranslationPipelineTests(unittest.TestCase):
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
                )
                after_first = runner.run_next_batch(task.task_id)

            self.assertEqual(after_first.status, TranslationTaskStatus.RUNNING)
            self.assertEqual(
                tuple(batch.status for batch in after_first.batches),
                (TranslationBatchStatus.COMPLETED, TranslationBatchStatus.PENDING),
            )
            self.assertEqual(len(first_backend.calls), 1)
            filtered = first_backend.calls[0].segments[0]
            self.assertFalse(hasattr(filtered, "source_file"))
            self.assertFalse(hasattr(filtered, "line_number"))

            second_backend = _RecordingBackend()
            with TranslationStore(database, input_project_root=input_project) as store:
                runner = TranslationTaskRunner(
                    store,
                    second_backend,
                    validate_renpy_translation_proposal,
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
                )
                with self.assertRaisesRegex(TranslationExecutionError, "temporary failure"):
                    runner.run_next_batch(task.task_id)
                failed = store.load_task(task.task_id).checkpoint
                self.assertEqual(store.load_accepted_proposals(task.task_id), ())

                resumed = runner.resume_task(task.task_id)
                successful_backend = _RecordingBackend()
                completed = TranslationTaskRunner(
                    store,
                    successful_backend,
                    validate_renpy_translation_proposal,
                ).run_next_batch(task.task_id)

        self.assertEqual(failing_backend.calls, 1)
        self.assertEqual(failed.status, TranslationTaskStatus.FAILED)
        self.assertEqual(failed.batches[0].status, TranslationBatchStatus.FAILED)
        self.assertIn("RuntimeError", failed.batches[0].last_error or "")
        self.assertEqual(resumed.status, TranslationTaskStatus.RUNNING)
        self.assertEqual(completed.status, TranslationTaskStatus.COMPLETED)
        self.assertEqual(completed.batches[0].attempts, 2)

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
                )
                with self.assertRaisesRegex(TranslationExecutionError, "受保护标记"):
                    runner.run_next_batch(task.task_id)
                failed = store.load_task(task.task_id).checkpoint
                proposals = store.load_accepted_proposals(task.task_id)

        self.assertEqual(failed.status, TranslationTaskStatus.FAILED)
        self.assertEqual(proposals, ())

    def test_missing_proposal_marks_batch_failed_without_saving_body(self) -> None:
        task = _task(segment_count=1)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            database = root / "translation.sqlite3"

            with TranslationStore(database, input_project_root=input_project) as store:
                store.initialize_task(task)
                runner = TranslationTaskRunner(
                    store,
                    _EmptyBackend(),
                    validate_renpy_translation_proposal,
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
                )
                with self.assertRaisesRegex(TranslationExecutionError, "显式恢复"):
                    runner.run_next_batch(task.task_id)
                recovered = runner.recover_interrupted_task(task.task_id)
                resumed = runner.resume_task(task.task_id)
                completed = runner.run_next_batch(task.task_id)

        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(recovered.status, TranslationTaskStatus.PAUSED)
        self.assertEqual(recovered.batches[0].status, TranslationBatchStatus.PENDING)
        self.assertEqual(resumed.status, TranslationTaskStatus.RUNNING)
        self.assertEqual(completed.batches[0].attempts, 2)

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


if __name__ == "__main__":
    unittest.main()
