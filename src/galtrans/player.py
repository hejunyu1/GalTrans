from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from galtrans.automated import (
    AutomatedRenpyTranslationResult,
    ProgressCallback,
    default_automated_workspace,
    run_automated_renpy_translation,
)
from galtrans.providers import OpenAICompatibleChatBackend


@dataclass(frozen=True, slots=True)
class PlayerTranslationRequest:
    sdk_path: Path
    project_path: Path
    output_path: Path
    endpoint: str
    model: str
    api_key: str
    workspace_path: Path | None = None
    source_language: str = "ja"
    target_language: str = "schinese"
    batch_size: int = 8
    provider_timeout_seconds: float = 120.0
    sdk_timeout_seconds: float = 60.0
    max_definitive_attempts: int = 2

    @property
    def resolved_workspace_path(self) -> Path:
        return self.workspace_path or default_automated_workspace(self.output_path)


def execute_player_translation(
    request: PlayerTranslationRequest,
    progress_callback: ProgressCallback | None = None,
) -> AutomatedRenpyTranslationResult:
    """Run the automatic workflow without putting credentials in the environment."""
    backend = OpenAICompatibleChatBackend(
        endpoint=request.endpoint,
        model=request.model,
        api_key=request.api_key,
        timeout_seconds=request.provider_timeout_seconds,
    )
    return run_automated_renpy_translation(
        request.sdk_path,
        request.project_path,
        request.output_path,
        request.resolved_workspace_path,
        backend,
        backend_identity=backend.identity,
        source_language=request.source_language,
        target_language=request.target_language,
        batch_size=request.batch_size,
        max_definitive_attempts=request.max_definitive_attempts,
        sdk_timeout_seconds=request.sdk_timeout_seconds,
        progress_callback=progress_callback,
    )


def redacted_error_message(error: Exception, secret: str) -> str:
    message = str(error) or type(error).__name__
    if secret:
        message = message.replace(secret, "[凭据已隐藏]")
    return message
