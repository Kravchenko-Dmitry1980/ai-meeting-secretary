"""Offline MAX contract vectors; these do not qualify a live bot or gateway.

The signing helper is a fixture oracle for the published two-stage HMAC,
not the future authentication endpoint. T6 must exercise its real validator
against these same vectors, freshness, replay and actor checks.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import unquote

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def contract():
    return json.loads((ROOT / "docs/contracts/max-contract.json").read_text(encoding="utf-8"))


def signing_fields(raw: str) -> tuple[str, str]:
    """Decode each inner value once, retain raw JSON and reject duplicate keys."""
    values = {}
    for pair in raw.split("&"):
        key, separator, value = pair.partition("=")
        if not separator or not key or key in values:
            raise ValueError("invalid_or_duplicate_fixture_key")
        values[key] = unquote(value, encoding="utf-8", errors="strict")
    if "hash" not in values:
        raise ValueError("missing_fixture_hash")
    supplied = values.pop("hash")
    return "\n".join(f"{key}={values[key]}" for key in sorted(values)), supplied


def fixture_signature(token: str, launch_params: str) -> str:
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, launch_params.encode(), hashlib.sha256).hexdigest()


def test_documented_hmac_matches_fixed_synthetic_vector(contract):
    vector = contract["fixtures"]["init_data_vector"]
    params, supplied = signing_fields(vector["init_data"])
    assert params == vector["launch_params"]
    assert supplied == "1a7ec09713a57bc3064d23c3ba5c67dd2bd5c7195d8d45a76c7638e9d0862ab0"
    assert hmac.compare_digest(fixture_signature(vector["synthetic_token"], params), supplied)


def test_inner_values_are_decoded_once_without_plus_conversion(contract):
    vector = contract["fixtures"]["init_data_vector"]
    params, _ = signing_fields(vector["init_data"])
    user = json.loads(next(line[5:] for line in params.split("\n") if line.startswith("user=")))
    assert user["first_name"] == "A+B %2F"
    assert user["id"] == 9007199254740993


@pytest.mark.parametrize("mutation", ["reverse", "lowercase_percent"])
def test_equivalent_encoding_and_order_keep_signature_identity(contract, mutation):
    vector = contract["fixtures"]["init_data_vector"]
    raw = vector["init_data"]
    altered = "&".join(reversed(raw.split("&"))) if mutation == "reverse" else raw.replace("%7B", "%7b")
    assert signing_fields(altered) == signing_fields(raw)
    # A replay key must use the verified signature, not hash of the raw query.
    assert hashlib.sha256(altered.encode()).digest() != hashlib.sha256(raw.encode()).digest()


@pytest.mark.parametrize("key", ["hash", "user", "auth_date"])
def test_duplicate_signed_keys_cannot_make_an_ambiguous_vector(contract, key):
    vector = contract["fixtures"]["init_data_vector"]
    with pytest.raises(ValueError, match="duplicate"):
        signing_fields(vector["init_data"] + f"&{key}=synthetic")


def test_tampered_actor_does_not_match_signature(contract):
    vector = contract["fixtures"]["init_data_vector"]
    raw = vector["init_data"].replace("%3A9007199254740993", "%3A42")
    params, supplied = signing_fields(raw)
    assert not hmac.compare_digest(fixture_signature(vector["synthetic_token"], params), supplied)


def test_reversed_hmac_key_and_message_is_incompatible(contract):
    vector = contract["fixtures"]["init_data_vector"]
    wrong_key = hmac.new(vector["synthetic_token"].encode(), b"WebAppData", hashlib.sha256).digest()
    wrong = hmac.new(wrong_key, vector["launch_params"].encode(), hashlib.sha256).hexdigest()
    assert wrong != vector["signature_hex"]


def test_provider_wire_id_and_timestamp_units_do_not_lose_precision(contract):
    event = contract["fixtures"]["message_created_audio"]
    assert event["message"]["sender"]["user_id"] == 9007199254740993
    assert event["message"]["recipient"]["chat_id"] == -9007199254740993
    assert event["timestamp"] == 1791045908000
    bounds = contract["wire_schema"]["$defs"]["Int64"]
    assert bounds["minimum"] == -(2 ** 63)
    assert bounds["maximum"] == 2 ** 63 - 1
    assert contract["init_data"]["auth_date_unit"] == "Unix seconds"


def test_callback_can_have_no_message_and_has_its_own_identity(contract):
    event = contract["fixtures"]["message_callback_without_message"]
    assert event["message"] is None
    assert event["callback"]["callback_id"] == "synthetic-callback-001"
    assert "update_id" not in event
    schema = contract["wire_schema"]["$defs"]["MessageCallbackUpdate"]
    assert "message" not in schema["required"]
    assert {"type": "null"} in schema["properties"]["message"]["anyOf"]
    assert contract["events"]["universal_event_id"] is None


def test_forwarded_only_body_and_hidden_activity_are_documented_nullable_cases(contract):
    event = contract["fixtures"]["forwarded_only_message"]
    assert event["message"]["body"] is None
    assert "sender" not in event["message"]
    user = event["message"]["link"]["sender"]
    assert user["last_name"] is None
    assert "last_activity_time" not in user
    definitions = contract["wire_schema"]["$defs"]
    assert {"type": "null"} in definitions["Message"]["properties"]["body"]["anyOf"]
    assert "last_activity_time" not in definitions["User"]["required"]


def test_native_voice_fixture_is_not_mistaken_for_audio_or_live_proof(contract):
    missing = contract["fixtures"]["native_voice_missing_attachment"]
    assert missing["message"]["body"]["attachments"] == []
    actual = contract["fixtures"]["message_created_audio"]["message"]["body"]["attachments"][0]
    assert actual["type"] == "audio"
    assert set(actual["payload"]) == {"url", "token"}
    assert contract["qualification"]["live_bot_verified"] is False
    assert contract["audio"]["native_voice_status"] == "NOT_QUALIFIED"
