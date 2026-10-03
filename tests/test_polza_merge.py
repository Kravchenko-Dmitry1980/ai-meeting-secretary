from __future__ import annotations

import json

import httpx
import pytest

from secretary.infrastructure.polza import PolzaClient, ProviderError
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
