from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from galtrans.adapters.renpy import validate_renpy_translation_proposal
from galtrans.adapters.renpy.extractor import find_renpy_protected_tokens
from galtrans.ir import SegmentKind, TextSegment
from galtrans.providers import (
    OpenAICompatibleChatBackend,
    OpenAICompatibleProviderError,
)
from galtrans.translation import ProviderRequestStatus, create_translation_task


def _batch():
    segment = TextSegment(
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
        source_text="こんにちは、[name]",
        protected_tokens=find_renpy_protected_tokens("こんにちは、[name]"),
    )
    task = create_translation_task(
        (segment,),
        source_language="ja",
        target_language="schinese",
        batch_size=1,
    )
    return task, task.batches[0]


class _ProviderServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _ProviderHandler)
        self.response_status = 200
        self.response_payload: dict[str, Any] = {
            "id": "chatcmpl-local-test",
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "translations": [
                                    {
                                        "segment_id": "seg_dialogue",
                                        "target_text": "你好，[name]",
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ],
        }
        self.response_headers: dict[str, str] = {}
        self.requests: list[tuple[dict[str, str], dict[str, Any]]] = []


class _ProviderHandler(BaseHTTPRequestHandler):
    server_version = "GalTransProviderTest/1"

    def do_POST(self) -> None:
        server = cast(_ProviderServer, self.server)
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        server.requests.append((dict(self.headers.items()), payload))
        body = json.dumps(server.response_payload, ensure_ascii=False).encode("utf-8")
        self.send_response(server.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in server.response_headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _RunningProviderServer:
    def __enter__(self) -> _ProviderServer:
        self.server = _ProviderServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class OpenAICompatibleProviderTests(unittest.TestCase):
    def test_submits_filtered_json_and_returns_validated_proposals(self) -> None:
        task, batch = _batch()
        api_key = "test-secret-key"
        request_id = "request_" + "1" * 24
        with _RunningProviderServer() as server:
            endpoint = (
                f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
            )
            backend = OpenAICompatibleChatBackend(
                endpoint=endpoint,
                model="test-model",
                api_key=api_key,
            )
            receipt = backend.submit(batch, request_id)

        self.assertEqual(receipt.status, ProviderRequestStatus.SUCCEEDED)
        self.assertEqual(receipt.provider_request_id, "chatcmpl-local-test")
        self.assertEqual(receipt.proposals[0].target_text, "你好，[name]")
        validate_renpy_translation_proposal(task, receipt.proposals[0])
        headers, payload = server.requests[0]
        self.assertEqual(headers["Authorization"], f"Bearer {api_key}")
        self.assertEqual(headers["Idempotency-Key"], request_id)
        self.assertEqual(headers["User-Agent"], "GalTrans/0.4.3")
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        serialized_request = json.dumps(payload, ensure_ascii=False)
        self.assertIn("こんにちは、[name]", serialized_request)
        self.assertNotIn(str(Path("game/script.rpy")), serialized_request)
        self.assertNotIn(api_key, serialized_request)
        self.assertNotIn(api_key, backend.identity)
        self.assertNotIn(api_key, json.dumps(receipt.to_dict(), ensure_ascii=False))

        queried = backend.query(request_id, receipt.provider_request_id)
        self.assertEqual(queried.status, ProviderRequestStatus.UNKNOWN)
        self.assertEqual(queried.provider_request_id, receipt.provider_request_id)

    def test_invalid_success_body_is_unknown_and_not_trusted(self) -> None:
        _, batch = _batch()
        with _RunningProviderServer() as server:
            server.response_payload = {
                "id": "chatcmpl-invalid",
                "choices": [{"message": {"content": "not-json"}}],
            }
            backend = OpenAICompatibleChatBackend(
                endpoint=(
                    f"http://127.0.0.1:{server.server_address[1]}"
                    "/v1/chat/completions"
                ),
                model="test-model",
                api_key="test-key",
            )
            receipt = backend.submit(batch, "request_" + "2" * 24)

        self.assertEqual(receipt.status, ProviderRequestStatus.UNKNOWN)
        self.assertEqual(receipt.provider_request_id, "chatcmpl-invalid")
        self.assertEqual(receipt.proposals, ())

    def test_http_failures_distinguish_definitive_from_unknown(self) -> None:
        _, batch = _batch()
        with _RunningProviderServer() as server:
            endpoint = (
                f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
            )
            backend = OpenAICompatibleChatBackend(
                endpoint=endpoint,
                model="test-model",
                api_key="test-key",
            )
            server.response_status = 401
            definitive = backend.submit(batch, "request_" + "3" * 24)
            server.response_status = 503
            unknown = backend.submit(batch, "request_" + "4" * 24)

        self.assertEqual(definitive.status, ProviderRequestStatus.FAILED)
        self.assertEqual(unknown.status, ProviderRequestStatus.UNKNOWN)
        self.assertNotIn("test-key", definitive.error or "")
        self.assertNotIn("test-key", unknown.error or "")

    def test_rejects_remote_http_and_unsafe_credentials(self) -> None:
        with self.assertRaisesRegex(OpenAICompatibleProviderError, "HTTPS"):
            OpenAICompatibleChatBackend(
                endpoint="http://example.com/v1/chat/completions",
                model="test-model",
                api_key="test-key",
            )
        with self.assertRaisesRegex(OpenAICompatibleProviderError, "API key"):
            OpenAICompatibleChatBackend(
                endpoint="https://example.com/v1/chat/completions",
                model="test-model",
                api_key="has whitespace",
            )

    def test_rejects_redirects_and_provider_echoed_credentials(self) -> None:
        _, batch = _batch()
        request_id = "request_" + "5" * 24
        api_key = "provider-echo-secret"
        with _RunningProviderServer() as server:
            endpoint = (
                f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
            )
            backend = OpenAICompatibleChatBackend(
                endpoint=endpoint,
                model="test-model",
                api_key=api_key,
            )
            server.response_status = 307
            server.response_headers = {
                "Location": f"http://127.0.0.1:{server.server_address[1]}/stolen"
            }
            redirected = backend.submit(batch, request_id)
            self.assertEqual(len(server.requests), 1)

            server.response_status = 200
            server.response_headers = {}
            server.response_payload = {
                "id": "chatcmpl-credential-echo",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "translations": [
                                        {
                                            "segment_id": "seg_dialogue",
                                            "target_text": api_key,
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ],
            }
            echoed = backend.submit(batch, "request_" + "6" * 24)

        self.assertEqual(redirected.status, ProviderRequestStatus.UNKNOWN)
        self.assertEqual(echoed.status, ProviderRequestStatus.UNKNOWN)
        self.assertNotIn(api_key, json.dumps(echoed.to_dict(), ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
