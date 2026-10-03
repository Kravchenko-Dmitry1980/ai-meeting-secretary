"""Pure review proposals; raw cosine is never an identity probability."""
from __future__ import annotations

import math
from dataclasses import dataclass

from secretary.domain.voice import ModelStamp, VoiceError, canonical_uuid, valid_vector

ALGORITHM_REVISION = "mean-cosine-v1"


@dataclass(frozen=True, repr=False)
class CandidateMaterial:
    profile_id: str
    material_revision: int
    model: ModelStamp
    vectors: tuple[tuple[float, ...], ...]


@dataclass(frozen=True)
class CalibrationSnapshot:
    revision: str
    evidence_id: str
    model: ModelStamp
    threshold: float
    margin: float
    algorithm_revision: str = ALGORITHM_REVISION

    def __post_init__(self):
        if (not isinstance(self.revision, str) or not 1 <= len(self.revision) <= 128
                or not isinstance(self.evidence_id, str) or not 1 <= len(self.evidence_id) <= 128
                or self.algorithm_revision != ALGORITHM_REVISION
                or type(self.threshold) not in (int, float) or not -1 <= self.threshold <= 1
                or not math.isfinite(self.threshold)
                or type(self.margin) not in (int, float) or not 0 <= self.margin <= 2
                or not math.isfinite(self.margin)):
            raise VoiceError("invalid_calibration")


@dataclass(frozen=True)
class RankedCandidate:
    profile_id: str
    material_revision: int
    raw_score: float


@dataclass(frozen=True)
class MatchResult:
    status: str
    proposed_profile_id: str | None
    raw_score: float | None
    second_best_margin: float | None
    reasons: tuple[str, ...]
    candidates: tuple[RankedCandidate, ...]
    model: ModelStamp
    calibration_revision: str | None
    algorithm_revision: str = ALGORITHM_REVISION


def cosine(left, right) -> float:
    if not valid_vector(left) or not valid_vector(right):
        raise VoiceError("invalid_vector")
    # Normalize first, avoiding overflow even for finite huge input components.
    amax, bmax = max(abs(x) for x in left), max(abs(x) for x in right)
    a, b = [x/amax for x in left], [x/bmax for x in right]
    return max(-1.0, min(1.0, sum(x*y for x, y in zip(a,b)) /
                             math.sqrt(sum(x*x for x in a)*sum(y*y for y in b))))


def match_voice(query_vectors, candidates: tuple[CandidateMaterial, ...] | list[CandidateMaterial], *,
                model: ModelStamp, calibration: CalibrationSnapshot | None = None,
                duration_samples: int, overlap: bool = False, quality_ok: bool = True) -> MatchResult:
    """Caller supplies immutable, consent-confirmed candidate snapshots.

    Lifecycle/revision revalidation belongs to callers. Minimum query duration is
    1 second; technical eligibility does not establish speech or clean identity.
    """
    ranked = []
    revision = calibration.revision if calibration else None
    def result(status, reasons, profile=None, margin=None):
        return MatchResult(status, profile, ranked[0].raw_score if ranked else None, margin,
                           tuple(reasons), tuple(ranked), model, revision)
    if overlap:
        return result("conflict", ["overlap"])
    if type(duration_samples) is not int or duration_samples < 16000:
        return result("unknown", ["short_clip"])
    if quality_ok is not True:
        return result("unknown", ["bad_quality"])
    if (not isinstance(query_vectors, (tuple, list)) or not 1 <= len(query_vectors) <= 32
            or any(not valid_vector(v) for v in query_vectors)):
        return result("unknown", ["invalid_vector"])
    if not isinstance(candidates, (tuple, list)) or len(candidates) > 100:
        return result("unknown", ["invalid_candidates"])
    seen = set()
    for candidate in candidates:
        try:
            canonical_uuid(candidate.profile_id)
            if (candidate.profile_id in seen or type(candidate.material_revision) is not int
                    or candidate.material_revision < 0 or candidate.model != model
                    or not 1 <= len(candidate.vectors) <= 3
                    or any(not valid_vector(v) for v in candidate.vectors)):
                return result("unknown", ["invalid_candidates"])
        except (VoiceError, AttributeError, TypeError):
            return result("unknown", ["invalid_candidates"])
        seen.add(candidate.profile_id)
        score = sum(cosine(q, v) for q in query_vectors for v in candidate.vectors) / (len(query_vectors)*len(candidate.vectors))
        ranked.append(RankedCandidate(candidate.profile_id, candidate.material_revision, score))
    ranked.sort(key=lambda c: (-c.raw_score, c.profile_id))
    if not ranked:
        return result("unknown", ["no_candidates"])
    margin = ranked[0].raw_score-ranked[1].raw_score if len(ranked) > 1 else None
    if calibration is None:
        return result("unknown", ["uncalibrated", "human_review_required"], margin=margin)
    if calibration.model != model:
        return result("unknown", ["calibration_model_mismatch"], margin=margin)
    if margin is None:
        return result("unknown", ["second_candidate_missing", "human_review_required"])
    if ranked[0].raw_score < calibration.threshold:
        return result("unknown", ["below_calibrated_threshold"], margin=margin)
    if margin <= 0 or margin < calibration.margin:
        return result("conflict", ["ambiguous_candidates"], margin=margin)
    # Disagreement across technically usable excerpts stays a conflict.
    winners = set()
    for query in query_vectors:
        scores = [(c.profile_id, sum(cosine(query,v) for v in c.vectors)/len(c.vectors))
                  for c in candidates]
        best = max(score for _,score in scores)
        excerpt_winners = [profile_id for profile_id,score in scores if score == best]
        if len(excerpt_winners) != 1:
            return result("conflict", ["ambiguous_excerpt"], margin=margin)
        winners.add(excerpt_winners[0])
    if len(winners) != 1:
        return result("conflict", ["excerpt_disagreement"], margin=margin)
    return result("proposed", ["human_review_required"], ranked[0].profile_id, margin)
