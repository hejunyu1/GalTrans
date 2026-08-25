from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from galtrans.adapters.renpy import (
    RenpyExportValidation,
    RenpySdkCrosscheck,
    RenpyTemplateMapping,
    extract_renpy_path,
)
from galtrans.adapters.renpy.extractor import find_renpy_string_literals
from galtrans.automated import (
    AutomatedRenpyTranslationError,
    run_automated_renpy_translation,
)
from galtrans.ir import SegmentKind, TextSegment
from galtrans.qa import TranslationQualityOutcome
from galtrans.translation import (
    PROVIDER_RECEIPT_SCHEMA_VERSION,
    TRANSLATION_PROPOSAL_SCHEMA_VERSION,
    ProviderRequestReceipt,
    ProviderRequestStatus,
    TranslationBackend,
    TranslationBatch,
    TranslationProposal,
)


class _AutomaticBackend(TranslationBackend):
    def __init__(self, *, unchanged_segment_id: str | None = None) -> None:
        self.unchanged_segment_id = unchanged_segment_id
        self.calls: list[str] = []

    def submit(
        self,
        batch: TranslationBatch,
        idempotency_key: str,
    ) -> ProviderRequestReceipt:
        self.calls.append(idempotency_key)
        proposals = []
        for index, segment in enumerate(batch.segments):
            target_text = (
                segment.source_text
                if segment.segment_id == self.unchanged_segment_id
                else f"译文{batch.index}-{index}"
                + "".join(token.value for token in segment.protected_tokens)
            )
            proposals.append(
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
                        "target_text": target_text,
                    }
                )
            )
        return ProviderRequestReceipt.from_dict(
            {
                "schema_version": PROVIDER_RECEIPT_SCHEMA_VERSION,
                "request_id": idempotency_key,
                "provider_request_id": f"local-{batch.index}",
                "status": ProviderRequestStatus.SUCCEEDED.value,
                "proposals": [proposal.to_dict() for proposal in proposals],
                "error": None,
            }
        )

    def query(
        self,
        idempotency_key: str,
        provider_request_id: str | None,
    ) -> ProviderRequestReceipt:
        raise AssertionError(
            f"同步成功后不应查询：{idempotency_key} / {provider_request_id}"
        )


class _DefinitiveFailureThenSuccess(TranslationBackend):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.success = _AutomaticBackend()

    def submit(
        self,
        batch: TranslationBatch,
        idempotency_key: str,
    ) -> ProviderRequestReceipt:
        self.calls.append(idempotency_key)
        if len(self.calls) == 1:
            return ProviderRequestReceipt.from_dict(
                {
                    "schema_version": PROVIDER_RECEIPT_SCHEMA_VERSION,
                    "request_id": idempotency_key,
                    "provider_request_id": None,
                    "status": ProviderRequestStatus.FAILED.value,
                    "proposals": [],
                    "error": "temporary definitive failure",
                }
            )
        return self.success.submit(batch, idempotency_key)

    def query(
        self,
        idempotency_key: str,
        provider_request_id: str | None,
    ) -> ProviderRequestReceipt:
        raise AssertionError(
            f"确定失败应受控重试而非查询：{idempotency_key} / {provider_request_id}"
        )


def _segments(project_root: Path) -> tuple[TextSegment, ...]:
    return tuple(
        segment
        for result in extract_renpy_path(project_root)
        for segment in result.segments
    )


def _mapping(segment: TextSegment, index: int) -> RenpyTemplateMapping:
    if segment.kind is SegmentKind.MENU_CHOICE:
        source_code = f'"{segment.source_text}"'
        translation_identifier = None
    else:
        speaker = f"{segment.speaker} " if segment.speaker else ""
        source_code = f'{speaker}"{segment.source_text}"'
        translation_identifier = f"start_{index}"
    literal = find_renpy_string_literals(source_code)[0]
    return RenpyTemplateMapping(
        segment_id=segment.id,
        source_file=segment.source_file,
        line_number=segment.line_number,
        kind=segment.kind,
        source_text=segment.source_text,
        template_file="script.rpy",
        translation_identifier=translation_identifier,
        source_code=source_code,
        literal_start=literal.start,
        literal_end=literal.end,
        protected_tokens=tuple(token.value for token in segment.protected_tokens),
    )


def _crosscheck(project_root: Path) -> RenpySdkCrosscheck:
    segments = _segments(project_root)
    dialogue_count = sum(
        segment.kind in {SegmentKind.DIALOGUE, SegmentKind.NARRATION}
        for segment in segments
    )
    string_count = sum(
        segment.kind is SegmentKind.MENU_CHOICE for segment in segments
    )
    return RenpySdkCrosscheck(
        sdk_root=Path("C:/sdk"),
        executable=Path("C:/sdk/renpy.exe"),
        version="8.5.3.test",
        language="schinese",
        source_file_count=1,
        template_file_count=1,
        galtrans_dialogue_count=dialogue_count,
        official_dialogue_count=dialogue_count,
        galtrans_string_count=string_count,
        official_string_count=string_count,
        mappings=tuple(
            _mapping(segment, index) for index, segment in enumerate(segments)
        ),
        unmatched_segment_ids=(),
        unmatched_template_entries=(),
        template_warnings=(),
        lint_report="Lint finished.",
    )


def _validation(export_root: Path) -> RenpyExportValidation:
    translation_files = tuple((export_root / "game" / "tl" / "schinese").rglob("*.rpy"))
    if not translation_files:
        raise AssertionError("自动流程没有生成待验证翻译文件")
    return RenpyExportValidation(
        sdk_root=Path("C:/sdk"),
        version="8.5.3.test",
        language="schinese",
        source_file_count=1,
        translation_file_count=len(translation_files),
        compiled_file_count=len(translation_files) + 1,
        lint_report="Lint finished.",
    )


class AutomatedRenpyTranslationTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        project = root / "project"
        game = project / "game"
        game.mkdir(parents=True)
        (game / "script.rpy").write_text(
            """define aoi = Character(\"葵\")

label start:
    aoi \"こんにちは、[name]\"
    \"またね\"
    menu:
        \"はい\":
            return
""",
            encoding="utf-8",
        )
        return project

    def test_runs_complete_translation_to_validated_new_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = self._project(root)
            workspace = root / "workspace"
            output = root / "translated"
            source = project / "game" / "script.rpy"
            before_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            backend = _AutomaticBackend()

            def validate(
                sdk_path: Path,
                project_path: Path,
                export_path: Path,
                *,
                language: str,
                timeout_seconds: float,
            ) -> RenpyExportValidation:
                self.assertEqual(sdk_path, Path("C:/sdk"))
                self.assertEqual(project_path, project.resolve())
                self.assertEqual(language, "schinese")
                self.assertEqual(timeout_seconds, 60.0)
                return _validation(export_path)

            with (
                mock.patch(
                    "galtrans.automated.crosscheck_renpy_sdk",
                    return_value=_crosscheck(project),
                ),
                mock.patch(
                    "galtrans.automated.validate_renpy_export",
                    side_effect=validate,
                ),
            ):
                result = run_automated_renpy_translation(
                    Path("C:/sdk"),
                    project,
                    output,
                    workspace,
                    backend,
                    backend_identity="automatic-test-v1",
                    batch_size=2,
                )

            self.assertEqual(result.segment_count, 3)
            self.assertEqual(result.batch_count, 2)
            self.assertEqual(result.quality_outcome, TranslationQualityOutcome.CLEAR)
            self.assertEqual(len(backend.calls), 2)
            self.assertTrue(result.database_path.is_file())
            self.assertTrue(result.translation_files[0].is_file())
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before_hash)
            contents = result.translation_files[0].read_text(encoding="utf-8-sig")
            self.assertIn("译文0-0[name]", contents)
            self.assertIn("translate schinese strings:", contents)

    def test_low_confidence_is_reported_but_does_not_require_human_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = self._project(root)
            segments = _segments(project)
            backend = _AutomaticBackend(unchanged_segment_id=segments[0].id)
            with (
                mock.patch(
                    "galtrans.automated.crosscheck_renpy_sdk",
                    return_value=_crosscheck(project),
                ),
                mock.patch(
                    "galtrans.automated.validate_renpy_export",
                    side_effect=lambda _sdk, _project, export, **_kwargs: _validation(
                        export
                    ),
                ),
            ):
                result = run_automated_renpy_translation(
                    Path("C:/sdk"),
                    project,
                    root / "translated",
                    root / "workspace",
                    backend,
                    backend_identity="automatic-test-v1",
                )

            self.assertEqual(
                result.quality_outcome,
                TranslationQualityOutcome.LOW_CONFIDENCE,
            )
            self.assertEqual(result.low_confidence_segment_ids, (segments[0].id,))
            self.assertTrue(result.output_root.is_dir())

    def test_failed_validation_keeps_final_output_absent_and_resume_reuses_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = self._project(root)
            workspace = root / "workspace"
            backend = _AutomaticBackend()
            first_output = root / "failed-output"
            second_output = root / "translated"
            crosscheck = _crosscheck(project)
            with (
                mock.patch(
                    "galtrans.automated.crosscheck_renpy_sdk",
                    return_value=crosscheck,
                ),
                mock.patch(
                    "galtrans.automated.validate_renpy_export",
                    side_effect=RuntimeError("compile failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "compile failed"),
            ):
                run_automated_renpy_translation(
                    Path("C:/sdk"),
                    project,
                    first_output,
                    workspace,
                    backend,
                    backend_identity="automatic-test-v1",
                )
            calls_after_failure = len(backend.calls)
            self.assertFalse(first_output.exists())
            self.assertEqual(tuple(root.glob(".galtrans-auto-*")), ())

            with (
                mock.patch(
                    "galtrans.automated.crosscheck_renpy_sdk",
                    return_value=crosscheck,
                ),
                mock.patch(
                    "galtrans.automated.validate_renpy_export",
                    side_effect=lambda _sdk, _project, export, **_kwargs: _validation(
                        export
                    ),
                ),
            ):
                result = run_automated_renpy_translation(
                    Path("C:/sdk"),
                    project,
                    second_output,
                    workspace,
                    backend,
                    backend_identity="automatic-test-v1",
                )

            self.assertEqual(len(backend.calls), calls_after_failure)
            self.assertTrue(result.output_root.is_dir())

    def test_definitive_provider_failure_is_retried_once_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = self._project(root)
            backend = _DefinitiveFailureThenSuccess()
            with (
                mock.patch(
                    "galtrans.automated.crosscheck_renpy_sdk",
                    return_value=_crosscheck(project),
                ),
                mock.patch(
                    "galtrans.automated.validate_renpy_export",
                    side_effect=lambda _sdk, _project, export, **_kwargs: _validation(
                        export
                    ),
                ),
            ):
                result = run_automated_renpy_translation(
                    Path("C:/sdk"),
                    project,
                    root / "translated",
                    root / "workspace",
                    backend,
                    backend_identity="automatic-retry-test-v1",
                    batch_size=8,
                    max_definitive_attempts=2,
                )

            self.assertEqual(len(backend.calls), 2)
            self.assertEqual(backend.calls[0], backend.calls[1])
            self.assertTrue(result.output_root.is_dir())

    def test_refuses_existing_output_or_workspace_inside_input_before_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = self._project(root)
            existing_output = root / "existing"
            existing_output.mkdir()
            backend = _AutomaticBackend()
            with self.assertRaises(FileExistsError):
                run_automated_renpy_translation(
                    Path("C:/sdk"),
                    project,
                    existing_output,
                    root / "workspace",
                    backend,
                    backend_identity="automatic-test-v1",
                )
            with self.assertRaisesRegex(
                AutomatedRenpyTranslationError,
                "工作区不得与输入项目重叠",
            ):
                run_automated_renpy_translation(
                    Path("C:/sdk"),
                    project,
                    root / "output",
                    project / ".galtrans",
                    backend,
                    backend_identity="automatic-test-v1",
                )
            self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
