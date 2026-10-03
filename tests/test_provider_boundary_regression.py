"""Provider boundaries: all responses are synthetic, no credentials or live HTTP."""
from __future__ import annotations

import gzip
import asyncio
import json
import zlib

import httpx
import pytest

from secretary.infrastructure.polza import PolzaClient, ProviderError
from test_polza import chat_response, empty_summary, settings, wav_file


@pytest.mark.parametrize("on_submit", [False, True])
@pytest.mark.parametrize("error", ["Private provider failure", {"code": "BAD_REQUEST", "message": "Private failure"}])
async def test_async_terminal_error_keeps_id_and_receipt(tmp_path, on_submit, error):
    calls, saved = [], []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"id": "known-terminal", "status": "failed", "error": error,
                                        "usage": {"cost_rub": .03}})
    client = PolzaClient(settings(tmp_path, stt_model="aiesa/transcribe"), httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as caught:
        await client.transcribe(wav_file(tmp_path), provider_job_id=None if on_submit else "known-terminal",
                                on_provider_job=saved.append)
    assert caught.value.code == "provider_job_failed"
    assert caught.value.provider_job_id == "known-terminal"
    assert caught.value.usage_records == [{"kind": "transcription", "provider_request_id": "known-terminal", "confirmed_rub": .03}]
    assert not caught.value.uncertain and not caught.value.retryable
    assert "Private" not in str(caught.value)
    assert calls == (["POST"] if on_submit else ["GET"])
    assert saved == (["known-terminal"] if on_submit else [])


@pytest.mark.parametrize("body", [
    json.dumps({"text": "Words", "usage": {"cost_rub": 10**400}}).encode(),
    b'{"text":"Words","duration":1e308,"usage":{"cost_rub":0.04}}',
    b'{"text":"Words","segments":[{"text":"Words","start":1e300,"end":1e300}]}',
    ("[" * 20000 + "0" + "]" * 20000).encode(),
], ids=["cost-overflow", "duration-overflow", "segment-overflow", "deep-json"])
async def test_pathological_post_never_becomes_retryable_processing_error(tmp_path, body):
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, content=body)
    with pytest.raises(ProviderError) as caught:
        await PolzaClient(settings(tmp_path), httpx.MockTransport(handler)).transcribe(wav_file(tmp_path))
    assert caught.value.uncertain and not caught.value.retryable
    assert calls == ["POST"]


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("compressed", [False, True])
async def test_oversized_summary_stream_stops_before_remaining_body(tmp_path, compressed):
    body = b'{"padding":"' + b"x" * (1024 * 1024) + b'"}'
    stream = Chunks([gzip.compress(body) if compressed else body, b"never-read"])
    def handler(request):
        return httpx.Response(200, stream=stream, headers={"Content-Encoding": "gzip"} if compressed else {})
    with pytest.raises(ProviderError) as caught:
        await PolzaClient(settings(tmp_path), httpx.MockTransport(handler)).summarize([{"id": "s1", "text": "Words"}])
    assert caught.value.code == "response_too_large"
    assert caught.value.uncertain and not caught.value.retryable
    assert stream.read == 1 and stream.closed


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
async def test_bounded_stream_preserves_valid_response(tmp_path, encoding):
    body = json.dumps({"text": "Words", "usage": {"cost_rub": .02}}).encode()
    encoded = gzip.compress(body) if encoding == "gzip" else zlib.compress(body) if encoding == "deflate" else body
    stream = Chunks([encoded[:5], encoded[5:]])
    client = PolzaClient(settings(tmp_path), httpx.MockTransport(lambda request: httpx.Response(
        200, stream=stream, headers={"Content-Encoding": encoding})))
    value = await client.transcribe(wav_file(tmp_path))
    assert value["segments"][0]["text"] == "Words"
    assert value["usage"]["confirmed_rub"] == .02
    assert stream.closed


@pytest.mark.parametrize("body,encoding", [(b"broken", "gzip"), (gzip.compress(b'{}')[:-4], "gzip"),
                                           (b"unreadable", "unknown")])
async def test_invalid_encoding_is_typed_ambiguous_and_closed(tmp_path, body, encoding):
    stream = Chunks([body])
    with pytest.raises(ProviderError) as caught:
        await PolzaClient(settings(tmp_path), httpx.MockTransport(lambda request: httpx.Response(
            200, stream=stream, headers={"Content-Encoding": encoding}))).transcribe(wav_file(tmp_path))
    assert caught.value.uncertain and not caught.value.retryable
    assert stream.closed


async def test_repair_uses_only_current_source_slice_and_retains_evidence_refs(tmp_path):
    calls, repair_sizes = [], []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        value = empty_summary()
        if len(calls) == 1:
            value["decisions"] = [{"text": "Valid decision", "source_segment_ids": ["s1"], "evidence_quote": "invented"}]
        elif len(payload["messages"]) == 3:
            content = payload["messages"][-1]["content"]
            repair = json.loads(content[content.index('{"untrusted_invalid_candidate"'):])
            source = repair["untrusted_source_segments"]
            repair_sizes.append(len(json.dumps(source, ensure_ascii=False).encode()))
            assert repair_sizes[-1] <= 5000
            assert {item["id"] for item in source} == {"s1"}
            value["decisions"] = [{"text": "Valid decision", "source_segment_ids": ["s1"], "evidence_quote": "Approved."}]
        elif "merge_partial_protocols" in payload["messages"][1]["content"]:
            value["decisions"] = [{"text": "Valid decision", "source_segment_ids": ["s1"], "evidence_quote": "Approved."}]
        return chat_response(value)
    result = await PolzaClient(settings(tmp_path), httpx.MockTransport(handler)).summarize(
        [{"id": "s1", "text": "Approved. " * 12000}])
    assert repair_sizes
    assert result["decisions"][0]["source_segment_ids"] == ["s1"]


@pytest.mark.parametrize("usage,want", [({"cost": .12}, .12), ({"cost": 0}, 0),
    ({"cost_rub": .03, "cost": .12}, .03), ({"cost_rub": None, "cost": .12}, None),
    ({"cost": True}, None), ({"cost": "nan"}, None), ({"cost": -1}, None)])
async def test_documented_rub_cost_alias_with_explicit_field_precedence(tmp_path, usage, want):
    client = PolzaClient(settings(tmp_path), httpx.MockTransport(lambda request: httpx.Response(
        200, json={"text": "Words", "usage": usage})))
    assert (await client.transcribe(wav_file(tmp_path)))["usage"]["confirmed_rub"] == want


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("extra", [0, 1])
async def test_summary_response_accepts_exact_limit_and_rejects_one_extra_byte(tmp_path, compressed, extra):
    body = b'{"accepted":true}' + b" " * (512 * 1024 - 17 + extra)
    stream = Chunks([gzip.compress(body) if compressed else body])
    client = PolzaClient(settings(tmp_path), httpx.MockTransport(lambda request: httpx.Response(
        200, stream=stream, headers={"Content-Encoding": "gzip"} if compressed else {})))
    if extra:
        with pytest.raises(ProviderError) as caught:
            await client._request("POST", "chat/completions", payload={})
        assert caught.value.code == "response_too_large" and caught.value.uncertain
    else:
        assert await client._request("POST", "chat/completions", payload={}) == {"accepted": True}
    assert stream.closed


async def test_streaming_total_deadline_preserves_get_id(tmp_path):
    class Slow(Chunks):
        async def __aiter__(self):
            for _ in range(10):
                await asyncio.sleep(.015)
                yield b" "
    stream = Slow([])
    config = settings(tmp_path, stt_model="aiesa/transcribe")
    config.request_timeout_seconds = .025
    client = PolzaClient(config, httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)))
    with pytest.raises(ProviderError) as caught:
        await client.transcribe(tmp_path / "not-read.wav", provider_job_id="preserved-id")
    assert caught.value.code == "poll_unavailable" and caught.value.retryable
    assert caught.value.provider_job_id == "preserved-id"
    assert stream.closed


@pytest.mark.parametrize("reason,want", [("noProvidersForModel", "noProvidersForModel"),
    ("SENSITIVE-REASON", None), ({"private": "SENSITIVE-REASON"}, None)])
async def test_machine_reason_is_allowlisted_and_does_not_infer_price_rejection(tmp_path, reason, want):
    body = {"error": {"code": "BAD_REQUEST", "message": "SENSITIVE-MESSAGE", "metadata": {
        "reason": reason, "raw": "SENSITIVE-RAW", "provider_name": "SENSITIVE-NAME"}}}
    with pytest.raises(ProviderError) as caught:
        await PolzaClient(settings(tmp_path), httpx.MockTransport(lambda request: httpx.Response(
            400, json=body))).transcribe(wav_file(tmp_path))
    assert caught.value.code == "invalid_request"
    assert caught.value.provider_error_reason == want
    assert "SENSITIVE" not in repr(vars(caught.value))


async def test_merge_repair_uses_bounded_verbatim_excerpts_not_original_long_segment(tmp_path):
    repairs, rejected = [], False
    def handler(request):
        nonlocal rejected
        payload = json.loads(request.content)
        value = empty_summary()
        value["decisions"] = [{"text": "Decision", "source_segment_ids": ["s1"], "evidence_quote": "Approved."}]
        if len(payload["messages"]) == 3:
            content = payload["messages"][-1]["content"]
            repair = json.loads(content[content.index('{"untrusted_invalid_candidate"'):])
            repairs.append(repair)
            assert len(json.dumps(repair["untrusted_source_segments"], ensure_ascii=False).encode()) <= 5000
            assert repair["untrusted_source_segments"] == [{"id": "s1", "text": "Approved."}]
        elif "merge_partial_protocols" in payload["messages"][1]["content"] and not rejected:
            rejected = True
            value["decisions"][0]["evidence_quote"] = "invented"
        return chat_response(value)
    result = await PolzaClient(settings(tmp_path), httpx.MockTransport(handler)).summarize(
        [{"id": "s1", "text": "Approved. " * 12000}])
    assert len(repairs) == 1
    assert result["decisions"][0]["source_segment_ids"] == ["s1"]


@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_poll_cannot_attach_another_job_result_or_receipt(tmp_path, status):
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"id": "wrong-id", "status": status, "text": "Wrong job",
                                        "usage": {"cost_rub": .07}})
    with pytest.raises(ProviderError) as caught:
        await PolzaClient(settings(tmp_path, stt_model="aiesa/transcribe"), httpx.MockTransport(handler)).transcribe(
            tmp_path / "not-read.wav", provider_job_id="expected-id")
    assert caught.value.code == "invalid_response"
    assert caught.value.provider_job_id == "expected-id"
    assert caught.value.usage_records == []
    assert calls == ["GET"]


@pytest.mark.parametrize("kind", ["transcription", "summary"])
async def test_escaped_lone_surrogate_is_typed_before_persistence_and_keeps_receipt(tmp_path, kind):
    calls, receipts = [], []
    if kind == "transcription":
        response = {"id": "paid-unicode", "text": "\ud800", "usage": {"cost_rub": .04}}
    else:
        value = empty_summary()
        value["overview"] = "\ud800"
        response = {"id": "paid-unicode", "choices": [{"message": {"content": json.dumps(value)},
                    "finish_reason": "stop"}], "usage": {"cost_rub": .04}}
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, content=json.dumps(response).encode())
    client = PolzaClient(settings(tmp_path), httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as caught:
        if kind == "transcription":
            await client.transcribe(wav_file(tmp_path))
        else:
            await client.summarize([{"id": "s1", "text": "Words"}], on_usage=receipts.append)
    assert caught.value.uncertain and not caught.value.retryable
    assert caught.value.usage_records[0]["confirmed_rub"] == .04
    assert caught.value.usage_records[0]["provider_request_id"] == "paid-unicode"
    assert calls == ["POST"]
    assert "\\ud800" not in repr(vars(caught.value))
