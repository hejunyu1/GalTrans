from __future__ import annotations

import unittest
from dataclasses import replace

from galtrans.adapters.renpy import validate_renpy_translation_proposal
from galtrans.adapters.renpy.extractor import find_renpy_protected_tokens
from galtrans.ir import SegmentKind, TextSegment
from galtrans.translation import (
    TRANSLATION_PROPOSAL_SCHEMA_VERSION,
    TranslationBackend,
    TranslationBatch,
    TranslationBatchStatus,
    TranslationProposal,
    TranslationSchemaError,
    TranslationStateError,
    TranslationTask,
    TranslationTaskStatus,
    TranslationValidationError,
    complete_translation_batch,
    create_translation_task,
    fail_translation_batch,
    new_translation_checkpoint,
    pause_translation_task,
    recover_interrupted_translation_task,
    resume_translation_task,
    start_translation_batch,
    translation_checkpoint_from_dict,
)


def _segment(
    segment_id: str,
    source_text: str,
    *,
    source_sha256: str = "a" * 64,
) -> TextSegment:
    return TextSegment(
        schema_version=1,
        id=segment_id,
        engine="renpy",
        source_file="game/script.rpy",
        source_encoding="utf-8",
        source_sha256=source_sha256,
        line_number=8,
        scene="start",
        kind=SegmentKind.DIALOGUE,
        speaker="aoi",
        speaker_display="葵",
        source_text=source_text,
        protected_tokens=find_renpy_protected_tokens(source_text),
    )


def _proposal(
    task: TranslationTask,
    *,
    batch_index: int = 0,
    segment_index: int = 0,
    target_text: str = "你好，[player_name]",
) -> TranslationProposal:
    batch = task.batches[batch_index]
    segment = batch.segments[segment_index]
    return TranslationProposal.from_dict(
        {
            "schema_version": TRANSLATION_PROPOSAL_SCHEMA_VERSION,
            "task_id": task.task_id,
            "batch_id": batch.batch_id,
            "segment_id": segment.segment_id,
            "source_schema_version": segment.source_schema_version,
            "source_sha256": segment.source_sha256,
            "target_language": task.target_language,
            "protected_tokens": [
                token.to_dict() for token in segment.protected_tokens
            ],
            "target_text": target_text,
        }
    )


class _DeterministicBackend(TranslationBackend):
    def propose(self, batch: TranslationBatch) -> tuple[TranslationProposal, ...]:
        return tuple(
            TranslationProposal.from_dict(
                {
                    "schema_version": TRANSLATION_PROPOSAL_SCHEMA_VERSION,
                    "task_id": batch.task_id,
                    "batch_id": batch.batch_id,
                    "segment_id": segment.segment_id,
                    "source_schema_version": segment.source_schema_version,
                    "source_sha256": segment.source_sha256,
                    "target_language": batch.target_language,
                    "protected_tokens": [
                        token.to_dict() for token in segment.protected_tokens
                    ],
                    "target_text": segment.source_text.replace("こんにちは", "你好"),
                }
            )
            for segment in batch.segments
        )


class TranslationBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.segments = (
            _segment("seg_one", "こんにちは、[player_name]"),
            _segment("seg_two", "またね"),
        )
        self.task = create_translation_task(
            self.segments,
            source_language="ja",
            target_language="zh-Hans",
            batch_size=1,
        )

    def test_task_and_batch_ids_are_stable_and_requests_are_filtered(self) -> None:
        repeated = create_translation_task(
            self.segments,
            source_language="ja",
            target_language="zh-Hans",
            batch_size=1,
        )

        self.assertEqual(repeated.task_id, self.task.task_id)
        self.assertEqual(
            [batch.batch_id for batch in repeated.batches],
            [batch.batch_id for batch in self.task.batches],
        )
        request = self.task.batches[0].segments[0].to_dict()
        self.assertNotIn("source_file", request)
        self.assertNotIn("line_number", request)
        self.assertNotIn("source_encoding", request)
        self.assertEqual(TranslationTask.from_dict(self.task.to_dict()), self.task)

    def test_task_id_changes_with_source_snapshot_order_language_or_batch_policy(self) -> None:
        changed_hash = create_translation_task(
            (replace(self.segments[0], source_sha256="b" * 64), self.segments[1]),
            source_language="ja",
            target_language="zh-Hans",
            batch_size=1,
        )
        reversed_task = create_translation_task(
            reversed(self.segments),
            source_language="ja",
            target_language="zh-Hans",
            batch_size=1,
        )
        changed_language = create_translation_task(
            self.segments,
            source_language="ja",
            target_language="zh-Hant",
            batch_size=1,
        )
        changed_batching = create_translation_task(
            self.segments,
            source_language="ja",
            target_language="zh-Hans",
            batch_size=2,
        )

        self.assertEqual(
            len(
                {
                    self.task.task_id,
                    changed_hash.task_id,
                    reversed_task.task_id,
                    changed_language.task_id,
                    changed_batching.task_id,
                }
            ),
            5,
        )

    def test_proposal_schema_is_closed_and_versioned(self) -> None:
        raw = _proposal(self.task).to_dict()
        raw["unexpected"] = True
        with self.assertRaisesRegex(TranslationSchemaError, "额外"):
            TranslationProposal.from_dict(raw)

        raw = _proposal(self.task).to_dict()
        raw["schema_version"] = 2
        with self.assertRaisesRegex(TranslationSchemaError, "schema"):
            TranslationProposal.from_dict(raw)

    def test_task_and_checkpoint_versions_fail_closed(self) -> None:
        raw_task = self.task.to_dict()
        raw_task["schema_version"] = 2
        with self.assertRaisesRegex(TranslationSchemaError, "schema"):
            TranslationTask.from_dict(raw_task)

        raw_checkpoint = new_translation_checkpoint(self.task).to_dict()
        raw_checkpoint["schema_version"] = 2
        with self.assertRaisesRegex(TranslationSchemaError, "schema"):
            translation_checkpoint_from_dict(self.task, raw_checkpoint)

    def test_proposal_rejects_wrong_task_batch_segment_schema_hash_or_language(self) -> None:
        valid = _proposal(self.task)
        changes = (
            replace(valid, schema_version=2),
            replace(valid, task_id="task_wrong"),
            replace(valid, batch_id=self.task.batches[1].batch_id),
            replace(valid, segment_id="seg_wrong"),
            replace(valid, source_schema_version=2),
            replace(valid, source_sha256="b" * 64),
            replace(valid, target_language="en"),
        )
        for proposal in changes:
            with self.subTest(proposal=proposal):
                with self.assertRaises(TranslationValidationError):
                    validate_renpy_translation_proposal(self.task, proposal)

        other_engine_task = create_translation_task(
            (replace(self.segments[0], engine="other"),),
            source_language="ja",
            target_language="zh-Hans",
            batch_size=1,
        )
        with self.assertRaisesRegex(TranslationValidationError, "引擎"):
            validate_renpy_translation_proposal(
                other_engine_task,
                _proposal(other_engine_task),
            )

    def test_renpy_adapter_rescans_target_protected_tokens(self) -> None:
        valid = validate_renpy_translation_proposal(self.task, _proposal(self.task))
        repeated = validate_renpy_translation_proposal(self.task, _proposal(self.task))
        self.assertEqual(valid.proposal_id, repeated.proposal_id)

        with self.assertRaisesRegex(TranslationValidationError, "回显"):
            validate_renpy_translation_proposal(
                self.task,
                replace(_proposal(self.task), protected_tokens=()),
            )
        with self.assertRaisesRegex(TranslationValidationError, "受保护标记"):
            validate_renpy_translation_proposal(
                self.task,
                _proposal(self.task, target_text="你好"),
            )
        with self.assertRaisesRegex(TranslationValidationError, "受保护标记"):
            validate_renpy_translation_proposal(
                self.task,
                _proposal(self.task, target_text="你好，[other_name]"),
            )

    def test_deterministic_backend_requires_no_file_or_network_access(self) -> None:
        backend = _DeterministicBackend()
        proposals = backend.propose(self.task.batches[0])
        validated = tuple(
            validate_renpy_translation_proposal(self.task, proposal)
            for proposal in proposals
        )

        self.assertEqual([item.segment_id for item in validated], ["seg_one"])
        self.assertEqual(validated[0].target_text, "你好、[player_name]")

    def test_checkpoint_pauses_resumes_fails_retries_and_completes(self) -> None:
        checkpoint = new_translation_checkpoint(self.task)
        first_batch = self.task.batches[0]
        checkpoint = start_translation_batch(self.task, checkpoint, first_batch.batch_id)
        self.assertEqual(checkpoint.batches[0].attempts, 1)

        checkpoint = pause_translation_task(self.task, checkpoint)
        self.assertEqual(checkpoint.status, TranslationTaskStatus.PAUSED)
        self.assertEqual(checkpoint.batches[0].status, TranslationBatchStatus.PENDING)
        checkpoint = resume_translation_task(self.task, checkpoint)
        checkpoint = start_translation_batch(self.task, checkpoint, first_batch.batch_id)
        checkpoint = fail_translation_batch(
            self.task,
            checkpoint,
            first_batch.batch_id,
            "deterministic failure",
        )
        self.assertEqual(checkpoint.status, TranslationTaskStatus.FAILED)
        checkpoint = resume_translation_task(self.task, checkpoint)
        checkpoint = start_translation_batch(self.task, checkpoint, first_batch.batch_id)
        self.assertEqual(checkpoint.batches[0].attempts, 3)
        self.assertEqual(checkpoint.batches[0].last_error, "deterministic failure")

        first = validate_renpy_translation_proposal(self.task, _proposal(self.task))
        checkpoint = complete_translation_batch(
            self.task,
            checkpoint,
            first_batch.batch_id,
            (first,),
        )
        second_batch = self.task.batches[1]
        checkpoint = start_translation_batch(self.task, checkpoint, second_batch.batch_id)
        second = validate_renpy_translation_proposal(
            self.task,
            _proposal(
                self.task,
                batch_index=1,
                target_text="再见",
            ),
        )
        checkpoint = complete_translation_batch(
            self.task,
            checkpoint,
            second_batch.batch_id,
            (second,),
        )

        self.assertEqual(checkpoint.status, TranslationTaskStatus.COMPLETED)

    def test_completed_batch_replay_is_idempotent_and_conflict_is_rejected(self) -> None:
        batch = self.task.batches[0]
        checkpoint = start_translation_batch(
            self.task,
            new_translation_checkpoint(self.task),
            batch.batch_id,
        )
        accepted = validate_renpy_translation_proposal(self.task, _proposal(self.task))
        completed = complete_translation_batch(
            self.task,
            checkpoint,
            batch.batch_id,
            (accepted,),
        )

        self.assertIs(
            complete_translation_batch(
                self.task,
                completed,
                batch.batch_id,
                (accepted,),
            ),
            completed,
        )
        conflicting = validate_renpy_translation_proposal(
            self.task,
            _proposal(self.task, target_text="您好，[player_name]"),
        )
        with self.assertRaisesRegex(TranslationStateError, "冲突"):
            complete_translation_batch(
                self.task,
                completed,
                batch.batch_id,
                (conflicting,),
            )
        with self.assertRaisesRegex(TranslationStateError, "来源"):
            complete_translation_batch(
                self.task,
                checkpoint,
                batch.batch_id,
                (replace(accepted, source_sha256="b" * 64),),
            )

    def test_checkpoint_round_trip_and_interrupted_batch_recovery(self) -> None:
        batch = self.task.batches[0]
        running = start_translation_batch(
            self.task,
            new_translation_checkpoint(self.task),
            batch.batch_id,
        )
        restored = translation_checkpoint_from_dict(self.task, running.to_dict())
        self.assertEqual(restored, running)

        recovered = recover_interrupted_translation_task(self.task, restored)
        self.assertEqual(recovered.status, TranslationTaskStatus.PAUSED)
        self.assertEqual(recovered.batches[0].status, TranslationBatchStatus.PENDING)
        resumed = resume_translation_task(self.task, recovered)
        retried = start_translation_batch(self.task, resumed, batch.batch_id)
        self.assertEqual(retried.batches[0].attempts, 2)


if __name__ == "__main__":
    unittest.main()
