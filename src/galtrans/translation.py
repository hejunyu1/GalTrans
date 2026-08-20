from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from hashlib import sha256
from typing import Any, Protocol

from galtrans.ir import ProtectedToken, ProtectedTokenKind, SegmentKind, TextSegment


TRANSLATION_TASK_SCHEMA_VERSION = 1
TRANSLATION_PROPOSAL_SCHEMA_VERSION = 1
TRANSLATION_CHECKPOINT_SCHEMA_VERSION = 1

_LANGUAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class TranslationSchemaError(ValueError):
    """Raised when serialized translation data does not match the closed schema."""


class TranslationValidationError(ValueError):
    """Raised when a proposal does not match the task and source snapshot."""


class TranslationStateError(ValueError):
    """Raised when a task checkpoint transition is not allowed."""


class TranslationTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    COMPLETED = "completed"


class TranslationBatchStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    COMPLETED = "completed"


def _canonical_digest(prefix: str, value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{prefix}_{sha256(encoded).hexdigest()[:24]}"


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
        raise TranslationSchemaError(
            f"{location} 字段不匹配；缺少 {missing}；额外 {extra}"
        )


def _expect_mapping(value: object, *, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TranslationSchemaError(f"{location} 必须是字符串键对象")
    return value


def _expect_string(value: object, *, location: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise TranslationSchemaError(f"{location} 必须是非空字符串")
    return value


def _expect_optional_string(value: object, *, location: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise TranslationSchemaError(f"{location} 必须是字符串或 null")
    return value


def _expect_integer(value: object, *, location: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise TranslationSchemaError(f"{location} 必须是大于等于 {minimum} 的整数")
    return value


def _validate_language(language: str, *, location: str) -> None:
    if _LANGUAGE_RE.fullmatch(language) is None:
        raise TranslationSchemaError(
            f"{location} 只能包含英文字母、数字、下划线和连字符，且必须以字母开头"
        )


@dataclass(frozen=True, slots=True)
class ProtectedTokenReference:
    index: int
    kind: ProtectedTokenKind
    value: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind.value,
            "value": self.value,
        }

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, Any],
        *,
        location: str,
    ) -> ProtectedTokenReference:
        _expect_keys(raw, {"index", "kind", "value"}, location=location)
        index = _expect_integer(raw["index"], location=f"{location}.index")
        kind_value = _expect_string(raw["kind"], location=f"{location}.kind")
        try:
            kind = ProtectedTokenKind(kind_value)
        except ValueError as error:
            raise TranslationSchemaError(
                f"{location}.kind 不是受支持的标记类型：{kind_value}"
            ) from error
        value = _expect_string(raw["value"], location=f"{location}.value")
        return cls(index=index, kind=kind, value=value)


def _token_references(tokens: Iterable[ProtectedToken]) -> tuple[ProtectedTokenReference, ...]:
    return tuple(
        ProtectedTokenReference(index=token.index, kind=token.kind, value=token.value)
        for token in tokens
    )


def _parse_token_references(
    raw: object,
    *,
    location: str,
) -> tuple[ProtectedTokenReference, ...]:
    if not isinstance(raw, list):
        raise TranslationSchemaError(f"{location} 必须是数组")
    tokens = tuple(
        ProtectedTokenReference.from_dict(
            _expect_mapping(item, location=f"{location}[{index}]"),
            location=f"{location}[{index}]",
        )
        for index, item in enumerate(raw)
    )
    if tuple(token.index for token in tokens) != tuple(range(len(tokens))):
        raise TranslationSchemaError(f"{location} 的 index 必须从 0 连续递增")
    return tokens


@dataclass(frozen=True, slots=True)
class TranslationSegmentRequest:
    segment_id: str
    source_schema_version: int
    engine: str
    source_sha256: str
    kind: SegmentKind
    scene: str
    speaker: str | None
    speaker_display: str | None
    source_text: str
    protected_tokens: tuple[ProtectedTokenReference, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "source_schema_version": self.source_schema_version,
            "engine": self.engine,
            "source_sha256": self.source_sha256,
            "kind": self.kind.value,
            "scene": self.scene,
            "speaker": self.speaker,
            "speaker_display": self.speaker_display,
            "source_text": self.source_text,
            "protected_tokens": [token.to_dict() for token in self.protected_tokens],
        }

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, Any],
        *,
        location: str,
    ) -> TranslationSegmentRequest:
        _expect_keys(
            raw,
            {
                "segment_id",
                "source_schema_version",
                "engine",
                "source_sha256",
                "kind",
                "scene",
                "speaker",
                "speaker_display",
                "source_text",
                "protected_tokens",
            },
            location=location,
        )
        kind_value = _expect_string(raw["kind"], location=f"{location}.kind")
        try:
            kind = SegmentKind(kind_value)
        except ValueError as error:
            raise TranslationSchemaError(
                f"{location}.kind 不是受支持的文本类型：{kind_value}"
            ) from error
        request = cls(
            segment_id=_expect_string(raw["segment_id"], location=f"{location}.segment_id"),
            source_schema_version=_expect_integer(
                raw["source_schema_version"],
                location=f"{location}.source_schema_version",
                minimum=1,
            ),
            engine=_expect_string(raw["engine"], location=f"{location}.engine"),
            source_sha256=_expect_string(
                raw["source_sha256"], location=f"{location}.source_sha256"
            ),
            kind=kind,
            scene=_expect_string(raw["scene"], location=f"{location}.scene"),
            speaker=_expect_optional_string(raw["speaker"], location=f"{location}.speaker"),
            speaker_display=_expect_optional_string(
                raw["speaker_display"], location=f"{location}.speaker_display"
            ),
            source_text=_expect_string(
                raw["source_text"], location=f"{location}.source_text"
            ),
            protected_tokens=_parse_token_references(
                raw["protected_tokens"], location=f"{location}.protected_tokens"
            ),
        )
        _validate_request_segment(request)
        return request


@dataclass(frozen=True, slots=True)
class TranslationBatch:
    schema_version: int
    task_id: str
    batch_id: str
    index: int
    source_language: str
    target_language: str
    segments: tuple[TranslationSegmentRequest, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "batch_id": self.batch_id,
            "index": self.index,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "segments": [segment.to_dict() for segment in self.segments],
        }


@dataclass(frozen=True, slots=True)
class TranslationTask:
    schema_version: int
    task_id: str
    source_language: str
    target_language: str
    batch_size: int
    batches: tuple[TranslationBatch, ...]

    @property
    def segment_count(self) -> int:
        return sum(len(batch.segments) for batch in self.batches)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "batch_size": self.batch_size,
            "batches": [batch.to_dict() for batch in self.batches],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> TranslationTask:
        _expect_keys(
            raw,
            {
                "schema_version",
                "task_id",
                "source_language",
                "target_language",
                "batch_size",
                "batches",
            },
            location="task",
        )
        schema_version = _expect_integer(
            raw["schema_version"], location="task.schema_version", minimum=1
        )
        if schema_version != TRANSLATION_TASK_SCHEMA_VERSION:
            raise TranslationSchemaError(
                f"不支持的翻译任务 schema 版本：{schema_version}"
            )
        task_id = _expect_string(raw["task_id"], location="task.task_id")
        source_language = _expect_string(
            raw["source_language"], location="task.source_language"
        )
        target_language = _expect_string(
            raw["target_language"], location="task.target_language"
        )
        batch_size = _expect_integer(raw["batch_size"], location="task.batch_size", minimum=1)
        raw_batches = raw["batches"]
        if not isinstance(raw_batches, list) or not raw_batches:
            raise TranslationSchemaError("task.batches 必须是非空数组")

        requests: list[TranslationSegmentRequest] = []
        serialized_batch_ids: list[str] = []
        for batch_index, raw_batch_value in enumerate(raw_batches):
            location = f"task.batches[{batch_index}]"
            raw_batch = _expect_mapping(raw_batch_value, location=location)
            _expect_keys(
                raw_batch,
                {
                    "schema_version",
                    "task_id",
                    "batch_id",
                    "index",
                    "source_language",
                    "target_language",
                    "segments",
                },
                location=location,
            )
            serialized_schema_version = _expect_integer(
                raw_batch["schema_version"],
                location=f"{location}.schema_version",
                minimum=1,
            )
            if serialized_schema_version != schema_version:
                raise TranslationSchemaError(f"{location}.schema_version 与任务不一致")
            serialized_task_id = _expect_string(
                raw_batch["task_id"], location=f"{location}.task_id"
            )
            if serialized_task_id != task_id:
                raise TranslationSchemaError(f"{location}.task_id 与任务不一致")
            serialized_source_language = _expect_string(
                raw_batch["source_language"],
                location=f"{location}.source_language",
            )
            if serialized_source_language != source_language:
                raise TranslationSchemaError(f"{location}.source_language 与任务不一致")
            serialized_target_language = _expect_string(
                raw_batch["target_language"],
                location=f"{location}.target_language",
            )
            if serialized_target_language != target_language:
                raise TranslationSchemaError(f"{location}.target_language 与任务不一致")
            serialized_index = _expect_integer(
                raw_batch["index"], location=f"{location}.index"
            )
            if serialized_index != batch_index:
                raise TranslationSchemaError(f"{location}.index 必须按顺序连续递增")
            serialized_batch_ids.append(
                _expect_string(raw_batch["batch_id"], location=f"{location}.batch_id")
            )
            raw_segments = raw_batch["segments"]
            if not isinstance(raw_segments, list) or not raw_segments:
                raise TranslationSchemaError(f"{location}.segments 必须是非空数组")
            requests.extend(
                TranslationSegmentRequest.from_dict(
                    _expect_mapping(item, location=f"{location}.segments[{segment_index}]"),
                    location=f"{location}.segments[{segment_index}]",
                )
                for segment_index, item in enumerate(raw_segments)
            )

        rebuilt = _build_translation_task(
            requests,
            source_language=source_language,
            target_language=target_language,
            batch_size=batch_size,
        )
        if rebuilt.task_id != task_id:
            raise TranslationSchemaError("task.task_id 与任务内容摘要不一致")
        if tuple(batch.batch_id for batch in rebuilt.batches) != tuple(serialized_batch_ids):
            raise TranslationSchemaError("任务中的 batch_id 与批次内容摘要不一致")
        if len(rebuilt.batches) != len(raw_batches):
            raise TranslationSchemaError("任务批次边界与 batch_size 不一致")
        return rebuilt


def _validate_request_segment(request: TranslationSegmentRequest) -> None:
    if request.source_schema_version != 1:
        raise TranslationSchemaError(
            f"{request.segment_id} 使用不受支持的 IR schema：{request.source_schema_version}"
        )
    if _SHA256_RE.fullmatch(request.source_sha256) is None:
        raise TranslationSchemaError(f"{request.segment_id} 的来源 SHA-256 格式无效")
    if tuple(token.index for token in request.protected_tokens) != tuple(
        range(len(request.protected_tokens))
    ):
        raise TranslationSchemaError(f"{request.segment_id} 的受保护标记 index 不连续")


def _request_from_segment(segment: TextSegment) -> TranslationSegmentRequest:
    if tuple(token.index for token in segment.protected_tokens) != tuple(
        range(len(segment.protected_tokens))
    ):
        raise TranslationSchemaError(f"{segment.id} 的受保护标记 index 不连续")
    previous_end = 0
    for token in segment.protected_tokens:
        if (
            token.start < previous_end
            or token.start < 0
            or token.end <= token.start
            or token.end > len(segment.source_text)
            or segment.source_text[token.start : token.end] != token.value
        ):
            raise TranslationSchemaError(f"{segment.id} 的受保护标记位置与原文不一致")
        previous_end = token.end

    request = TranslationSegmentRequest(
        segment_id=segment.id,
        source_schema_version=segment.schema_version,
        engine=segment.engine,
        source_sha256=segment.source_sha256,
        kind=segment.kind,
        scene=segment.scene,
        speaker=segment.speaker,
        speaker_display=segment.speaker_display,
        source_text=segment.source_text,
        protected_tokens=_token_references(segment.protected_tokens),
    )
    _validate_request_segment(request)
    return request


def _build_translation_task(
    requests: Sequence[TranslationSegmentRequest],
    *,
    source_language: str,
    target_language: str,
    batch_size: int,
) -> TranslationTask:
    if not requests:
        raise TranslationSchemaError("翻译任务至少需要一个文本段")
    _validate_language(source_language, location="源语言")
    _validate_language(target_language, location="目标语言")
    if source_language == target_language:
        raise TranslationSchemaError("源语言与目标语言不能相同")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise TranslationSchemaError("批大小必须是大于等于 1 的整数")

    seen_ids: set[str] = set()
    for request in requests:
        _validate_request_segment(request)
        if request.segment_id in seen_ids:
            raise TranslationSchemaError(f"翻译任务包含重复文本段 ID：{request.segment_id}")
        seen_ids.add(request.segment_id)

    task_payload = {
        "schema_version": TRANSLATION_TASK_SCHEMA_VERSION,
        "source_language": source_language,
        "target_language": target_language,
        "batch_size": batch_size,
        "segments": [request.to_dict() for request in requests],
    }
    task_id = _canonical_digest("task", task_payload)
    batches: list[TranslationBatch] = []
    for index, start in enumerate(range(0, len(requests), batch_size)):
        batch_segments = tuple(requests[start : start + batch_size])
        batch_id = _canonical_digest(
            "batch",
            {
                "task_id": task_id,
                "index": index,
                "segment_ids": [segment.segment_id for segment in batch_segments],
            },
        )
        batches.append(
            TranslationBatch(
                schema_version=TRANSLATION_TASK_SCHEMA_VERSION,
                task_id=task_id,
                batch_id=batch_id,
                index=index,
                source_language=source_language,
                target_language=target_language,
                segments=batch_segments,
            )
        )
    return TranslationTask(
        schema_version=TRANSLATION_TASK_SCHEMA_VERSION,
        task_id=task_id,
        source_language=source_language,
        target_language=target_language,
        batch_size=batch_size,
        batches=tuple(batches),
    )


def create_translation_task(
    segments: Iterable[TextSegment],
    *,
    source_language: str,
    target_language: str,
    batch_size: int,
) -> TranslationTask:
    """Build a deterministic task containing only filtered text and local context."""
    return _build_translation_task(
        tuple(_request_from_segment(segment) for segment in segments),
        source_language=source_language,
        target_language=target_language,
        batch_size=batch_size,
    )


@dataclass(frozen=True, slots=True)
class TranslationProposal:
    schema_version: int
    task_id: str
    batch_id: str
    segment_id: str
    source_schema_version: int
    source_sha256: str
    target_language: str
    protected_tokens: tuple[ProtectedTokenReference, ...]
    target_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "batch_id": self.batch_id,
            "segment_id": self.segment_id,
            "source_schema_version": self.source_schema_version,
            "source_sha256": self.source_sha256,
            "target_language": self.target_language,
            "protected_tokens": [token.to_dict() for token in self.protected_tokens],
            "target_text": self.target_text,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> TranslationProposal:
        _expect_keys(
            raw,
            {
                "schema_version",
                "task_id",
                "batch_id",
                "segment_id",
                "source_schema_version",
                "source_sha256",
                "target_language",
                "protected_tokens",
                "target_text",
            },
            location="proposal",
        )
        schema_version = _expect_integer(
            raw["schema_version"], location="proposal.schema_version", minimum=1
        )
        if schema_version != TRANSLATION_PROPOSAL_SCHEMA_VERSION:
            raise TranslationSchemaError(
                f"不支持的翻译提案 schema 版本：{schema_version}"
            )
        proposal = cls(
            schema_version=schema_version,
            task_id=_expect_string(raw["task_id"], location="proposal.task_id"),
            batch_id=_expect_string(raw["batch_id"], location="proposal.batch_id"),
            segment_id=_expect_string(raw["segment_id"], location="proposal.segment_id"),
            source_schema_version=_expect_integer(
                raw["source_schema_version"],
                location="proposal.source_schema_version",
                minimum=1,
            ),
            source_sha256=_expect_string(
                raw["source_sha256"], location="proposal.source_sha256"
            ),
            target_language=_expect_string(
                raw["target_language"], location="proposal.target_language"
            ),
            protected_tokens=_parse_token_references(
                raw["protected_tokens"], location="proposal.protected_tokens"
            ),
            target_text=_expect_string(raw["target_text"], location="proposal.target_text"),
        )
        if _SHA256_RE.fullmatch(proposal.source_sha256) is None:
            raise TranslationSchemaError("proposal.source_sha256 格式无效")
        _validate_language(proposal.target_language, location="proposal.target_language")
        return proposal


@dataclass(frozen=True, slots=True)
class ValidatedTranslation:
    proposal_id: str
    task_id: str
    batch_id: str
    segment_id: str
    source_sha256: str
    target_language: str
    target_text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ProtectedTokenFinder = Callable[[str], Iterable[ProtectedToken]]


def validate_translation_proposal(
    task: TranslationTask,
    proposal: TranslationProposal,
    *,
    expected_engine: str,
    protected_token_finder: ProtectedTokenFinder,
) -> ValidatedTranslation:
    """Validate one provider proposal against an immutable task and engine token parser."""
    if proposal.schema_version != TRANSLATION_PROPOSAL_SCHEMA_VERSION:
        raise TranslationValidationError(
            f"不支持的翻译提案 schema 版本：{proposal.schema_version}"
        )
    if proposal.task_id != task.task_id:
        raise TranslationValidationError("提案 task_id 与当前任务不一致")
    batch = next((item for item in task.batches if item.batch_id == proposal.batch_id), None)
    if batch is None:
        raise TranslationValidationError("提案 batch_id 不属于当前任务")
    segment = next(
        (item for item in batch.segments if item.segment_id == proposal.segment_id),
        None,
    )
    if segment is None:
        raise TranslationValidationError("提案 segment_id 不属于指定批次")
    if segment.engine != expected_engine:
        raise TranslationValidationError(
            f"提案文本段引擎 {segment.engine} 不能由 {expected_engine} 适配器验证"
        )
    if proposal.source_schema_version != segment.source_schema_version:
        raise TranslationValidationError("提案 IR schema 版本与来源文本段不一致")
    if proposal.source_sha256 != segment.source_sha256:
        raise TranslationValidationError("提案来源 SHA-256 与当前文本段不一致")
    if proposal.target_language != task.target_language:
        raise TranslationValidationError("提案目标语言与当前任务不一致")
    if proposal.protected_tokens != segment.protected_tokens:
        raise TranslationValidationError("提案回显的受保护标记与来源文本段不一致")

    target_tokens = _token_references(protected_token_finder(proposal.target_text))
    if target_tokens != segment.protected_tokens:
        raise TranslationValidationError("译文没有按原种类、值和顺序保留受保护标记")

    return ValidatedTranslation(
        proposal_id=_canonical_digest("proposal", proposal.to_dict()),
        task_id=task.task_id,
        batch_id=batch.batch_id,
        segment_id=segment.segment_id,
        source_sha256=segment.source_sha256,
        target_language=task.target_language,
        target_text=proposal.target_text,
    )


class TranslationBackend(Protocol):
    """Narrow provider seam; implementations receive only a filtered batch."""

    def propose(self, batch: TranslationBatch) -> tuple[TranslationProposal, ...]: ...


@dataclass(frozen=True, slots=True)
class TranslationBatchCheckpoint:
    batch_id: str
    status: TranslationBatchStatus
    attempts: int
    validated_proposal_ids: tuple[str, ...]
    last_error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "status": self.status.value,
            "attempts": self.attempts,
            "validated_proposal_ids": list(self.validated_proposal_ids),
            "last_error": self.last_error,
        }


@dataclass(frozen=True, slots=True)
class TranslationTaskCheckpoint:
    schema_version: int
    task_id: str
    status: TranslationTaskStatus
    batches: tuple[TranslationBatchCheckpoint, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "status": self.status.value,
            "batches": [batch.to_dict() for batch in self.batches],
        }


def new_translation_checkpoint(task: TranslationTask) -> TranslationTaskCheckpoint:
    return TranslationTaskCheckpoint(
        schema_version=TRANSLATION_CHECKPOINT_SCHEMA_VERSION,
        task_id=task.task_id,
        status=TranslationTaskStatus.PENDING,
        batches=tuple(
            TranslationBatchCheckpoint(
                batch_id=batch.batch_id,
                status=TranslationBatchStatus.PENDING,
                attempts=0,
                validated_proposal_ids=(),
                last_error=None,
            )
            for batch in task.batches
        ),
    )


def _replace_batch_checkpoint(
    checkpoint: TranslationTaskCheckpoint,
    updated: TranslationBatchCheckpoint,
    *,
    status: TranslationTaskStatus,
) -> TranslationTaskCheckpoint:
    return replace(
        checkpoint,
        status=status,
        batches=tuple(
            updated if batch.batch_id == updated.batch_id else batch
            for batch in checkpoint.batches
        ),
    )


def _task_batch(task: TranslationTask, batch_id: str) -> TranslationBatch:
    batch = next((item for item in task.batches if item.batch_id == batch_id), None)
    if batch is None:
        raise TranslationStateError(f"批次不属于当前任务：{batch_id}")
    return batch


def _checkpoint_batch(
    checkpoint: TranslationTaskCheckpoint,
    batch_id: str,
) -> TranslationBatchCheckpoint:
    batch = next((item for item in checkpoint.batches if item.batch_id == batch_id), None)
    if batch is None:
        raise TranslationStateError(f"检查点中没有批次：{batch_id}")
    return batch


def _validate_checkpoint_identity(
    task: TranslationTask,
    checkpoint: TranslationTaskCheckpoint,
) -> None:
    if checkpoint.schema_version != TRANSLATION_CHECKPOINT_SCHEMA_VERSION:
        raise TranslationStateError(
            f"不支持的检查点 schema 版本：{checkpoint.schema_version}"
        )
    if checkpoint.task_id != task.task_id:
        raise TranslationStateError("检查点 task_id 与任务不一致")
    if tuple(item.batch_id for item in checkpoint.batches) != tuple(
        item.batch_id for item in task.batches
    ):
        raise TranslationStateError("检查点批次与任务不一致")


def start_translation_batch(
    task: TranslationTask,
    checkpoint: TranslationTaskCheckpoint,
    batch_id: str,
) -> TranslationTaskCheckpoint:
    _validate_checkpoint_identity(task, checkpoint)
    _task_batch(task, batch_id)
    if checkpoint.status not in {
        TranslationTaskStatus.PENDING,
        TranslationTaskStatus.RUNNING,
    }:
        raise TranslationStateError(f"任务处于 {checkpoint.status.value}，不能开始批次")
    batch = _checkpoint_batch(checkpoint, batch_id)
    if batch.status is not TranslationBatchStatus.PENDING:
        raise TranslationStateError(f"批次处于 {batch.status.value}，不能开始")
    updated = replace(
        batch,
        status=TranslationBatchStatus.RUNNING,
        attempts=batch.attempts + 1,
    )
    return _replace_batch_checkpoint(
        checkpoint,
        updated,
        status=TranslationTaskStatus.RUNNING,
    )


def fail_translation_batch(
    task: TranslationTask,
    checkpoint: TranslationTaskCheckpoint,
    batch_id: str,
    error: str,
) -> TranslationTaskCheckpoint:
    _validate_checkpoint_identity(task, checkpoint)
    _task_batch(task, batch_id)
    if not error:
        raise TranslationStateError("批次失败必须记录非空错误")
    batch = _checkpoint_batch(checkpoint, batch_id)
    if batch.status is not TranslationBatchStatus.RUNNING:
        raise TranslationStateError(f"批次处于 {batch.status.value}，不能标记失败")
    updated = replace(batch, status=TranslationBatchStatus.FAILED, last_error=error)
    return _replace_batch_checkpoint(
        checkpoint,
        updated,
        status=TranslationTaskStatus.FAILED,
    )


def complete_translation_batch(
    task: TranslationTask,
    checkpoint: TranslationTaskCheckpoint,
    batch_id: str,
    proposals: Iterable[ValidatedTranslation],
) -> TranslationTaskCheckpoint:
    _validate_checkpoint_identity(task, checkpoint)
    task_batch = _task_batch(task, batch_id)
    checkpoint_batch = _checkpoint_batch(checkpoint, batch_id)
    by_segment: dict[str, ValidatedTranslation] = {}
    for proposal in proposals:
        if (
            proposal.task_id != task.task_id
            or proposal.batch_id != batch_id
            or proposal.segment_id in by_segment
        ):
            raise TranslationStateError("已验证提案不属于当前任务批次或包含重复文本段")
        by_segment[proposal.segment_id] = proposal
    expected_ids = tuple(segment.segment_id for segment in task_batch.segments)
    if set(by_segment) != set(expected_ids):
        raise TranslationStateError("完成批次需要且只能包含该批次的全部文本段")
    expected_by_id = {segment.segment_id: segment for segment in task_batch.segments}
    for segment_id, proposal in by_segment.items():
        expected = expected_by_id[segment_id]
        if (
            proposal.source_sha256 != expected.source_sha256
            or proposal.target_language != task.target_language
            or not proposal.target_text
            or re.fullmatch(r"proposal_[0-9a-f]{24}", proposal.proposal_id) is None
        ):
            raise TranslationStateError("已验证提案的来源、语言、文本或摘要记录不一致")
    proposal_ids = tuple(by_segment[segment_id].proposal_id for segment_id in expected_ids)

    if checkpoint_batch.status is TranslationBatchStatus.COMPLETED:
        if checkpoint_batch.validated_proposal_ids == proposal_ids:
            return checkpoint
        raise TranslationStateError("已完成批次收到冲突的重复结果")
    if checkpoint_batch.status is not TranslationBatchStatus.RUNNING:
        raise TranslationStateError(
            f"批次处于 {checkpoint_batch.status.value}，不能标记完成"
        )

    updated = replace(
        checkpoint_batch,
        status=TranslationBatchStatus.COMPLETED,
        validated_proposal_ids=proposal_ids,
        last_error=None,
    )
    provisional_batches = tuple(
        updated if batch.batch_id == updated.batch_id else batch
        for batch in checkpoint.batches
    )
    task_status = (
        TranslationTaskStatus.COMPLETED
        if all(batch.status is TranslationBatchStatus.COMPLETED for batch in provisional_batches)
        else TranslationTaskStatus.RUNNING
    )
    return replace(checkpoint, status=task_status, batches=provisional_batches)


def pause_translation_task(
    task: TranslationTask,
    checkpoint: TranslationTaskCheckpoint,
) -> TranslationTaskCheckpoint:
    _validate_checkpoint_identity(task, checkpoint)
    if checkpoint.status not in {
        TranslationTaskStatus.PENDING,
        TranslationTaskStatus.RUNNING,
    }:
        raise TranslationStateError(f"任务处于 {checkpoint.status.value}，不能暂停")
    return replace(
        checkpoint,
        status=TranslationTaskStatus.PAUSED,
        batches=tuple(
            replace(batch, status=TranslationBatchStatus.PENDING)
            if batch.status is TranslationBatchStatus.RUNNING
            else batch
            for batch in checkpoint.batches
        ),
    )


def recover_interrupted_translation_task(
    task: TranslationTask,
    checkpoint: TranslationTaskCheckpoint,
) -> TranslationTaskCheckpoint:
    _validate_checkpoint_identity(task, checkpoint)
    if checkpoint.status is not TranslationTaskStatus.RUNNING:
        raise TranslationStateError("只有运行中的任务可以作为中断任务恢复")
    return replace(
        checkpoint,
        status=TranslationTaskStatus.PAUSED,
        batches=tuple(
            replace(batch, status=TranslationBatchStatus.PENDING)
            if batch.status is TranslationBatchStatus.RUNNING
            else batch
            for batch in checkpoint.batches
        ),
    )


def resume_translation_task(
    task: TranslationTask,
    checkpoint: TranslationTaskCheckpoint,
) -> TranslationTaskCheckpoint:
    _validate_checkpoint_identity(task, checkpoint)
    if checkpoint.status not in {
        TranslationTaskStatus.PAUSED,
        TranslationTaskStatus.FAILED,
    }:
        raise TranslationStateError(f"任务处于 {checkpoint.status.value}，不能恢复")
    return replace(
        checkpoint,
        status=TranslationTaskStatus.RUNNING,
        batches=tuple(
            replace(batch, status=TranslationBatchStatus.PENDING)
            if batch.status in {TranslationBatchStatus.RUNNING, TranslationBatchStatus.FAILED}
            else batch
            for batch in checkpoint.batches
        ),
    )


def translation_checkpoint_from_dict(
    task: TranslationTask,
    raw: Mapping[str, Any],
) -> TranslationTaskCheckpoint:
    _expect_keys(raw, {"schema_version", "task_id", "status", "batches"}, location="checkpoint")
    schema_version = _expect_integer(
        raw["schema_version"], location="checkpoint.schema_version", minimum=1
    )
    task_id = _expect_string(raw["task_id"], location="checkpoint.task_id")
    status_value = _expect_string(raw["status"], location="checkpoint.status")
    try:
        status = TranslationTaskStatus(status_value)
    except ValueError as error:
        raise TranslationSchemaError(f"checkpoint.status 无效：{status_value}") from error
    raw_batches = raw["batches"]
    if not isinstance(raw_batches, list):
        raise TranslationSchemaError("checkpoint.batches 必须是数组")

    batches: list[TranslationBatchCheckpoint] = []
    for index, raw_batch_value in enumerate(raw_batches):
        location = f"checkpoint.batches[{index}]"
        raw_batch = _expect_mapping(raw_batch_value, location=location)
        _expect_keys(
            raw_batch,
            {"batch_id", "status", "attempts", "validated_proposal_ids", "last_error"},
            location=location,
        )
        batch_status_value = _expect_string(raw_batch["status"], location=f"{location}.status")
        try:
            batch_status = TranslationBatchStatus(batch_status_value)
        except ValueError as error:
            raise TranslationSchemaError(
                f"{location}.status 无效：{batch_status_value}"
            ) from error
        raw_proposal_ids = raw_batch["validated_proposal_ids"]
        if not isinstance(raw_proposal_ids, list):
            raise TranslationSchemaError(f"{location}.validated_proposal_ids 必须是数组")
        proposal_ids = tuple(
            _expect_string(item, location=f"{location}.validated_proposal_ids[{item_index}]")
            for item_index, item in enumerate(raw_proposal_ids)
        )
        batches.append(
            TranslationBatchCheckpoint(
                batch_id=_expect_string(raw_batch["batch_id"], location=f"{location}.batch_id"),
                status=batch_status,
                attempts=_expect_integer(raw_batch["attempts"], location=f"{location}.attempts"),
                validated_proposal_ids=proposal_ids,
                last_error=_expect_optional_string(
                    raw_batch["last_error"], location=f"{location}.last_error"
                ),
            )
        )

    checkpoint = TranslationTaskCheckpoint(
        schema_version=schema_version,
        task_id=task_id,
        status=status,
        batches=tuple(batches),
    )
    try:
        _validate_checkpoint_identity(task, checkpoint)
    except TranslationStateError as error:
        raise TranslationSchemaError(str(error)) from error

    for task_batch, batch in zip(task.batches, checkpoint.batches, strict=True):
        if batch.status is TranslationBatchStatus.COMPLETED:
            if len(batch.validated_proposal_ids) != len(task_batch.segments):
                raise TranslationSchemaError(
                    f"已完成批次 {batch.batch_id} 的提案记录数量不一致"
                )
        elif batch.validated_proposal_ids:
            raise TranslationSchemaError(
                f"未完成批次 {batch.batch_id} 不能包含已验证提案记录"
            )
        if batch.status is TranslationBatchStatus.RUNNING and batch.attempts < 1:
            raise TranslationSchemaError(f"运行中批次 {batch.batch_id} 缺少尝试次数")
        if batch.status is TranslationBatchStatus.FAILED and not batch.last_error:
            raise TranslationSchemaError(f"失败批次 {batch.batch_id} 缺少错误记录")

    all_completed = all(
        batch.status is TranslationBatchStatus.COMPLETED for batch in checkpoint.batches
    )
    if status is TranslationTaskStatus.COMPLETED and not all_completed:
        raise TranslationSchemaError("已完成任务仍包含未完成批次")
    if status is not TranslationTaskStatus.COMPLETED and all_completed:
        raise TranslationSchemaError("全部批次已完成但任务状态不是 completed")
    if status is TranslationTaskStatus.FAILED and not any(
        batch.status is TranslationBatchStatus.FAILED for batch in checkpoint.batches
    ):
        raise TranslationSchemaError("失败任务没有失败批次")
    if status is not TranslationTaskStatus.FAILED and any(
        batch.status is TranslationBatchStatus.FAILED for batch in checkpoint.batches
    ):
        raise TranslationSchemaError("非失败任务包含失败批次")
    if status is TranslationTaskStatus.PAUSED and any(
        batch.status is TranslationBatchStatus.RUNNING for batch in checkpoint.batches
    ):
        raise TranslationSchemaError("已暂停任务不能包含运行中批次")
    if status is TranslationTaskStatus.PENDING and any(
        batch.status is not TranslationBatchStatus.PENDING or batch.attempts != 0
        for batch in checkpoint.batches
    ):
        raise TranslationSchemaError("初始 pending 任务包含已开始的批次")
    return checkpoint
