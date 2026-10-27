"""Operator calibration file: a valid file enables proposals; anything else stays uncalibrated."""
import json

import pytest

from secretary.domain.voice import ModelStamp
from secretary.infrastructure.voice_calibration import CALIBRATION_FILE, load_calibration

MODEL = ModelStamp("speechbrain/spkrec-ecapa-voxceleb", "a"*40, "b"*64)
VALID = {"revision": "provisional-1", "evidence_id": "operator-review", "threshold": 0.4, "margin": 0.05}


class Enrollments:
    def model(self):
        return MODEL


def test_valid_file_is_bound_to_the_pinned_model(tmp_path):
    (tmp_path / CALIBRATION_FILE).write_text(json.dumps(VALID), encoding="utf-8")
    calibration = load_calibration(tmp_path, Enrollments())
    assert calibration.model == MODEL
    assert (calibration.revision, calibration.threshold, calibration.margin) == ("provisional-1", 0.4, 0.05)


@pytest.mark.parametrize("content", [json.dumps({**VALID, "threshold": 2}), json.dumps({**VALID, "revision": 7}),
                                     '{"revision": "r"}', "not json", "[]"])
def test_invalid_file_keeps_proposals_uncalibrated(tmp_path, content):
    (tmp_path / CALIBRATION_FILE).write_text(content, encoding="utf-8")
    assert load_calibration(tmp_path, Enrollments()) is None


def test_missing_file_or_voice_runtime_stays_uncalibrated(tmp_path):
    assert load_calibration(tmp_path, Enrollments()) is None
    (tmp_path / CALIBRATION_FILE).write_text(json.dumps(VALID), encoding="utf-8")
    assert load_calibration(tmp_path, None) is None
