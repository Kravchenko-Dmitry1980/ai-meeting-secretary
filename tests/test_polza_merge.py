from __future__ import annotations

import json

import httpx
import pytest

from secretary.application.summary_context import freeze_summary_context
from secretary.infrastructure.polza import MAX_SUMMARY_REQUEST_BYTES, PolzaClient, ProviderError
from test_polza import chat_response, empty_summary, run, settings


@pytest.mark.parametrize("partial_chars", [3500, 6000])
def test_merge_can_group_partials_larger_than_map_window_and_reuses_checkpoints(tmp_path, partial_chars):
    requests, checkpoints, receipts = [], {}, []

    def handler(request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])
        requests.append(data)
        result = empty_summary()
        if data["operation"] == "summarize_source_segments":
            item = data["untrusted_meeting_data"][0]
            result["overview"] = "x" * partial_chars
            result["decisions"] = [{"text": "Обсуждение", "source_segment_ids": [item["id"]],
                                    "evidence_quote": item["text"][:10]}]
        else:
            assert len(data["untrusted_meeting_data"]) >= 2
            assert len(json.dumps(data["untrusted_meeting_data"], ensure_ascii=False, separators=(",", ":")).encode()) <= 40000
            result["decisions"] = [item for partial in data["untrusted_meeting_data"] for item in partial["decisions"]]
        return chat_response(result)

    client = PolzaClient(settings(tmp_path), httpx.MockTransport(handler))
    segments = [{"id": "s1", "text": "Я" * 2000}, {"id": "s2", "text": "Б" * 2000}]
    kwargs = {"checkpoint_get": checkpoints.get, "checkpoint_put": checkpoints.__setitem__, "on_usage": receipts.append}
    result = run(client.summarize(segments, **kwargs))
    assert requests[-1]["operation"] == "merge_partial_protocols"
    assert result["excluded_items_count"] == 0 and result["decisions"]
    assert len(receipts) == len(requests) == len(checkpoints)
    previous_calls = len(requests)
    client.validate_legacy_checkpoints(segments, checkpoints)
    assert len(requests) == previous_calls
    assert run(client.summarize(segments, **kwargs))["decisions"] == result["decisions"]
    assert len(requests) == previous_calls


def test_merge_still_stops_when_partials_cannot_reduce_within_40000_bytes(tmp_path):
    requests = []
    def handler(request):
        data = json.loads(json.loads(request.content)["messages"][1]["content"])
        requests.append(data)
        result = empty_summary()
        result["overview"] = "x" * 21000
        return chat_response(result)
    client = PolzaClient(settings(tmp_path), httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as caught:
        run(client.summarize([{"id": "s1", "text": "Я" * 2000}, {"id": "s2", "text": "Б" * 2000}]))
    assert caught.value.code == "summary_reduce_limit"
    assert len(requests) > 1
    assert all(item["operation"] == "summarize_source_segments" for item in requests)
    assert len(caught.value.usage_records) == len(requests)


def test_v2_merge_uses_remaining_full_request_budget_above_40000_user_bytes(tmp_path):
    requests = []
    segments = [{"id": f"s{i}", "text": chr(0x410 + i) * 2000} for i in range(4)]
    frozen = freeze_summary_context(
        meeting_id="m", transcript_version=1,
        settings={"summary_model": "qwen/qwen3-30b-a3b-instruct-2507",
                  "summary_batch_chars": 5000, "summary_max_output_tokens": 4096},
        segments=[{"id": item["id"], "meeting_id": "m", "transcript_version": 1,
                   "text": item["text"], "start_ms": i * 1000, "end_ms": i * 1000 + 500,
                   "timing_precision": "segment", "channel": "mixed"}
                  for i, item in enumerate(segments)],
        attribution={"meeting_id": "m", "transcript_version": 1, "revision": 1,
                     "roster_revision": 1,
                     "participants": [{"id": "p", "display_name": "Участник",
                                       "enabled": True, "aliases": [], "person_profile_id": None}],
                     "items": []},
    )

    def handler(request):
        payload = json.loads(request.content)
        body = json.loads(payload["messages"][1]["content"])
        requests.append((body, len(request.content)))
        result = empty_summary()
        if body["operation"] == "summarize_source_segments":
            result["overview"] = "x" * 21000
        else:
            result["overview"] = "Сжатый итог"
        return chat_response(result)

    client = PolzaClient(settings(tmp_path), httpx.MockTransport(handler))
    result = run(client.summarize(segments, context=frozen))

    map_calls = [body for body, _ in requests if body["operation"] == "summarize_source_segments"]
    merge_requests = [(body, size) for body, size in requests
                      if body["operation"] == "merge_partial_protocols"]
    assert len(map_calls) > 1
    assert merge_requests
    assert any(len(body["untrusted_meeting_data"][0].get("overview", "").encode()) >= 21000
               for body, _ in merge_requests)
    assert max(size for _, size in requests) <= MAX_SUMMARY_REQUEST_BYTES
    assert result["overview"] == "Сжатый итог"


def test_expanded_merge_retry_keeps_original_paid_map_and_merge_checkpoints(tmp_path):
    requests, checkpoints = [], {}
    fail_once = True
    def handler(request):
        nonlocal fail_once
        data = json.loads(json.loads(request.content)["messages"][1]["content"])
        requests.append(request.content)
        result = empty_summary()
        if data["operation"] == "summarize_source_segments":
            result["overview"] = "x" * 1900
        elif data["untrusted_meeting_data"][0]["overview"].startswith("x"):
            result["overview"] = "y" * 4000
        elif fail_once:
            fail_once = False
            return httpx.Response(429, json={"error": {"message": "mock retry later"}})
        return chat_response(result)
    client = PolzaClient(settings(tmp_path), httpx.MockTransport(handler))
    segments = [{"id": f"s{i}", "text": str(i) * 4000} for i in range(4)]
    kwargs = {"checkpoint_get": checkpoints.get, "checkpoint_put": checkpoints.__setitem__}
    with pytest.raises(ProviderError) as caught:
        run(client.summarize(segments, **kwargs))
    assert caught.value.code == "rate_limited"
    previous_paid = len(checkpoints)
    previous_requests = len(requests)
    assert previous_paid > 4
    # Missing future reduction is permitted when every accepted ancestor is exact.
    client.validate_legacy_checkpoints(segments, checkpoints)
    assert len(requests) == previous_requests
    result = run(client.summarize(segments, **kwargs))
    assert len(requests) == previous_requests + 1
    assert len(checkpoints) == previous_paid + 1
    assert sum(item["reused"] for item in result["usage_records"]) == previous_paid
