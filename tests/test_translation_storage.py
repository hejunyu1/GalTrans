from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from galtrans.adapters.renpy import validate_renpy_translation_proposal
from galtrans.adapters.renpy.extractor import find_renpy_protected_tokens
from galtrans.ir import SegmentKind, TextSegment
from galtrans.storage import TranslationStorageError, TranslationStore
from galtrans.translation import (
    PROVIDER_RECEIPT_SCHEMA_VERSION,
    TRANSLATION_PROPOSAL_SCHEMA_VERSION,
    ProviderRequestReceipt,
    ProviderRequestStatus,
    TranslationProposal,
    TranslationTask,
    ValidatedTranslation,
    complete_translation_batch,
    create_translation_task,
    new_translation_checkpoint,
    start_translation_batch,
    translation_request_id,
)


def _task() -> TranslationTask:
    source_texts = ("こんにちは、[player_name]", "またね")
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
        batch_size=1,
    )


def _proposal(
    task: TranslationTask,
    *,
    target_text: str = "你好，[player_name]",
) -> TranslationProposal:
    batch = task.batches[0]
    segment = batch.segments[0]
    return TranslationProposal.from_dict(
        {
            "schema_version": TRANSLATION_PROPOSAL_SCHEMA_VERSION,
            "task_id": task.task_id,
            "batch_id": batch.batch_id,
            "segment_id": segment.segment_id,
            "source_schema_version": segment.source_schema_version,
            "source_sha256": segment.source_sha256,
            "target_language": task.target_language,
            "protected_tokens": [token.to_dict() for token in segment.protected_tokens],
            "target_text": target_text,
        }
    )


def _provider_receipt(
    task: TranslationTask,
    status: ProviderRequestStatus,
    *,
    provider_request_id: str | None = None,
    proposals: tuple[TranslationProposal, ...] = (),
    error: str | None = None,
) -> ProviderRequestReceipt:
    batch = task.batches[0]
    return ProviderRequestReceipt.from_dict(
        {
            "schema_version": PROVIDER_RECEIPT_SCHEMA_VERSION,
            "request_id": translation_request_id(batch, "deterministic:test-v1"),
            "provider_request_id": provider_request_id,
            "status": status.value,
            "proposals": [proposal.to_dict() for proposal in proposals],
            "error": error,
        }
    )


class TranslationStorageTests(unittest.TestCase):
    def test_provider_receipts_are_cas_persisted_and_reopened(self) -> None:
        task = _task()
        batch = task.batches[0]
        unknown = _provider_receipt(
            task,
            ProviderRequestStatus.UNKNOWN,
            error="submission outcome unknown",
        )
        in_flight = _provider_receipt(
            task,
            ProviderRequestStatus.IN_FLIGHT,
            provider_request_id="provider-123",
        )
        succeeded = _provider_receipt(
            task,
            ProviderRequestStatus.SUCCEEDED,
            provider_request_id="provider-123",
            proposals=(_proposal(task),),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            database = root / "translation.sqlite3"

            with TranslationStore(database, input_project_root=input_project) as store:
                store.initialize_task(task)
                stored_unknown = store.store_provider_receipt(
                    task,
                    batch,
                    "deterministic:test-v1",
                    unknown,
                )
                stored_in_flight = store.store_provider_receipt(
                    task,
                    batch,
                    "deterministic:test-v1",
                    in_flight,
                    expected_receipt=unknown,
                )

            with TranslationStore(database, input_project_root=input_project) as store:
                reopened = store.load_provider_receipt(
                    task,
                    batch,
                    "deterministic:test-v1",
                )
                stored_succeeded = store.store_provider_receipt(
                    task,
                    batch,
                    "deterministic:test-v1",
                    succeeded,
                    expected_receipt=in_flight,
                )
                with self.assertRaisesRegex(TranslationStorageError, "其他执行者"):
                    store.store_provider_receipt(
                        task,
                        batch,
                        "deterministic:test-v1",
                        unknown,
                        expected_receipt=in_flight,
                    )

        self.assertEqual(stored_unknown, unknown)
        self.assertEqual(stored_in_flight, in_flight)
        self.assertEqual(reopened, in_flight)
        self.assertEqual(stored_succeeded, succeeded)

    def test_provider_receipt_terminal_and_reference_changes_fail_closed(self) -> None:
        task = _task()
        batch = task.batches[0]
        in_flight = _provider_receipt(
            task,
            ProviderRequestStatus.IN_FLIGHT,
            provider_request_id="provider-123",
        )
        changed_reference = _provider_receipt(
            task,
            ProviderRequestStatus.FAILED,
            provider_request_id="provider-456",
            error="definitive failure",
        )
        succeeded = _provider_receipt(
            task,
            ProviderRequestStatus.SUCCEEDED,
            provider_request_id="provider-123",
            proposals=(_proposal(task),),
        )
        failed = _provider_receipt(
            task,
            ProviderRequestStatus.FAILED,
            provider_request_id="provider-123",
            error="late failure",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            database = root / "translation.sqlite3"

            with TranslationStore(database, input_project_root=input_project) as store:
                store.initialize_task(task)
                store.store_provider_receipt(
                    task,
                    batch,
                    "deterministic:test-v1",
                    in_flight,
                )
                with self.assertRaisesRegex(TranslationStorageError, "不能被移除或替换"):
                    store.store_provider_receipt(
                        task,
                        batch,
                        "deterministic:test-v1",
                        changed_reference,
                        expected_receipt=in_flight,
                    )
                store.store_provider_receipt(
                    task,
                    batch,
                    "deterministic:test-v1",
                    succeeded,
                    expected_receipt=in_flight,
                )
                with self.assertRaisesRegex(TranslationStorageError, "不能从"):
                    store.store_provider_receipt(
                        task,
                        batch,
                        "deterministic:test-v1",
                        failed,
                        expected_receipt=succeeded,
                    )

    def test_initializes_outside_input_and_recovers_checkpoint_after_reopen(self) -> None:
        task = _task()
        initial = new_translation_checkpoint(task)
        started = start_translation_batch(task, initial, task.batches[0].batch_id)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            source = input_project / "script.rpy"
            source.write_bytes(b'label start:\n    "Original"\n')
            original = source.read_bytes()
            workspace = root / "workspace"
            workspace.mkdir()
            database = workspace / "translation.sqlite3"

            with TranslationStore(database, input_project_root=input_project) as store:
                self.assertEqual(store.initialize_task(task), initial)
                store.commit_checkpoint(task, initial, started)

            with TranslationStore(database, input_project_root=input_project) as store:
                recovered = store.load_task(task.task_id)

            final_source = source.read_bytes()

        self.assertEqual(recovered.task, task)
        self.assertEqual(recovered.checkpoint, started)
        self.assertEqual(final_source, original)

    def test_refuses_database_inside_input_or_unrecognized_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            inside = input_project / "translation.sqlite3"

            with self.assertRaisesRegex(TranslationStorageError, "输入项目"):
                TranslationStore(inside, input_project_root=input_project)
            self.assertFalse(inside.exists())

            unrelated = root / "unrelated.sqlite3"
            connection = sqlite3.connect(unrelated)
            try:
                connection.execute("CREATE TABLE keep_me (value TEXT NOT NULL)")
                connection.execute("INSERT INTO keep_me VALUES ('keep')")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(TranslationStorageError, "GalTrans"):
                TranslationStore(unrelated, input_project_root=input_project)
            connection = sqlite3.connect(unrelated)
            try:
                value = connection.execute("SELECT value FROM keep_me").fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(value, "keep")

    def test_refuses_previous_and_unknown_storage_schema_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            for version in (1, 2, 4):
                database = root / f"translation-v{version}.sqlite3"
                with TranslationStore(database, input_project_root=input_project):
                    pass
                connection = sqlite3.connect(database)
                try:
                    connection.execute(f"PRAGMA user_version = {version}")
                    connection.commit()
                finally:
                    connection.close()

                with self.subTest(version=version):
                    with self.assertRaisesRegex(TranslationStorageError, "schema"):
                        TranslationStore(database, input_project_root=input_project)
                    connection = sqlite3.connect(database)
                    try:
                        final_version = connection.execute(
                            "PRAGMA user_version"
                        ).fetchone()[0]
                    finally:
                        connection.close()
                    self.assertEqual(final_version, version)

    def test_stores_and_revalidates_backend_scoped_cached_proposals(self) -> None:
        task = _task()
        proposal = _proposal(task)
        batch = task.batches[0]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            database = root / "translation.sqlite3"

            with TranslationStore(database, input_project_root=input_project) as store:
                store.initialize_task(task)
                self.assertIsNone(
                    store.load_cached_proposals(
                        task,
                        batch,
                        "deterministic:test-v1",
                        validate_renpy_translation_proposal,
                    )
                )
                request_id = store.store_cached_proposals(
                    task,
                    batch,
                    "deterministic:test-v1",
                    (proposal,),
                    validate_renpy_translation_proposal,
                )
                cached = store.load_cached_proposals(
                    task,
                    batch,
                    "deterministic:test-v1",
                    validate_renpy_translation_proposal,
                )
                other_backend = store.load_cached_proposals(
                    task,
                    batch,
                    "deterministic:test-v2",
                    validate_renpy_translation_proposal,
                )

        self.assertRegex(request_id, r"^request_[0-9a-f]{24}$")
        self.assertEqual(cached, (proposal,))
        self.assertIsNone(other_backend)

    def test_rejects_invalid_or_conflicting_cached_proposals(self) -> None:
        task = _task()
        proposal = _proposal(task)
        batch = task.batches[0]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            database = root / "translation.sqlite3"

            with TranslationStore(database, input_project_root=input_project) as store:
                store.initialize_task(task)
                with self.assertRaisesRegex(TranslationStorageError, "验证失败"):
                    store.store_cached_proposals(
                        task,
                        batch,
                        "deterministic:test-v1",
                        (replace(proposal, target_text="你好"),),
                        validate_renpy_translation_proposal,
                    )
                store.store_cached_proposals(
                    task,
                    batch,
                    "deterministic:test-v1",
                    (proposal,),
                    validate_renpy_translation_proposal,
                )
                with self.assertRaisesRegex(TranslationStorageError, "不同的缓存结果"):
                    store.store_cached_proposals(
                        task,
                        batch,
                        "deterministic:test-v1",
                        (_proposal(task, target_text="您好，[player_name]"),),
                        validate_renpy_translation_proposal,
                    )

                connection = sqlite3.connect(database)
                try:
                    connection.execute(
                        "UPDATE translation_request_cache SET proposals_json = '[]'"
                    )
                    connection.commit()
                finally:
                    connection.close()
                with self.assertRaisesRegex(TranslationStorageError, "完整"):
                    store.load_cached_proposals(
                        task,
                        batch,
                        "deterministic:test-v1",
                        validate_renpy_translation_proposal,
                    )

    def test_commits_validated_proposal_and_checkpoint_atomically(self) -> None:
        task = _task()
        initial = new_translation_checkpoint(task)
        batch = task.batches[0]
        started = start_translation_batch(task, initial, batch.batch_id)
        proposal = _proposal(task)
        validated = validate_renpy_translation_proposal(task, proposal)
        completed = complete_translation_batch(
            task,
            started,
            batch.batch_id,
            (validated,),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            database = root / "translation.sqlite3"

            with TranslationStore(database, input_project_root=input_project) as store:
                store.initialize_task(task)
                store.commit_checkpoint(task, initial, started)
                store.commit_checkpoint(
                    task,
                    started,
                    completed,
                    proposals=(proposal,),
                    proposal_validator=validate_renpy_translation_proposal,
                )

            with TranslationStore(database, input_project_root=input_project) as store:
                recovered = store.load_task(task.task_id)
                proposals = store.load_accepted_proposals(task.task_id)

        self.assertEqual(recovered.checkpoint, completed)
        self.assertEqual(proposals, (proposal,))

    def test_invalid_or_missing_proposal_rolls_back_checkpoint(self) -> None:
        task = _task()
        initial = new_translation_checkpoint(task)
        batch = task.batches[0]
        started = start_translation_batch(task, initial, batch.batch_id)
        valid_proposal = _proposal(task)
        validated = validate_renpy_translation_proposal(task, valid_proposal)
        completed = complete_translation_batch(
            task,
            started,
            batch.batch_id,
            (validated,),
        )
        invalid_proposal = replace(valid_proposal, target_text="你好")
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            database = root / "translation.sqlite3"

            with TranslationStore(database, input_project_root=input_project) as store:
                store.initialize_task(task)
                store.commit_checkpoint(task, initial, started)
                with self.assertRaisesRegex(TranslationStorageError, "提案验证失败"):
                    store.commit_checkpoint(
                        task,
                        started,
                        completed,
                        proposals=(invalid_proposal,),
                        proposal_validator=validate_renpy_translation_proposal,
                    )
                self.assertEqual(store.load_task(task.task_id).checkpoint, started)
                self.assertEqual(store.load_accepted_proposals(task.task_id), ())

                with self.assertRaisesRegex(TranslationStorageError, "新增提案"):
                    store.commit_checkpoint(task, started, completed)
                self.assertEqual(store.load_task(task.task_id).checkpoint, started)

                def forged_validator(
                    candidate_task: TranslationTask,
                    candidate: TranslationProposal,
                ) -> ValidatedTranslation:
                    result = validate_renpy_translation_proposal(
                        candidate_task,
                        candidate,
                    )
                    return replace(result, segment_id="seg_wrong")

                with self.assertRaisesRegex(TranslationStorageError, "来源记录"):
                    store.commit_checkpoint(
                        task,
                        started,
                        completed,
                        proposals=(valid_proposal,),
                        proposal_validator=forged_validator,
                    )
                self.assertEqual(store.load_task(task.task_id).checkpoint, started)
                self.assertEqual(store.load_accepted_proposals(task.task_id), ())

    def test_stale_checkpoint_cannot_overwrite_newer_state(self) -> None:
        task = _task()
        initial = new_translation_checkpoint(task)
        started = start_translation_batch(task, initial, task.batches[0].batch_id)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            input_project = root / "input"
            input_project.mkdir()
            database = root / "translation.sqlite3"

            with (
                TranslationStore(database, input_project_root=input_project) as first,
                TranslationStore(database, input_project_root=input_project) as second,
            ):
                first.initialize_task(task)
                second_view = second.load_task(task.task_id).checkpoint
                first.commit_checkpoint(task, initial, started)
                with self.assertRaisesRegex(TranslationStorageError, "已被其他执行更新"):
                    second.commit_checkpoint(task, second_view, started)
                current = second.load_task(task.task_id).checkpoint

        self.assertEqual(current, started)

    def test_initialize_is_idempotent_and_does_not_reset_progress(self) -> None:
        task = _task()
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
                repeated = store.initialize_task(task)

        self.assertEqual(repeated, started)


if __name__ == "__main__":
    unittest.main()
