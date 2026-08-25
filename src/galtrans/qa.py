from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from galtrans.translation import (
    TRANSLATION_PROPOSAL_SCHEMA_VERSION,
    TranslationProposal,
    TranslationSchemaError,
    TranslationSegmentRequest,
    TranslationTask,
    ValidatedTranslation,
    translation_proposal_id,
)

TRANSLATION_QUALITY_REPORT_SCHEMA_VERSION = 1
UNCHANGED_SOURCE_TEXT_CHECK_ID = "unchanged_source_text_v1"

_PROPOSAL_ID_RE = re.compile(r"^proposal_[0-9a-f]{24}$")
_TASK_ID_RE = re.compile(r"^task_[0-9a-f]{24}$")
_SUPPORTED_CHECK_IDS = (UNCHANGED_SOURCE_TEXT_CHECK_ID,)


class TranslationQualitySchemaError(ValueError):
    """Raised when serialized quality data does not match the closed schema."""


class TranslationQualityValidationError(ValueError):
    """Raised when quality inputs do not exactly cover one translation task."""


class TranslationQualityOutcome(StrEnum):
    CLEAR = "clear"
    LOW_CONFIDENCE = "low_confidence"


class TranslationQualityIssueCode(StrEnum):
    SOURCE_TEXT_UNCHANGED = "source_text_unchanged"


def _expect_keys(
    value: Mapping[str, Any],
    expected: set[str],
    *,
    location: str,
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise TranslationQualitySchemaError(
            f"{location} 字段不匹配；缺少 {missing}；额外 {extra}"
        )


def _expect_mapping(value: object, *, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TranslationQualitySchemaError(f"{location} 必须是字符串键对象")
    return value


def _expect_string(value: object, *, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise TranslationQualitySchemaError(f"{location} 必须是非空字符串")
    return value


@dataclass(frozen=True, slots=True)
class TranslationQualityFinding:
    check_id: str
    code: TranslationQualityIssueCode

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "code": self.code.value,
        }

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, Any],
        *,
        location: str,
    ) -> TranslationQualityFinding:
        _expect_keys(raw, {"check_id", "code"}, location=location)
        check_id = _expect_string(raw["check_id"], location=f"{location}.check_id")
        code_value = _expect_string(raw["code"], location=f"{location}.code")
        try:
            code = TranslationQualityIssueCode(code_value)
        except ValueError as error:
            raise TranslationQualitySchemaError(
                f"{location}.code 不是受支持的质量问题：{code_value}"
            ) from error
        if (
            check_id != UNCHANGED_SOURCE_TEXT_CHECK_ID
            or code is not TranslationQualityIssueCode.SOURCE_TEXT_UNCHANGED
        ):
            raise TranslationQualitySchemaError(
                f"{location} 的检查 ID 与质量问题不匹配"
            )
        return cls(check_id=check_id, code=code)


@dataclass(frozen=True, slots=True)
class TranslationQualityResult:
    segment_id: str
    proposal_id: str
    outcome: TranslationQualityOutcome
    findings: tuple[TranslationQualityFinding, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "proposal_id": self.proposal_id,
            "outcome": self.outcome.value,
            "findings": [finding.to_dict() for finding in self.findings],
        }

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, Any],
        *,
        location: str,
    ) -> TranslationQualityResult:
        _expect_keys(
            raw,
            {"segment_id", "proposal_id", "outcome", "findings"},
            location=location,
        )
        segment_id = _expect_string(raw["segment_id"], location=f"{location}.segment_id")
        proposal_id = _expect_string(
            raw["proposal_id"], location=f"{location}.proposal_id"
        )
        if _PROPOSAL_ID_RE.fullmatch(proposal_id) is None:
            raise TranslationQualitySchemaError(f"{location}.proposal_id 格式无效")
        outcome_value = _expect_string(raw["outcome"], location=f"{location}.outcome")
        try:
            outcome = TranslationQualityOutcome(outcome_value)
        except ValueError as error:
            raise TranslationQualitySchemaError(
                f"{location}.outcome 不是受支持的质量结果：{outcome_value}"
            ) from error
        raw_findings = raw["findings"]
        if not isinstance(raw_findings, list):
            raise TranslationQualitySchemaError(f"{location}.findings 必须是数组")
        findings = tuple(
            TranslationQualityFinding.from_dict(
                _expect_mapping(item, location=f"{location}.findings[{index}]"),
                location=f"{location}.findings[{index}]",
            )
            for index, item in enumerate(raw_findings)
        )
        if len({(finding.check_id, finding.code) for finding in findings}) != len(findings):
            raise TranslationQualitySchemaError(f"{location}.findings 包含重复问题")
        if outcome is TranslationQualityOutcome.CLEAR and findings:
            raise TranslationQualitySchemaError("clear 质量结果不能包含问题")
        if outcome is TranslationQualityOutcome.LOW_CONFIDENCE and not findings:
            raise TranslationQualitySchemaError("low_confidence 质量结果必须包含问题")
        return cls(
            segment_id=segment_id,
            proposal_id=proposal_id,
            outcome=outcome,
            findings=findings,
        )


@dataclass(frozen=True, slots=True)
class TranslationQualityReport:
    schema_version: int
    task_id: str
    check_ids: tuple[str, ...]
    results: tuple[TranslationQualityResult, ...]

    @property
    def low_confidence_results(self) -> tuple[TranslationQualityResult, ...]:
        return tuple(
            result
            for result in self.results
            if result.outcome is TranslationQualityOutcome.LOW_CONFIDENCE
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "check_ids": list(self.check_ids),
            "results": [result.to_dict() for result in self.results],
        }

    @classmethod
    def from_dict(
        cls,
        task: TranslationTask,
        raw: Mapping[str, Any],
    ) -> TranslationQualityReport:
        if not isinstance(task, TranslationTask):
            raise TranslationQualitySchemaError("译文质量报告的任务类型无效")
        try:
            checked_task = TranslationTask.from_dict(task.to_dict())
        except TranslationSchemaError as error:
            raise TranslationQualitySchemaError(
                f"译文质量报告的任务无效：{error}"
            ) from error
        if checked_task != task:
            raise TranslationQualitySchemaError("译文质量报告的任务序列化往返不一致")
        _expect_keys(
            raw,
            {"schema_version", "task_id", "check_ids", "results"},
            location="quality report",
        )
        schema_version = raw["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise TranslationQualitySchemaError(
                "quality report.schema_version 必须是整数"
            )
        if schema_version != TRANSLATION_QUALITY_REPORT_SCHEMA_VERSION:
            raise TranslationQualitySchemaError(
                f"不支持的译文质量报告 schema 版本：{schema_version}"
            )
        task_id = _expect_string(raw["task_id"], location="quality report.task_id")
        if _TASK_ID_RE.fullmatch(task_id) is None:
            raise TranslationQualitySchemaError("quality report.task_id 格式无效")
        if task_id != task.task_id:
            raise TranslationQualitySchemaError("quality report.task_id 与当前任务不一致")
        raw_check_ids = raw["check_ids"]
        if not isinstance(raw_check_ids, list) or any(
            not isinstance(check_id, str) for check_id in raw_check_ids
        ):
            raise TranslationQualitySchemaError("quality report.check_ids 必须是字符串数组")
        check_ids = tuple(raw_check_ids)
        if check_ids != _SUPPORTED_CHECK_IDS:
            raise TranslationQualitySchemaError(
                "quality report.check_ids 与当前 schema 支持的检查集合不一致"
            )
        raw_results = raw["results"]
        if not isinstance(raw_results, list) or not raw_results:
            raise TranslationQualitySchemaError(
                "quality report.results 必须是非空数组"
            )
        results = tuple(
            TranslationQualityResult.from_dict(
                _expect_mapping(item, location=f"quality report.results[{index}]"),
                location=f"quality report.results[{index}]",
            )
            for index, item in enumerate(raw_results)
        )
        if len({result.segment_id for result in results}) != len(results):
            raise TranslationQualitySchemaError("quality report.results 包含重复文本段")
        if len({result.proposal_id for result in results}) != len(results):
            raise TranslationQualitySchemaError("quality report.results 包含重复提案")
        expected_segment_ids = tuple(
            segment.segment_id for batch in task.batches for segment in batch.segments
        )
        if tuple(result.segment_id for result in results) != expected_segment_ids:
            raise TranslationQualitySchemaError(
                "quality report.results 未按任务顺序完整覆盖文本段"
            )
        return cls(
            schema_version=schema_version,
            task_id=task_id,
            check_ids=check_ids,
            results=results,
        )


def _validate_translation_for_segment(
    task: TranslationTask,
    batch_id: str,
    segment: TranslationSegmentRequest,
    translation: ValidatedTranslation,
) -> None:
    if translation.task_id != task.task_id:
        raise TranslationQualityValidationError(
            f"{translation.segment_id} 的质量检查任务身份不一致"
        )
    if translation.batch_id != batch_id:
        raise TranslationQualityValidationError(
            f"{translation.segment_id} 的质量检查批次身份不一致"
        )
    if translation.source_sha256 != segment.source_sha256:
        raise TranslationQualityValidationError(
            f"{translation.segment_id} 的质量检查来源摘要不一致"
        )
    if translation.target_language != task.target_language:
        raise TranslationQualityValidationError(
            f"{translation.segment_id} 的质量检查目标语言不一致"
        )
    try:
        proposal = TranslationProposal.from_dict(
            {
                "schema_version": TRANSLATION_PROPOSAL_SCHEMA_VERSION,
                "task_id": task.task_id,
                "batch_id": batch_id,
                "segment_id": segment.segment_id,
                "source_schema_version": segment.source_schema_version,
                "source_sha256": segment.source_sha256,
                "target_language": task.target_language,
                "protected_tokens": [
                    token.to_dict() for token in segment.protected_tokens
                ],
                "target_text": translation.target_text,
            }
        )
    except TranslationSchemaError as error:
        raise TranslationQualityValidationError(
            f"{translation.segment_id} 的质量检查译文结构无效：{error}"
        ) from error
    if translation.proposal_id != translation_proposal_id(proposal):
        raise TranslationQualityValidationError(
            f"{translation.segment_id} 的质量检查提案摘要与译文不一致"
        )


def assess_translation_quality(
    task: TranslationTask,
    translations: Iterable[ValidatedTranslation],
) -> TranslationQualityReport:
    """Run deterministic checks over one complete set of engine-validated translations."""
    if not isinstance(task, TranslationTask):
        raise TranslationQualityValidationError("译文质量检查的任务类型无效")
    try:
        checked_task = TranslationTask.from_dict(task.to_dict())
    except TranslationSchemaError as error:
        raise TranslationQualityValidationError(
            f"译文质量检查的任务无效：{error}"
        ) from error
    if checked_task != task:
        raise TranslationQualityValidationError("译文质量检查的任务序列化往返不一致")

    checked_translations = tuple(translations)
    if any(
        not isinstance(translation, ValidatedTranslation)
        for translation in checked_translations
    ):
        raise TranslationQualityValidationError("译文质量检查包含无效的译文类型")
    translations_by_segment: dict[str, ValidatedTranslation] = {}
    for translation in checked_translations:
        if translation.segment_id in translations_by_segment:
            raise TranslationQualityValidationError(
                f"译文质量检查包含重复文本段：{translation.segment_id}"
            )
        translations_by_segment[translation.segment_id] = translation

    expected_segment_ids = tuple(
        segment.segment_id for batch in task.batches for segment in batch.segments
    )
    if set(translations_by_segment) != set(expected_segment_ids):
        raise TranslationQualityValidationError(
            "译文质量检查结果没有完整且唯一地覆盖任务文本段"
        )

    results: list[TranslationQualityResult] = []
    for batch in task.batches:
        for segment in batch.segments:
            translation = translations_by_segment[segment.segment_id]
            _validate_translation_for_segment(
                task,
                batch.batch_id,
                segment,
                translation,
            )
            findings: tuple[TranslationQualityFinding, ...] = ()
            if unicodedata.normalize("NFC", translation.target_text) == unicodedata.normalize(
                "NFC", segment.source_text
            ):
                findings = (
                    TranslationQualityFinding(
                        check_id=UNCHANGED_SOURCE_TEXT_CHECK_ID,
                        code=TranslationQualityIssueCode.SOURCE_TEXT_UNCHANGED,
                    ),
                )
            outcome = (
                TranslationQualityOutcome.LOW_CONFIDENCE
                if findings
                else TranslationQualityOutcome.CLEAR
            )
            results.append(
                TranslationQualityResult(
                    segment_id=segment.segment_id,
                    proposal_id=translation.proposal_id,
                    outcome=outcome,
                    findings=findings,
                )
            )

    return TranslationQualityReport(
        schema_version=TRANSLATION_QUALITY_REPORT_SCHEMA_VERSION,
        task_id=task.task_id,
        check_ids=_SUPPORTED_CHECK_IDS,
        results=tuple(results),
    )
