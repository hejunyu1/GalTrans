from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from galtrans.adapters.renpy import (
    RenderedRenpyFile,
    RenpyExportValidation,
    RenpyTemplateMapping,
    crosscheck_renpy_sdk,
    extract_renpy_path,
    prepare_renpy_translation_files,
    render_official_translation_fragment,
    validate_renpy_export,
    validate_renpy_translation_proposal,
    write_official_translation_directory,
)
from galtrans.pipeline import TranslationExecutionError, TranslationTaskRunner
from galtrans.qa import (
    TranslationQualityOutcome,
    assess_translation_quality,
)
from galtrans.storage import ProposalValidator, TranslationStore
from galtrans.translation import (
    ProviderRequestStatus,
    TranslationBackend,
    TranslationBatch,
    TranslationBatchCheckpoint,
    TranslationBatchStatus,
    TranslationProposal,
    TranslationTask,
    TranslationTaskCheckpoint,
    TranslationTaskStatus,
    ValidatedTranslation,
    create_translation_task,
)


class AutomatedRenpyTranslationError(RuntimeError):
    """Raised when an automatic source-only Ren'Py run cannot finish safely."""


@dataclass(frozen=True, slots=True)
class AutomatedRenpyTranslationResult:
    task_id: str
    segment_count: int
    batch_count: int
    quality_outcome: TranslationQualityOutcome
    low_confidence_segment_ids: tuple[str, ...]
    workspace_root: Path
    database_path: Path
    output_root: Path
    translation_files: tuple[Path, ...]
    sdk_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "segment_count": self.segment_count,
            "batch_count": self.batch_count,
            "quality_outcome": self.quality_outcome.value,
            "low_confidence_segment_ids": list(self.low_confidence_segment_ids),
            "workspace_root": str(self.workspace_root),
            "database_path": str(self.database_path),
            "output_root": str(self.output_root),
            "translation_files": [str(path) for path in self.translation_files],
            "sdk_version": self.sdk_version,
        }


def _roots_overlap(first: Path, second: Path) -> bool:
    return (
        first == second
        or first.is_relative_to(second)
        or second.is_relative_to(first)
    )


def _checked_paths(
    project_path: Path,
    workspace_path: Path,
    output_path: Path,
) -> tuple[Path, Path, Path]:
    project_root = project_path.expanduser().resolve()
    if not project_root.exists():
        raise FileNotFoundError(f"Ren'Py 项目路径不存在：{project_root}")
    if not project_root.is_dir():
        raise NotADirectoryError(f"Ren'Py 项目路径不是目录：{project_root}")

    expanded_workspace = workspace_path.expanduser()
    if expanded_workspace.is_symlink():
        raise AutomatedRenpyTranslationError(
            f"自动翻译工作区不能是符号链接：{expanded_workspace}"
        )
    workspace_root = expanded_workspace.resolve()
    if workspace_root.exists() and not workspace_root.is_dir():
        raise NotADirectoryError(f"自动翻译工作区不是目录：{workspace_root}")

    output_root = output_path.expanduser().resolve()
    if output_path.expanduser().is_symlink():
        raise AutomatedRenpyTranslationError(
            f"输出目录不能是符号链接：{output_path.expanduser()}"
        )
    if output_root.exists():
        raise FileExistsError(f"输出目录已存在，拒绝覆盖：{output_root}")
    if _roots_overlap(project_root, workspace_root):
        raise AutomatedRenpyTranslationError(
            f"自动翻译工作区不得与输入项目重叠：{workspace_root} / {project_root}"
        )
    if _roots_overlap(project_root, output_root):
        raise AutomatedRenpyTranslationError(
            f"输出目录不得与输入项目重叠：{output_root} / {project_root}"
        )
    if _roots_overlap(workspace_root, output_root):
        raise AutomatedRenpyTranslationError(
            f"自动翻译工作区不得与输出目录重叠：{workspace_root} / {output_root}"
        )
    return project_root, workspace_root, output_root


def _failed_batch(
    task: TranslationTask,
    checkpoint: TranslationTaskCheckpoint,
) -> tuple[TranslationBatch, TranslationBatchCheckpoint]:
    failed = next(
        (
            batch
            for batch in checkpoint.batches
            if batch.status is TranslationBatchStatus.FAILED
        ),
        None,
    )
    if failed is None:
        raise AutomatedRenpyTranslationError(
            "翻译任务标记为 failed，但没有失败批次"
        )
    batch = next(item for item in task.batches if item.batch_id == failed.batch_id)
    return batch, failed


def _export_safe_proposal_validator(
    mappings: Iterable[RenpyTemplateMapping],
    *,
    language: str,
) -> ProposalValidator:
    by_segment: dict[str, RenpyTemplateMapping] = {}
    for mapping in mappings:
        if mapping.segment_id in by_segment:
            raise AutomatedRenpyTranslationError(
                f"Ren'Py SDK 交叉验证包含重复映射：{mapping.segment_id}"
            )
        by_segment[mapping.segment_id] = mapping

    def validate(
        task: TranslationTask,
        proposal: TranslationProposal,
    ) -> ValidatedTranslation:
        validated = validate_renpy_translation_proposal(task, proposal)
        mapping = by_segment.get(validated.segment_id)
        if mapping is None:
            raise AutomatedRenpyTranslationError(
                f"Ren'Py SDK 交叉验证缺少映射：{validated.segment_id}"
            )
        render_official_translation_fragment(
            mapping,
            validated.target_text,
            language=language,
        )
        return validated

    return validate


def _complete_translation_task(
    store: TranslationStore,
    runner: TranslationTaskRunner,
    task: TranslationTask,
    *,
    backend_identity: str,
    max_definitive_attempts: int,
) -> TranslationTaskCheckpoint:
    progress_limit = len(task.batches) * (max_definitive_attempts + 3) + 4
    for _ in range(progress_limit):
        stored = store.load_task(task.task_id)
        checkpoint = stored.checkpoint
        if checkpoint.status is TranslationTaskStatus.COMPLETED:
            return checkpoint
        if checkpoint.status is TranslationTaskStatus.PAUSED:
            runner.resume_task(task.task_id)
            continue
        if checkpoint.status is TranslationTaskStatus.FAILED:
            batch, failed = _failed_batch(task, checkpoint)
            receipt = store.load_provider_receipt(
                task,
                batch,
                backend_identity,
            )
            if (
                receipt is None
                or receipt.status is not ProviderRequestStatus.FAILED
                or failed.attempts >= max_definitive_attempts
            ):
                detail = failed.last_error or "未知失败"
                raise AutomatedRenpyTranslationError(
                    f"翻译批次 {batch.batch_id} 无法自动恢复：{detail}"
                )
            runner.resume_task(task.task_id)
            continue
        running_batch = next(
            (
                batch
                for batch in checkpoint.batches
                if batch.status is TranslationBatchStatus.RUNNING
            ),
            None,
        )
        if running_batch is not None:
            try:
                recovered = runner.recover_interrupted_task(task.task_id)
            except TranslationExecutionError as error:
                raise AutomatedRenpyTranslationError(str(error)) from error
            if recovered.status is TranslationTaskStatus.RUNNING:
                raise AutomatedRenpyTranslationError(
                    "Provider 请求仍在进行，当前同步命令不会持续轮询"
                )
            continue
        try:
            runner.run_next_batch(task.task_id)
        except TranslationExecutionError as error:
            after = store.load_task(task.task_id).checkpoint
            if after.status is TranslationTaskStatus.FAILED:
                continue
            raise AutomatedRenpyTranslationError(str(error)) from error
    raise AutomatedRenpyTranslationError("自动翻译流程超过预期状态转换次数")


def _publish_validated_output(
    files: Iterable[RenderedRenpyFile],
    *,
    sdk_path: Path,
    project_root: Path,
    output_root: Path,
    language: str,
    sdk_timeout_seconds: float,
) -> tuple[tuple[Path, ...], RenpyExportValidation]:
    output_root.parent.mkdir(parents=True, exist_ok=True)
    staging_parent = Path(
        tempfile.mkdtemp(prefix=".galtrans-auto-", dir=output_root.parent)
    )
    staged_output = staging_parent / "export"
    try:
        written = write_official_translation_directory(
            files,
            staged_output,
            input_project_root=project_root,
        )
        validation = validate_renpy_export(
            sdk_path,
            project_root,
            written.root,
            language=language,
            timeout_seconds=sdk_timeout_seconds,
        )
        relative_files = tuple(
            path.relative_to(written.root) for path in written.files
        )
        if output_root.exists() or output_root.is_symlink():
            raise FileExistsError(f"输出目录已存在，拒绝覆盖：{output_root}")
        written.root.rename(output_root)
    finally:
        if staging_parent.exists():
            shutil.rmtree(staging_parent)
    return (
        tuple(output_root / relative_path for relative_path in relative_files),
        validation,
    )


def run_automated_renpy_translation(
    sdk_path: Path,
    project_path: Path,
    output_path: Path,
    workspace_path: Path,
    backend: TranslationBackend,
    *,
    backend_identity: str,
    source_language: str = "ja",
    target_language: str = "schinese",
    batch_size: int = 8,
    max_definitive_attempts: int = 2,
    sdk_timeout_seconds: float = 60.0,
) -> AutomatedRenpyTranslationResult:
    """Translate, quality-check, render, validate, and publish one new Ren'Py output."""
    if (
        isinstance(max_definitive_attempts, bool)
        or not isinstance(max_definitive_attempts, int)
        or not 1 <= max_definitive_attempts <= 5
    ):
        raise AutomatedRenpyTranslationError(
            "Provider 确定失败尝试次数必须是 1 到 5"
        )
    project_root, workspace_root, output_root = _checked_paths(
        project_path,
        workspace_path,
        output_path,
    )
    extraction_results = extract_renpy_path(project_root)
    segments = tuple(
        segment for result in extraction_results for segment in result.segments
    )
    if not segments:
        raise AutomatedRenpyTranslationError("Ren'Py 项目没有可翻译文本段")

    crosscheck = crosscheck_renpy_sdk(
        sdk_path,
        project_root,
        language=target_language,
        timeout_seconds=sdk_timeout_seconds,
    )
    if not crosscheck.matches:
        raise AutomatedRenpyTranslationError(
            "Ren'Py SDK 交叉验证不一致，自动翻译已在调用 Provider 前停止"
        )
    task = create_translation_task(
        segments,
        source_language=source_language,
        target_language=target_language,
        batch_size=batch_size,
    )
    proposal_validator = _export_safe_proposal_validator(
        crosscheck.mappings,
        language=target_language,
    )

    workspace_root.mkdir(parents=True, exist_ok=True)
    database_path = workspace_root / "translation.sqlite3"
    with TranslationStore(
        database_path,
        input_project_root=project_root,
    ) as store:
        store.initialize_task(task)
        runner = TranslationTaskRunner(
            store,
            backend,
            proposal_validator,
            backend_identity=backend_identity,
        )
        checkpoint = _complete_translation_task(
            store,
            runner,
            task,
            backend_identity=backend_identity,
            max_definitive_attempts=max_definitive_attempts,
        )
        proposals = store.load_accepted_proposals(task.task_id)
        validated = tuple(
            proposal_validator(task, proposal)
            for proposal in proposals
        )
        report = assess_translation_quality(task, validated)
        store.store_quality_report(
            task,
            report,
            proposal_validator,
        )

    rendered_files = prepare_renpy_translation_files(
        segments,
        task,
        checkpoint,
        proposals,
        crosscheck.mappings,
    )
    translation_files, validation = _publish_validated_output(
        rendered_files,
        sdk_path=sdk_path,
        project_root=project_root,
        output_root=output_root,
        language=target_language,
        sdk_timeout_seconds=sdk_timeout_seconds,
    )
    low_confidence_segment_ids = tuple(
        result.segment_id for result in report.low_confidence_results
    )
    quality_outcome = (
        TranslationQualityOutcome.LOW_CONFIDENCE
        if low_confidence_segment_ids
        else TranslationQualityOutcome.CLEAR
    )
    return AutomatedRenpyTranslationResult(
        task_id=task.task_id,
        segment_count=task.segment_count,
        batch_count=len(task.batches),
        quality_outcome=quality_outcome,
        low_confidence_segment_ids=low_confidence_segment_ids,
        workspace_root=workspace_root,
        database_path=database_path,
        output_root=output_root,
        translation_files=translation_files,
        sdk_version=validation.version,
    )
