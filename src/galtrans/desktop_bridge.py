from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from galtrans.automated import (
    AutomatedRenpyTranslationProgress,
    AutomatedRenpyTranslationResult,
    ProgressCallback,
)
from galtrans.player import (
    PlayerTranslationRequest,
    execute_player_translation,
    redacted_error_message,
)

DESKTOP_BRIDGE_SCHEMA_VERSION = 1
_MAX_REQUEST_CHARACTERS = 16 * 1024
_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "sdk_path",
        "project_path",
        "output_path",
        "endpoint",
        "model",
        "api_key",
    }
)

PlayerExecutor = Callable[
    [PlayerTranslationRequest, ProgressCallback | None],
    AutomatedRenpyTranslationResult,
]


class DesktopBridgeError(ValueError):
    """Raised when the desktop bridge request is not closed and valid."""


def _checked_string(
    raw: object,
    *,
    field: str,
    maximum: int,
    strip: bool,
) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > maximum:
        raise DesktopBridgeError(f"{field} 必须是 1 到 {maximum} 个字符的字符串")
    if any(ord(character) < 32 for character in raw):
        raise DesktopBridgeError(f"{field} 不能包含控制字符")
    if strip and raw != raw.strip():
        raise DesktopBridgeError(f"{field} 不能包含首尾空白")
    return raw


def _request_from_json(raw_request: str) -> PlayerTranslationRequest:
    if not raw_request or len(raw_request) > _MAX_REQUEST_CHARACTERS:
        raise DesktopBridgeError("桌面请求为空或超过 16 KiB")
    try:
        decoded: object = json.loads(raw_request)
    except json.JSONDecodeError as error:
        raise DesktopBridgeError(f"桌面请求不是有效 JSON：{error.msg}") from error
    if not isinstance(decoded, dict) or set(decoded) != _REQUEST_KEYS:
        raise DesktopBridgeError("桌面请求字段不匹配")
    if type(decoded["schema_version"]) is not int or decoded["schema_version"] != 1:
        raise DesktopBridgeError("桌面请求 schema_version 不受支持")

    return PlayerTranslationRequest(
        sdk_path=Path(
            _checked_string(decoded["sdk_path"], field="sdk_path", maximum=32767, strip=True)
        ),
        project_path=Path(
            _checked_string(
                decoded["project_path"],
                field="project_path",
                maximum=32767,
                strip=True,
            )
        ),
        output_path=Path(
            _checked_string(
                decoded["output_path"],
                field="output_path",
                maximum=32767,
                strip=True,
            )
        ),
        endpoint=_checked_string(
            decoded["endpoint"],
            field="endpoint",
            maximum=2048,
            strip=True,
        ),
        model=_checked_string(decoded["model"], field="model", maximum=200, strip=True),
        api_key=_checked_string(
            decoded["api_key"],
            field="api_key",
            maximum=4096,
            strip=False,
        ),
    )


def _write_event(output_stream: TextIO, payload: dict[str, Any]) -> None:
    output_stream.write(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    output_stream.flush()


def _progress_payload(
    progress: AutomatedRenpyTranslationProgress,
) -> dict[str, Any]:
    return {
        "schema_version": DESKTOP_BRIDGE_SCHEMA_VERSION,
        "type": "progress",
        "stage": progress.stage.value,
        "message": progress.message,
        "completed_batches": progress.completed_batches,
        "total_batches": progress.total_batches,
    }


def run_desktop_bridge(
    input_stream: TextIO,
    output_stream: TextIO,
    executor: PlayerExecutor = execute_player_translation,
) -> int:
    secret = ""
    try:
        raw_request = input_stream.read(_MAX_REQUEST_CHARACTERS + 1)
        request = _request_from_json(raw_request)
        secret = request.api_key

        def report(progress: AutomatedRenpyTranslationProgress) -> None:
            _write_event(output_stream, _progress_payload(progress))

        result = executor(request, report)
    except Exception as error:
        _write_event(
            output_stream,
            {
                "schema_version": DESKTOP_BRIDGE_SCHEMA_VERSION,
                "type": "failed",
                "message": redacted_error_message(error, secret),
            },
        )
        return 1

    _write_event(
        output_stream,
        {
            "schema_version": DESKTOP_BRIDGE_SCHEMA_VERSION,
            "type": "succeeded",
            "result": result.to_dict(),
        },
    )
    return 0


def main() -> int:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return run_desktop_bridge(sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
