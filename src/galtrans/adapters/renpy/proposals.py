from __future__ import annotations

from collections.abc import Iterable

from galtrans.adapters.renpy.exporter import (
    RenderedRenpyFile,
    assemble_official_translation_files,
)
from galtrans.adapters.renpy.extractor import find_renpy_protected_tokens
from galtrans.adapters.renpy.renderer import render_official_translation_fragment
from galtrans.adapters.renpy.sdk import RenpyTemplateMapping
from galtrans.ir import TextSegment
from galtrans.translation import (
    TranslationProposal,
    TranslationSchemaError,
    TranslationTask,
    TranslationTaskCheckpoint,
    TranslationTaskStatus,
    TranslationValidationError,
    ValidatedTranslation,
    create_translation_task,
    translation_checkpoint_from_dict,
    validate_translation_proposal,
)


class RenpyProposalPreparationError(ValueError):
    """Raised when accepted proposals cannot be tied to exact Ren'Py evidence."""


def validate_renpy_translation_proposal(
    task: TranslationTask,
    proposal: TranslationProposal,
) -> ValidatedTranslation:
    """Validate a proposal with Ren'Py's deterministic protected-token scanner."""
    return validate_translation_proposal(
        task,
        proposal,
        expected_engine="renpy",
        protected_token_finder=find_renpy_protected_tokens,
    )


def _checked_completed_task(
    source_segments: Iterable[TextSegment],
    task: TranslationTask,
    checkpoint: TranslationTaskCheckpoint,
) -> tuple[TextSegment, ...]:
    checked_segments = tuple(source_segments)
    if not checked_segments or any(
        not isinstance(segment, TextSegment) for segment in checked_segments
    ):
        raise RenpyProposalPreparationError(
            "Ren'Py 导出准备需要非空的原始文本段记录"
        )
    if not isinstance(task, TranslationTask):
        raise RenpyProposalPreparationError("Ren'Py 导出准备的翻译任务类型无效")
    if not isinstance(checkpoint, TranslationTaskCheckpoint):
        raise RenpyProposalPreparationError("Ren'Py 导出准备的检查点类型无效")

    try:
        rebuilt_task = create_translation_task(
            checked_segments,
            source_language=task.source_language,
            target_language=task.target_language,
            batch_size=task.batch_size,
        )
        checked_checkpoint = translation_checkpoint_from_dict(
            task,
            checkpoint.to_dict(),
        )
    except TranslationSchemaError as error:
        raise RenpyProposalPreparationError(
            f"Ren'Py 导出准备的任务或检查点无效：{error}"
        ) from error
    if rebuilt_task != task:
        raise RenpyProposalPreparationError(
            "原始文本段、语言或批策略与翻译任务身份不一致"
        )
    if checked_checkpoint != checkpoint:
        raise RenpyProposalPreparationError("翻译检查点序列化往返不一致")
    if checkpoint.status is not TranslationTaskStatus.COMPLETED:
        raise RenpyProposalPreparationError("只有已完成的翻译任务可以准备 Ren'Py 文件")
    return checked_segments


def _validated_proposals_by_segment(
    task: TranslationTask,
    checkpoint: TranslationTaskCheckpoint,
    proposals: Iterable[TranslationProposal],
) -> dict[str, ValidatedTranslation]:
    expected_segment_ids = tuple(
        segment.segment_id for batch in task.batches for segment in batch.segments
    )

    checked_proposals = tuple(proposals)
    if any(
        not isinstance(proposal, TranslationProposal)
        for proposal in checked_proposals
    ):
        raise RenpyProposalPreparationError("Ren'Py 导出准备包含无效的翻译提案类型")
    validated_by_segment: dict[str, ValidatedTranslation] = {}
    for proposal in checked_proposals:
        try:
            validated = validate_renpy_translation_proposal(task, proposal)
        except TranslationValidationError as error:
            raise RenpyProposalPreparationError(
                f"Ren'Py 导出准备的提案未通过当前验证：{error}"
            ) from error
        if validated.segment_id in validated_by_segment:
            raise RenpyProposalPreparationError(
                f"Ren'Py 导出准备包含重复文本段提案：{validated.segment_id}"
            )
        validated_by_segment[validated.segment_id] = validated

    if set(validated_by_segment) != set(expected_segment_ids):
        raise RenpyProposalPreparationError(
            "Ren'Py 导出准备的提案没有完整且唯一地覆盖任务文本段"
        )
    for task_batch, checkpoint_batch in zip(
        task.batches,
        checkpoint.batches,
        strict=True,
    ):
        proposal_ids = tuple(
            validated_by_segment[segment.segment_id].proposal_id
            for segment in task_batch.segments
        )
        if proposal_ids != checkpoint_batch.validated_proposal_ids:
            raise RenpyProposalPreparationError(
                f"批次 {task_batch.batch_id} 的提案与已完成检查点不一致"
            )
    return validated_by_segment


def _checked_mappings_by_segment(
    source_segments: tuple[TextSegment, ...],
    mappings: Iterable[RenpyTemplateMapping],
) -> dict[str, RenpyTemplateMapping]:
    expected_segment_ids = tuple(segment.id for segment in source_segments)

    checked_mappings = tuple(mappings)
    if any(
        not isinstance(mapping, RenpyTemplateMapping) for mapping in checked_mappings
    ):
        raise RenpyProposalPreparationError("Ren'Py 导出准备包含无效的 SDK 映射类型")
    mappings_by_segment: dict[str, RenpyTemplateMapping] = {}
    for mapping in checked_mappings:
        if mapping.segment_id in mappings_by_segment:
            raise RenpyProposalPreparationError(
                f"Ren'Py 导出准备包含重复 SDK 映射：{mapping.segment_id}"
            )
        mappings_by_segment[mapping.segment_id] = mapping
    if set(mappings_by_segment) != set(expected_segment_ids):
        raise RenpyProposalPreparationError(
            "Ren'Py SDK 映射没有完整且唯一地覆盖任务文本段"
        )
    for segment in source_segments:
        mapping = mappings_by_segment[segment.id]
        expected_tokens = tuple(token.value for token in segment.protected_tokens)
        if (
            mapping.source_file.replace("\\", "/")
            != segment.source_file.replace("\\", "/")
            or mapping.line_number != segment.line_number
            or mapping.kind is not segment.kind
            or mapping.source_text != segment.source_text
            or mapping.protected_tokens != expected_tokens
        ):
            raise RenpyProposalPreparationError(
                f"{segment.id} 的 SDK 映射与原始文本段证据不一致"
            )
    return mappings_by_segment


def prepare_renpy_translation_files(
    source_segments: Iterable[TextSegment],
    task: TranslationTask,
    checkpoint: TranslationTaskCheckpoint,
    proposals: Iterable[TranslationProposal],
    mappings: Iterable[RenpyTemplateMapping],
) -> tuple[RenderedRenpyFile, ...]:
    """Revalidate a completed task and prepare deterministic files in memory only."""
    checked_segments = _checked_completed_task(source_segments, task, checkpoint)
    validated_by_segment = _validated_proposals_by_segment(
        task,
        checkpoint,
        proposals,
    )
    mappings_by_segment = _checked_mappings_by_segment(
        checked_segments,
        mappings,
    )

    fragments = []
    for segment in checked_segments:
        mapping = mappings_by_segment[segment.id]
        fragments.append(
            render_official_translation_fragment(
                mapping,
                validated_by_segment[segment.id].target_text,
                language=task.target_language,
            )
        )
    return assemble_official_translation_files(
        fragments,
        language=task.target_language,
    )
