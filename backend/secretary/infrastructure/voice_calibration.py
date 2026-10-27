"""Operator-provided calibration for local voice proposals.

The file holds only a threshold, a margin and provenance labels. It enables the
"proposed" status for human review; it never confirms identity and does not
qualify the model for production.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from secretary.application.voice_matching import CalibrationSnapshot
from secretary.domain.enrollment import EnrollmentFailure
from secretary.domain.voice import VoiceError

logger = logging.getLogger(__name__)

CALIBRATION_FILE = "voice_calibration.json"


def load_calibration(data_dir: Path, enrollments) -> CalibrationSnapshot | None:
    path = Path(data_dir) / CALIBRATION_FILE
    if enrollments is None or not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return CalibrationSnapshot(raw["revision"], raw["evidence_id"], enrollments.model(),
                                   float(raw["threshold"]), float(raw["margin"]))
    except (OSError, ValueError, KeyError, TypeError, VoiceError, EnrollmentFailure) as exc:
        logger.warning("Voice calibration ignored (%s): proposals stay uncalibrated", type(exc).__name__)
        return None
