from __future__ import annotations

import unittest
from dataclasses import replace

from galtrans.adapters.renpy import (
    RenpyProposalPreparationError,
    RenpyTemplateMapping,
    prepare_renpy_translation_files,
    validate_renpy_translation_proposal,
)
from galtrans.adapters.renpy.extractor import (
    find_renpy_protected_tokens,
    find_renpy_string_literals,
)
from galtrans.ir import SegmentKind, TextSegment
from galtrans.translation import (
    TRANSLATION_PROPOSAL_SCHEMA_VERSION,
    TranslationProposal,
    TranslationTask,
    complete_translation_batch,
    create_translation_task,
    new_translation_checkpoint,
    start_translation_batch,
)


def _segments() -> tuple[TextSegment, ...]:
    return (
        TextSegment(
            schema_version=1,
            id="seg_dialogue",
            engine="renpy",
            source_file="game/script.rpy",
            source_encoding="utf-8",
            source_sha256="a" * 64,
            line_number=2,
            scene="start",
            kind=SegmentKind.DIALOGUE,
            speaker="aoi",
            speaker_display="葵",
            source_text="Hello, [name]",
            protected_tokens=find_renpy_protected_tokens("Hello, [name]"),
        ),
        TextSegment(
            schema_version=1,
            id="seg_menu",
            engine="renpy",
            source_file="game/script.rpy",
            source_encoding="utf-8",
            source_sha256="a" * 64,
            line_number=4,
            scene="start",
            kind=SegmentKind.MENU_CHOICE,
            speaker=None,
            speaker_display=None,
            source_text="Yes",
            protected_tokens=(),
        ),
    )


def _proposal(
    task: TranslationTask,
    segment_id: str,
    target_text: str,
) -> TranslationProposal:
    batch = next(
        batch
        for batch in task.batches
        if any(segment.segment_id == segment_id for segment in batch.segments)
    )
    segment = next(
        segment for segment in batch.segments if segment.segment_id == segment_id
    )
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


def _mapping(segment: TextSegment) -> RenpyTemplateMapping:
    source_code = (
        f'aoi "{segment.source_text}"'
        if segment.kind is SegmentKind.DIALOGUE
        else f'"{segment.source_text}"'
    )
    literal = find_renpy_string_literals(source_code)[0]
    return RenpyTemplateMapping(
        segment_id=segment.id,
        source_file=segment.source_file,
        line_number=segment.line_number,
        kind=segment.kind,
        source_text=segment.source_text,
        template_file="script.rpy",
        translation_identifier=(
            "start_dialogue" if segment.kind is SegmentKind.DIALOGUE else None
        ),
        source_code=source_code,
        literal_start=literal.start,
        literal_end=literal.end,
        protected_tokens=tuple(token.value for token in segment.protected_tokens),
    )


def _completed_inputs():
    segments = _segments()
    task = create_translation_task(
        segments,
        source_language="english",
        target_language="schinese",
        batch_size=1,
    )
    proposals = (
        _proposal(task, "seg_dialogue", "你好，[name]"),
        _proposal(task, "seg_menu", "是"),
    )
    checkpoint = new_translation_checkpoint(task)
    proposals_by_segment = {proposal.segment_id: proposal for proposal in proposals}
    for batch in task.batches:
        checkpoint = start_translation_batch(
            task,
            checkpoint,
            batch.batch_id,
        )
        checkpoint = complete_translation_batch(
            task,
            checkpoint,
            batch.batch_id,
            tuple(
                validate_renpy_translation_proposal(
                    task,
                    proposals_by_segment[segment.segment_id],
                )
                for segment in batch.segments
            ),
        )
    mappings = tuple(_mapping(segment) for segment in segments)
    return segments, task, checkpoint, proposals, mappings


class RenpyProposalPreparationTests(unittest.TestCase):
    def test_prepares_completed_proposals_as_official_files_in_memory(self) -> None:
        segments, task, checkpoint, proposals, mappings = _completed_inputs()

        files = prepare_renpy_translation_files(
            segments,
            task,
            checkpoint,
            proposals,
            mappings,
        )

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].relative_path, "game/tl/schinese/script.rpy")
        self.assertEqual(files[0].segment_ids, ("seg_dialogue", "seg_menu"))
        self.assertIn('aoi "你好，[name]"', files[0].content)
        self.assertIn('new "是"', files[0].content)

    def test_rejects_unfinished_or_checkpoint_conflicting_proposals(self) -> None:
        segments, task, checkpoint, proposals, mappings = _completed_inputs()
        with self.assertRaisesRegex(RenpyProposalPreparationError, "只有已完成"):
            prepare_renpy_translation_files(
                segments,
                task,
                new_translation_checkpoint(task),
                proposals,
                mappings,
            )

        with self.assertRaisesRegex(RenpyProposalPreparationError, "完整且唯一"):
            prepare_renpy_translation_files(
                segments,
                task,
                checkpoint,
                proposals[:1],
                mappings,
            )

        invalid = (_proposal(task, "seg_dialogue", "你好"), proposals[1])
        with self.assertRaisesRegex(RenpyProposalPreparationError, "当前验证"):
            prepare_renpy_translation_files(
                segments,
                task,
                checkpoint,
                invalid,
                mappings,
            )

        conflicting = (
            _proposal(task, "seg_dialogue", "您好，[name]"),
            proposals[1],
        )
        with self.assertRaisesRegex(RenpyProposalPreparationError, "检查点不一致"):
            prepare_renpy_translation_files(
                segments,
                task,
                checkpoint,
                conflicting,
                mappings,
            )

    def test_rejects_stale_sources_or_incomplete_sdk_mappings(self) -> None:
        segments, task, checkpoint, proposals, mappings = _completed_inputs()
        stale_segments = (
            replace(segments[0], source_sha256="b" * 64),
            segments[1],
        )
        with self.assertRaisesRegex(RenpyProposalPreparationError, "任务身份不一致"):
            prepare_renpy_translation_files(
                stale_segments,
                task,
                checkpoint,
                proposals,
                mappings,
            )

        with self.assertRaisesRegex(RenpyProposalPreparationError, "完整且唯一"):
            prepare_renpy_translation_files(
                segments,
                task,
                checkpoint,
                proposals,
                mappings[:1],
            )

        with self.assertRaisesRegex(RenpyProposalPreparationError, "重复 SDK 映射"):
            prepare_renpy_translation_files(
                segments,
                task,
                checkpoint,
                proposals,
                (mappings[0], *mappings),
            )

    def test_rejects_sdk_mapping_that_disagrees_with_source_evidence(self) -> None:
        segments, task, checkpoint, proposals, mappings = _completed_inputs()
        changed_mappings = (replace(mappings[0], line_number=99), mappings[1])

        with self.assertRaisesRegex(RenpyProposalPreparationError, "证据不一致"):
            prepare_renpy_translation_files(
                segments,
                task,
                checkpoint,
                proposals,
                changed_mappings,
            )


if __name__ == "__main__":
    unittest.main()
