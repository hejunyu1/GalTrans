from __future__ import annotations

import unittest
from dataclasses import replace

from galtrans.adapters.renpy import validate_renpy_translation_proposal
from galtrans.adapters.renpy.extractor import find_renpy_protected_tokens
from galtrans.ir import SegmentKind, TextSegment
from galtrans.qa import (
    TRANSLATION_QUALITY_REPORT_SCHEMA_VERSION,
    UNCHANGED_SOURCE_TEXT_CHECK_ID,
    TranslationQualityIssueCode,
    TranslationQualityOutcome,
    TranslationQualityReport,
    TranslationQualitySchemaError,
    TranslationQualityValidationError,
    assess_translation_quality,
)
from galtrans.translation import (
    TRANSLATION_PROPOSAL_SCHEMA_VERSION,
    TranslationProposal,
    TranslationTask,
    ValidatedTranslation,
    create_translation_task,
)


def _segment(segment_id: str, source_text: str) -> TextSegment:
    return TextSegment(
        schema_version=1,
        id=segment_id,
        engine="renpy",
        source_file="game/script.rpy",
        source_encoding="utf-8",
        source_sha256="a" * 64,
        line_number=8,
        scene="start",
        kind=SegmentKind.DIALOGUE,
        speaker="aoi",
        speaker_display="葵",
        source_text=source_text,
        protected_tokens=find_renpy_protected_tokens(source_text),
    )


def _validated(
    task: TranslationTask,
    segment_id: str,
    target_text: str,
) -> ValidatedTranslation:
    batch = next(
        batch
        for batch in task.batches
        if any(segment.segment_id == segment_id for segment in batch.segments)
    )
    segment = next(
        segment for segment in batch.segments if segment.segment_id == segment_id
    )
    proposal = TranslationProposal.from_dict(
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
    return validate_renpy_translation_proposal(task, proposal)


class TranslationQualityTests(unittest.TestCase):
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
        self.unchanged = _validated(
            self.task,
            "seg_one",
            "こんにちは、[player_name]",
        )
        self.changed = _validated(self.task, "seg_two", "再见")

    def test_reports_unchanged_text_as_low_confidence_in_task_order(self) -> None:
        report = assess_translation_quality(
            self.task,
            (self.changed, self.unchanged),
        )

        self.assertEqual(
            report.schema_version,
            TRANSLATION_QUALITY_REPORT_SCHEMA_VERSION,
        )
        self.assertEqual(report.check_ids, (UNCHANGED_SOURCE_TEXT_CHECK_ID,))
        self.assertEqual(
            [result.segment_id for result in report.results],
            ["seg_one", "seg_two"],
        )
        self.assertEqual(
            report.results[0].outcome,
            TranslationQualityOutcome.LOW_CONFIDENCE,
        )
        self.assertEqual(
            report.results[0].findings[0].code,
            TranslationQualityIssueCode.SOURCE_TEXT_UNCHANGED,
        )
        self.assertEqual(
            report.results[1].outcome,
            TranslationQualityOutcome.CLEAR,
        )
        self.assertEqual(report.results[1].findings, ())
        self.assertEqual(report.low_confidence_results, (report.results[0],))
        self.assertEqual(
            TranslationQualityReport.from_dict(self.task, report.to_dict()),
            report,
        )
        self.assertEqual(
            assess_translation_quality(
                self.task,
                (self.unchanged, self.changed),
            ),
            report,
        )

    def test_unchanged_check_uses_unicode_nfc_without_whitespace_guessing(self) -> None:
        segment = _segment("seg_unicode", "caf\u00e9")
        task = create_translation_task(
            (segment,),
            source_language="fr",
            target_language="en",
            batch_size=1,
        )

        canonically_equal = assess_translation_quality(
            task,
            (_validated(task, "seg_unicode", "cafe\u0301"),),
        )
        whitespace_changed = assess_translation_quality(
            task,
            (_validated(task, "seg_unicode", " caf\u00e9"),),
        )

        self.assertEqual(
            canonically_equal.results[0].outcome,
            TranslationQualityOutcome.LOW_CONFIDENCE,
        )
        self.assertEqual(
            whitespace_changed.results[0].outcome,
            TranslationQualityOutcome.CLEAR,
        )

    def test_quality_report_schema_is_closed_versioned_and_consistent(self) -> None:
        report = assess_translation_quality(
            self.task,
            (self.unchanged, self.changed),
        )
        raw = report.to_dict()

        with self.assertRaisesRegex(TranslationQualitySchemaError, "字段"):
            TranslationQualityReport.from_dict(self.task, {**raw, "extra": True})
        with self.assertRaisesRegex(TranslationQualitySchemaError, "schema"):
            TranslationQualityReport.from_dict(
                self.task,
                {**raw, "schema_version": 2},
            )
        with self.assertRaisesRegex(TranslationQualitySchemaError, "检查集合"):
            TranslationQualityReport.from_dict(self.task, {**raw, "check_ids": []})

        inconsistent = report.to_dict()
        inconsistent["results"][0]["findings"] = []
        with self.assertRaisesRegex(TranslationQualitySchemaError, "必须包含问题"):
            TranslationQualityReport.from_dict(self.task, inconsistent)

        unknown_issue = report.to_dict()
        unknown_issue["results"][0]["findings"][0]["code"] = "unknown"
        with self.assertRaisesRegex(TranslationQualitySchemaError, "质量问题"):
            TranslationQualityReport.from_dict(self.task, unknown_issue)

        incomplete = report.to_dict()
        incomplete["results"] = incomplete["results"][:1]
        with self.assertRaisesRegex(TranslationQualitySchemaError, "完整覆盖"):
            TranslationQualityReport.from_dict(self.task, incomplete)

    def test_rejects_incomplete_duplicate_or_mismatched_translations(self) -> None:
        with self.assertRaisesRegex(TranslationQualityValidationError, "完整且唯一"):
            assess_translation_quality(self.task, (self.unchanged,))
        with self.assertRaisesRegex(TranslationQualityValidationError, "重复文本段"):
            assess_translation_quality(
                self.task,
                (self.unchanged, self.unchanged, self.changed),
            )

        mismatches = (
            replace(self.unchanged, task_id="task_wrong"),
            replace(self.unchanged, batch_id=self.task.batches[1].batch_id),
            replace(self.unchanged, source_sha256="b" * 64),
            replace(self.unchanged, target_language="en"),
            replace(self.unchanged, target_text="changed without a new proposal digest"),
        )
        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch):
                with self.assertRaises(TranslationQualityValidationError):
                    assess_translation_quality(self.task, (mismatch, self.changed))


if __name__ == "__main__":
    unittest.main()
