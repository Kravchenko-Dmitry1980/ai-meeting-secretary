"""Parser rules for one-click assignment. No network and no meeting text."""
from __future__ import annotations

import json

import pytest

from secretary.application.assignment_autofill import (
    ASSIGNMENT_MODEL, assignment_key, assignment_payload, owner_changes,
)
from secretary.application.assignment_resolver import (
    resolve_assignment, revalidate_assignment, review_assignment,
)
from secretary.domain.assignment_evidence import DirectoryPerson, RosterParticipant
from tests.test_assignment_resolver import command, fixture

PAVEL = RosterParticipant(meeting_id="m", participant_id="pavel", display_name="Павел",
                          aliases=("Паша", "Павлик"))
ALEX = RosterParticipant(meeting_id="m", participant_id="alex", display_name="Алексей")
SVETA = DirectoryPerson(profile_id="sveta", display_name="Светлана", aliases=("Света",))


def test_owner_changes_keep_one_known_person_all_or_nobody():
    raw = json.dumps({"assignments": [
        {"action_id": "a", "choice": "person", "name": "Павлик"},
        {"action_id": "a", "choice": "person", "name": "Алексей"},
        {"action_id": "b", "choice": "all", "name": ""},
        {"action_id": "c", "choice": "unassigned", "name": ""},
        {"action_id": "d", "choice": "person", "name": "Света"},
        {"action_id": "e", "choice": "person", "name": "Никто"},
        {"action_id": "f", "choice": "unsure", "name": "Павел"},
        {"action_id": "g", "choice": "person", "name": "я"},
        {"action_id": "missing", "choice": "all", "name": ""},
    ]}, ensure_ascii=False)
    changes = owner_changes(raw, {"a", "b", "c", "d", "e", "f", "g"}, (PAVEL, ALEX), (SVETA,), "m")
    by_id = {change.action_id: change for change in changes}
    assert set(by_id) == {"a", "b", "c", "d"}
    assert (by_id["a"].decision, by_id["a"].participant_id) == ("apply_automatic", "pavel")
    assert by_id["b"].decision == "assign_all"
    assert by_id["c"].decision == "leave_unassigned"
    assert by_id["d"].assignee_profile_id == "sveta"
    assert by_id["d"].participant_id is None


def test_assignment_payload_rejects_an_oversized_batch():
    roster = [{"display_name": "Павел", "aliases": [], "attended": True}]
    tasks = [{"action_id": str(index), "text": "задача " * 40, "quote": "цитата " * 40, "flag": "ambiguous"}
             for index in range(180)]
    with pytest.raises(ValueError, match="assignment_request_too_large"):
        assignment_payload("openai/gpt-4.1-mini", roster, tasks)
    payload = assignment_payload("openai/gpt-4.1-mini", roster, tasks[:2])
    assert payload["model"] == "openai/gpt-4.1-mini"
    assert assignment_key(payload) == assignment_key(payload)
    assert "Павел" in payload["messages"][1]["content"]
    assert "Назначь ответственного" not in payload["messages"][1]["content"]
    luna = assignment_payload(ASSIGNMENT_MODEL, roster, tasks[:1])
    assert luna["model"] == "openai/gpt-6-luna-pro"
    assert "temperature" not in luna
    assert luna["max_tokens"] == 16000


def test_automatic_choice_stays_until_a_person_changes_it():
    action, context = fixture(text="Подготовить отчёт", basis="unknown", semantic="ambiguous")
    resolved = resolve_assignment(action, context)
    reviewed = review_assignment(resolved, command(resolved, context, "apply_automatic", "pavel"), context)
    assert reviewed.status == "confirmed"
    assert reviewed.reason_codes == ("automatic_assignment",)
    again = revalidate_assignment(reviewed, context)
    assert again.status == "confirmed"
    assert again.participant_id == "pavel"
    assert again.reason_codes == ("automatic_assignment",)
