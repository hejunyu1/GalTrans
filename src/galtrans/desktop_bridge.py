from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from galtrans.adapters.renpy.compatibility import (
    RenpyCompatibilityReport,
    inspect_renpy_compatibility,
)
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

DESKTOP_BRIDGE_SCHEMA_VERSION = 2
_MAX_REQUEST_CHARACTERS = 16 * 1024
_TRANSLATION_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "operation",
        "sdk_path",
        "project_path",
        "output_path",
        "endpoint",
        "model",
        "api_key",
    }
)
_COMPATIBILITY_REQUEST_KEYS = frozenset(
    {"schema_version", "operation", "project_path"}
)

PlayerExecutor = Callable[
    [PlayerTranslationRequest, ProgressCallback | None],
    AutomatedRenpyTranslationResult,
]
CompatibilityExecutor = Callable[[Path], RenpyCompatibilityReport]


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


def _decoded_request(raw_request: str) -> dict[str, object]:
    if not raw_request or len(raw_request) > _MAX_REQUEST_CHARACTERS:
        raise DesktopBridgeError("桌面请求为空或超过 16 KiB")
    try:
        decoded: object = json.loads(raw_request)
    except json.JSONDecodeError as error:
        raise DesktopBridgeError(f"桌面请求不是有效 JSON：{error.msg}") from error
    if not isinstance(decoded, dict):
        raise DesktopBridgeError("桌面请求必须是 JSON 对象")
    if (
        type(decoded.get("schema_version")) is not int
        or decoded["schema_version"] != DESKTOP_BRIDGE_SCHEMA_VERSION
    ):
        raise DesktopBridgeError("桌面请求 schema_version 不受支持")
    return decoded


def _translation_request(decoded: dict[str, object]) -> PlayerTranslationRequest:
    if set(decoded) != _TRANSLATION_REQUEST_KEYS:
        raise DesktopBridgeError("桌面翻译请求字段不匹配")
    if decoded["operation"] != "translate":
        raise DesktopBridgeError("桌面翻译请求 operation 不受支持")

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


def _compatibility_path(decoded: dict[str, object]) -> Path:
    if set(decoded) != _COMPATIBILITY_REQUEST_KEYS:
        raise DesktopBridgeError("桌面兼容性请求字段不匹配")
    if decoded["operation"] != "inspect_renpy_compatibility":
        raise DesktopBridgeError("桌面兼容性请求 operation 不受支持")
    return Path(
        _checked_string(
            decoded["project_path"],
            field="project_path",
            maximum=32767,
            strip=True,
        )
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
    compatibility_executor: CompatibilityExecutor = inspect_renpy_compatibility,
) -> int:
    secret = ""
    try:
        raw_request = input_stream.read(_MAX_REQUEST_CHARACTERS + 1)
        decoded = _decoded_request(raw_request)
        operation = decoded.get("operation")
        if operation == "inspect_renpy_compatibility":
            compatibility_report = compatibility_executor(_compatibility_path(decoded))
            _write_event(
                output_stream,
                {
                    "schema_version": DESKTOP_BRIDGE_SCHEMA_VERSION,
                    "type": "compatibility_report",
                    "report": compatibility_report.to_dict(),
                },
            )
            return 0
        if operation != "translate":
            raise DesktopBridgeError("桌面请求 operation 不受支持")

        request = _translation_request(decoded)
        secret = request.api_key
        compatibility_report = compatibility_executor(request.project_path)
        if not compatibility_report.can_translate_now:
            raise DesktopBridgeError(
                f"只读兼容性检查未通过：{compatibility_report.summary}"
            )

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
