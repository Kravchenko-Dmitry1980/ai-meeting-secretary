"""Validate the synthetic benchmark oracle, without evaluating ASR or application.

No provider, device, media downloader or runtime database is imported. Application
integration must evaluate the same cases separately; fixture validity is not a
model-quality result.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import UUID

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "docs/benchmarks/voice-commands-v1.json"
MOSCOW_OFFSET = timedelta(hours=3)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate benchmark JSON key: {key}")
        result[key] = value
    return result


def load_manifest():
    return json.loads(FIXTURE.read_text(encoding="utf-8"), object_pairs_hook=unique_object)


MANIFEST = load_manifest()
CASES = MANIFEST["cases"]
BY_ID = {case["id"]: case for case in CASES}


def test_manifest_is_an_explicit_unrun_synthetic_semantic_oracle():
    assert MANIFEST["schema_version"] == "secretary.team.voice-benchmark.v1"
    assert MANIFEST["status"] == "PREPARED_NOT_RUN_LIVE"
    assert MANIFEST["synthetic_only"] is True
    assert MANIFEST["timezone"] == "Europe/Moscow"
    assert MANIFEST["interpretation"] == "semantic_oracle_not_provider_wire_dto"
    assert MANIFEST["models"] == {
        "stt_candidate": "openai/whisper-large-v3-turbo",
        "intent_candidate": "openai/gpt-4.1-mini",
        "qualification": "NOT_QUALIFIED",
    }


def test_json_oracle_rejects_duplicate_keys_instead_of_overwriting_expectation():
    with pytest.raises(ValueError, match="duplicate benchmark JSON key"):
        json.loads('{"expected":{"kind":"none","kind":"intent"}}', object_pairs_hook=unique_object)


def test_dataset_has_at_least_thirty_unique_short_russian_text_cases():
    assert len(CASES) == len(BY_ID)
    text_cases = [case for case in CASES if case["input"]["kind"] == "text"]
    assert len(text_cases) >= 30
    assert len({case["input"]["text"] for case in text_cases}) == len(text_cases)
    for case in text_cases:
        text = case["input"]["text"]
        assert isinstance(text, str) and 1 <= len(text) <= 1500
        assert any("А" <= char <= "я" or char in "Ёё" for char in text)
        assert case["input"]["media_payload_present"] is False


def test_required_scenarios_have_independent_fixture_coverage():
    counts = Counter(tag for case in CASES for tag in case["tags"])
    minimums = {
        "builtin": 6, "create": 5, "assign": 3, "state": 4, "due": 6,
        "comment": 1, "classify": 2, "rename": 1, "person": 6,
        "patronymic": 2, "date_relative": 4, "date_explicit": 2,
        "date_ambiguous": 3, "negation": 3, "reported_speech": 3,
        "quoted_reporting": 2, "person_unknown": 2, "person_ambiguous": 2,
        "task_ambiguous": 1, "multi_two": 1, "multi_five": 1,
        "multi_exceeds_five": 1, "foreign_project": 2,
        "schema_injection": 1, "tool_injection": 1, "native_voice": 1,
        "audio_file": 1,
    }
    assert all(counts[tag] >= minimum for tag, minimum in minimums.items()), counts


def test_scope_is_three_synthetic_members_one_project_and_two_tasks():
    context = MANIFEST["context"]
    assert context["project_ids"] == ["7"] and context["default_project_id"] == "7"
    assert [member["display_name"] for member in context["members"]] == [
        "Дмитрий", "Павел Александрович", "Алексей"]
    member_ids = {member["id"] for member in context["members"]}
    assert len(member_ids) == 3 and context["actor_id"] in member_ids
    for member in context["members"]:
        assert str(UUID(member["id"])) == member["id"]
        assert member["id"].startswith("10000000-0000-4000-8000-")
        assert member["project_ids"] == ["7"] and member["role"] == "owner"
    assert {task["task_id"] for task in context["tasks"]} == {"50", "55"}
    assert all(task["project_id"] == "7" and task["assignee_id"] in member_ids
               for task in context["tasks"])
    for case in CASES:
        overrides = case.get("context_overrides", {})
        assert set(overrides) <= {"display_names"}
        assert set(overrides.get("display_names", {})) <= member_ids


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_each_case_has_anchored_time_bounded_expectations_and_literal_evidence(case):
    assert len(case["tags"]) == len(set(case["tags"])) and case["tags"]
    event = datetime.fromisoformat(case["event_datetime"])
    server = datetime.fromisoformat(case["server_now"])
    assert case["event_datetime"].endswith("+03:00") and event.utcoffset() == MOSCOW_OFFSET
    assert server.tzinfo is not None
    if "date_relative" in case["tags"]:
        assert event.date() != server.date()
    expected = case["expected"]
    assert expected["kind"] in {"builtin", "intent", "none", "clarify", "error"}
    assert isinstance(expected["resolution"], str) and expected["resolution"]
    assert expected["requires_confirmation"] is (expected["kind"] == "intent")
    assert len(expected["proposals"]) <= MANIFEST["limits"]["max_proposals"] == 5
    if expected["kind"] in {"builtin", "none", "error"}:
        assert expected["proposals"] == []
    if expected["kind"] == "clarify":
        assert expected["clarification_fields"]
    member_ids = {member["id"] for member in MANIFEST["context"]["members"]}
    for proposal in expected["proposals"]:
        assert proposal["action"] in {"create", "assign", "set_state", "set_due",
                                      "comment", "classify", "rename", "propose_due"}
        assert proposal["project_id"] == "7"
        assert proposal["task_id"] in {None, "50", "55"}
        assert proposal["member_id"] is None or proposal["member_id"] in member_ids
        quote = proposal["evidence_quote"]
        assert isinstance(quote, str) and quote and quote in case["input"]["text"]
        assert len(proposal["unresolved_fields"]) == len(set(proposal["unresolved_fields"]))
        if proposal["due_at_utc"] is not None:
            assert datetime.fromisoformat(proposal["due_at_utc"]).utcoffset() == timedelta(0)
        if "due" in proposal["unresolved_fields"] or "due_time" in proposal["unresolved_fields"]:
            assert proposal["due_at_utc"] is None
        if expected["kind"] == "intent":
            assert proposal["unresolved_fields"] == []


def test_relative_dates_have_fixed_utc_oracles_independent_of_server_date():
    expected_dates = {
        "create_patronymic_tomorrow": "2026-10-02T07:00:00Z",
        "due_friday_event_anchor": "2026-10-02T13:00:00Z",
        "due_tomorrow_friday_event": "2026-10-03T06:00:00Z",
        "due_tomorrow_midnight_boundary": "2026-10-01T21:10:00Z",
        "create_dmitry_explicit_date": "2026-10-05T12:00:00Z",
        "due_explicit_date": "2026-10-09T11:00:00Z",
    }
    for identifier, expected in expected_dates.items():
        assert BY_ID[identifier]["expected"]["proposals"][0]["due_at_utc"] == expected
    friday = BY_ID["due_friday_event_anchor"]
    due = datetime.fromisoformat(friday["expected"]["proposals"][0]["due_at_utc"])
    assert due.astimezone(timezone(MOSCOW_OFFSET)).weekday() == 4
    assert due.date() < datetime.fromisoformat(friday["server_now"]).date()
    assert datetime(2026, 10, 5).weekday() == 0  # The contradictory spoken Friday is a Monday.
    assert BY_ID["contradictory_weekday_date"]["expected"]["kind"] == "clarify"
    assert BY_ID["contradictory_weekday_date"]["expected"]["proposals"][0]["due_at_utc"] is None


def test_negation_reporting_quotes_and_retraction_are_no_obligation_oracles():
    negative_ids = ["negated_create", "negated_assign", "reported_discussion",
        "reported_quoted_command", "quote_for_minutes", "question_not_obligation",
        "hypothetical_not_obligation", "retracted_command"]
    assert BY_ID["negated_create"]["input"]["text"].startswith("Не ставь задачу")
    assert "Мы обсуждали" in BY_ID["reported_discussion"]["input"]["text"]
    assert "«Назначь Дмитрию задачу 50»" in BY_ID["reported_quoted_command"]["input"]["text"]
    for identifier in negative_ids:
        assert BY_ID[identifier]["expected"]["kind"] == "none"
        assert BY_ID[identifier]["expected"]["proposals"] == []


def test_ambiguous_member_fixture_reuses_ids_and_never_guesses_assignee():
    case = BY_ID["ambiguous_alexey_duplicate"]
    names = {member["id"]: member["display_name"] for member in MANIFEST["context"]["members"]}
    names.update(case["context_overrides"]["display_names"])
    assert len(names) == 3 and Counter(names.values())["Алексей"] == 2
    assert case["expected"]["kind"] == "clarify"
    assert case["expected"]["proposals"][0]["member_id"] is None
    assert case["expected"]["proposals"][0]["unresolved_fields"] == ["member"]


def test_multi_command_boundary_does_not_silently_truncate_sixth_request():
    assert len(BY_ID["multi_two"]["expected"]["proposals"]) == 2
    assert len(BY_ID["multi_five"]["expected"]["proposals"]) == 5
    sixth = BY_ID["multi_six_requires_clarification"]
    assert sixth["input"]["text"].count("создай задачу") == 6
    assert sixth["expected"]["kind"] == "clarify"
    assert sixth["expected"]["proposals"] == []
    assert sixth["expected"]["resolution"] == "split_command_no_silent_truncation"


def test_untrusted_instructions_never_authorize_ids_schema_or_tools():
    cases = [case for case in CASES if "injection" in case["tags"] or "foreign_project" in case["tags"]]
    assert len(cases) >= 5
    for case in cases:
        assert case["expected"]["kind"] == "error"
        assert case["expected"]["resolution"].startswith("reject_")
        assert case["expected"]["proposals"] == []


def test_native_voice_missing_payload_and_file_envelope_are_separate_unqualified_cases():
    native, file = BY_ID["native_voice_missing_payload"], BY_ID["audio_file_envelope_only"]
    assert native["input"]["kind"] == "native_voice" and native["input"]["media_payload_present"] is False
    assert native["expected"]["kind"] == "error"
    assert native["expected"]["resolution"] == "missing_media_payload_offer_file_or_text"
    assert file["input"]["kind"] == "audio_file" and file["input"]["media_payload_present"] is True
    assert file["input"]["media_fixture_only"] is True
    assert file["expected"]["resolution"] == "requires_real_file_payload"
    for case in (native, file):
        assert case["input"]["text"] is None and case["expected"]["proposals"] == []
        assert "url" not in case["input"] and "bytes" not in case["input"]


def test_live_trial_budget_is_inside_monthly_cap_and_has_no_fake_cost_or_price():
    budget = MANIFEST["budget"]
    assert type(budget["monthly_cap_rub"]) is int and budget["monthly_cap_rub"] == 3000
    assert type(budget["live_trial_cap_rub"]) is int
    assert 0 < budget["live_trial_cap_rub"] <= 100 <= budget["monthly_cap_rub"]
    assert budget["live_trial_inside_monthly_cap"] is True
    assert budget["requires_scope_and_remaining_budget"] is True
    assert budget["live_spent_rub"] is None and budget["price_status"] == "NOT_MEASURED"
    assert MANIFEST["limits"] == {"max_proposals": 5, "max_voice_seconds": 120,
        "max_download_bytes": 10 * 1024 * 1024, "max_schema_repairs": 1}
