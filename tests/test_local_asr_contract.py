"""Synthetic transport/sidecar invariants; no weights, models or network."""
import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from secretary.domain.local_asr import ImportLocalAsrRun, LocalAsrV2, seconds_to_ms

FIXTURE = Path(__file__).parent / "fixtures/local_asr/synthetic_run.json"


def fixture_payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_v2_preserves_integer_units_independent_overlap_unknown_and_tokens():
    payload = fixture_payload()
    parsed = ImportLocalAsrRun.model_validate(payload)
    assert parsed.result.segments == 5
    assert parsed.result.speaker_turns[3].started_ms == parsed.result.speaker_turns[4].started_ms
    assert parsed.result.segment_items[3].speaker_label == "Спикер 1 + Спикер 2"
    assert parsed.result.segment_items[4].speaker_label == "Спикер неизвестен"
    assert parsed.model_dump(mode="json") == payload
    assert LocalAsrV2.model_validate({k: v for k, v in payload["result"].items() if k != "speaker_turns"}).speaker_turns == []


@pytest.mark.parametrize("value,expected", [(0,0),(0.0005,1),(1.2345,1235),(1.2344,1234),(1800,1800000)])
def test_explicit_seconds_conversion(value, expected):
    assert seconds_to_ms(value) == expected


@pytest.mark.parametrize("value", [True,False,"1",float("nan"),float("inf"),-0.1,604800.001])
def test_invalid_seconds_rejected(value):
    with pytest.raises(ValueError):
        seconds_to_ms(value)


@pytest.mark.parametrize("field,value", [("started_ms", 0.1),("ended_ms",True),("started_ms",-1),("ended_ms",0),("ended_ms",5001),("ordinal",2)])
def test_transport_rejects_bad_units_ranges_or_ordinals(field,value):
    payload=fixture_payload()
    payload["result"]["segment_items"][0][field]=value
    with pytest.raises(ValidationError):
        ImportLocalAsrRun.model_validate(payload)


@pytest.mark.parametrize("mutate", [
    lambda p:p["result"].update(segments=4),
    lambda p:p["result"].update(segments=True),
    lambda p:p["result"]["metrics"].update(loss=float("nan")),
    lambda p:p["artifacts"].update(selected_text_sha256="0"*64),
    lambda p:p["artifacts"]["words"][0].update(text="Подмена"),
    lambda p:p["artifacts"]["words"].pop(),
    lambda p:p["artifacts"]["speaker_kinds"].update({"Спикер 1 + Спикер 2":"single"}),
    lambda p:p["artifacts"]["segment_sources"][0].update(source_segment_ids=["source-1"]),
    lambda p:p["result"]["segment_items"][0].update(text="Подмена фрагмента."),
    lambda p:p["artifacts"].update(audio_ref="../../private.wav"),
    lambda p:p["artifacts"]["identity"].update(service_token="secret"),
])
def test_rejects_corrupted_or_rewritten_artifacts(mutate):
    payload=fixture_payload();mutate(payload)
    with pytest.raises(ValidationError):
        ImportLocalAsrRun.model_validate(payload)


def test_explicit_empty_alignment_preserves_text_segments_and_diagnostics():
    payload=fixture_payload();original=copy.deepcopy(payload["result"])
    payload["artifacts"]["words"]=[]
    payload["artifacts"]["alignment"]={"status":"unavailable","reason_codes":["tokenizer_not_qualified"],"unsupported_token_indices":[],"tokenizer_compatibility":"unverified"}
    result=ImportLocalAsrRun.model_validate(payload)
    assert result.result.model_dump(mode="json")==original
    assert result.artifacts.selected_text==payload["artifacts"]["selected_text"]
    payload["artifacts"]["alignment"]["reason_codes"]=[]
    with pytest.raises(ValidationError):ImportLocalAsrRun.model_validate(payload)


def test_runtime_model_digest_includes_loader_processor_code_and_is_order_independent():
    from secretary.domain.local_asr import model_manifest_digest
    model=fixture_payload()['artifacts']['identity']['models'][0]
    second=dict(model,role='diarizer',model_id='nvidia/Nemotron-3-Diarization')
    baseline=model_manifest_digest([model,second])
    assert model_manifest_digest([second,model])==baseline
    for field in ('weights_sha256','loader_sha256','processor_sha256','custom_code_sha256'):
        assert model_manifest_digest([dict(model,**{field:'9'*64}),second])!=baseline
    with pytest.raises(ValueError,match='Duplicate model role'):model_manifest_digest([model,model])


def test_device_identity_is_explicit_or_unknown_never_inferred_from_label():
    payload=fixture_payload()
    for key in ('asr_device','diarization_device','physical_gpu_id'):payload['artifacts']['identity'].pop(key)
    parsed=ImportLocalAsrRun.model_validate(payload)
    assert parsed.artifacts.identity.asr_device is None
    assert parsed.artifacts.identity.diarization_device is None
    payload['artifacts']['identity'].update(asr_device='cpu',diarization_device='cuda')
    with pytest.raises(ValidationError,match='physical GPU'):ImportLocalAsrRun.model_validate(payload)
