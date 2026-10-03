"""Local, scoped voice review core. Never confirms an identity.

Task4b must hold the shared inference coordinator across iteration, register all
audio/material/model/calibration dependencies, and revalidate scope, consent,
revisions and manual precedence transactionally before any publication. This
module has no database, job, consent, roster or publication authority.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from typing import Iterator, Protocol

from secretary.application.voice_matching import CandidateMaterial, CalibrationSnapshot, RankedCandidate, match_voice
from secretary.domain.voice import ModelStamp, VoiceError, canonical_uuid, valid_vector
from secretary.infrastructure.meeting_voice_audio import ChunkAudioSnapshot, MeetingAudioReader, check_cancel, technical_quality
from secretary.infrastructure.voice_engine import EmbeddingBatch

CHANNELS = {"mixed", "microphone", "system", "import"}
RUNTIME_UNAVAILABLE = {"runtime_missing", "runtime_unverified", "runtime_incompatible", "runtime_monitor_unavailable", "audio_conversion_unavailable"}


@dataclass(frozen=True, repr=False)
class SegmentSnapshot:
    segment_id: str
    meeting_id: str
    transcript_version: int
    chunk_id: str
    raw_speaker_id: str | None
    channel: str
    start_ms: int | None
    end_ms: int | None
    timing_precision: str
    text: str
    overlap: bool = False
    quality_ok: bool = True


@dataclass(frozen=True, repr=False)
class IdentificationInput:
    meeting_id: str
    transcript_version: int
    model: ModelStamp
    chunks: tuple[ChunkAudioSnapshot, ...]
    segments: tuple[SegmentSnapshot, ...]
    candidates: tuple[CandidateMaterial, ...]
    calibration: CalibrationSnapshot | None = None


@dataclass(frozen=True)
class SegmentProposal:
    segment_id: str
    group_id: str
    status: str
    proposed_profile_id: str | None
    raw_score: float | None
    reason_codes: tuple[str, ...]
    candidates: tuple[RankedCandidate, ...]
    source: SegmentSnapshot = field(repr=False)


@dataclass(frozen=True)
class IdentificationProgress:
    observations_processed: int
    observations_embedded: int
    observations_skipped: int
    inference_batches: int
    excerpts_embedded: int
    elapsed_seconds: float


@dataclass(frozen=True)
class IdentificationGroupResult:
    group_id: str
    proposals: tuple[SegmentProposal, ...]
    progress: IdentificationProgress
    outcome: str  # completed, runtime_unavailable, error; cancellation raises


class EmbeddingEngine(Protocol):
    def embed(self, clips, *, cancel=None): ...


def observation_group_id(segment: SegmentSnapshot) -> str:
    """A raw label is local to this chunk/version/channel; null is per segment."""
    value = (segment.meeting_id, segment.transcript_version, segment.chunk_id,
             segment.channel, segment.raw_speaker_id,
             segment.segment_id if segment.raw_speaker_id is None else None)
    return "vg_" + hashlib.sha256(json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _validate_snapshot(snapshot):
    if (not isinstance(snapshot, IdentificationInput) or type(snapshot.transcript_version) is not int
            or snapshot.transcript_version < 1 or not isinstance(snapshot.model, ModelStamp)
            or type(snapshot.chunks) is not tuple or type(snapshot.segments) is not tuple
            or type(snapshot.candidates) is not tuple
            or (snapshot.calibration is not None and not isinstance(snapshot.calibration, CalibrationSnapshot))):
        raise VoiceError("invalid_identification_snapshot")
    canonical_uuid(snapshot.meeting_id)
    chunks, seen = {}, set()
    for chunk in snapshot.chunks:
        if (not isinstance(chunk, ChunkAudioSnapshot) or chunk.chunk_id in chunks
                or chunk.meeting_id != snapshot.meeting_id or chunk.channel not in CHANNELS
                or type(chunk.offset_ms) is not int or chunk.offset_ms < 0
                or type(chunk.duration_ms) is not int or chunk.duration_ms <= 0):
            raise VoiceError("invalid_chunk_scope")
        canonical_uuid(chunk.chunk_id)
        chunks[chunk.chunk_id] = chunk
    for s in snapshot.segments:
        if (not isinstance(s, SegmentSnapshot) or s.segment_id in seen
                or s.meeting_id != snapshot.meeting_id or s.transcript_version != snapshot.transcript_version
                or s.chunk_id not in chunks or s.channel != chunks[s.chunk_id].channel
                or not isinstance(s.text, str) or type(s.overlap) is not bool or type(s.quality_ok) is not bool):
            raise VoiceError("invalid_segment_scope")
        canonical_uuid(s.segment_id)
        if s.raw_speaker_id is not None:
            canonical_uuid(s.raw_speaker_id)
        seen.add(s.segment_id)
    # Validate all material before inference. Query is a synthetic shape-only
    # vector; no scoring metadata from this validation reaches the result.
    if any(not isinstance(c, CandidateMaterial) or type(c.vectors) is not tuple
           or any(type(v) is not tuple for v in c.vectors) for c in snapshot.candidates):
        raise VoiceError("invalid_candidates")
    probe = match_voice(((1.0,) + (0.0,) * 191,), snapshot.candidates,
                        model=snapshot.model, calibration=snapshot.calibration, duration_samples=16000)
    if "invalid_candidates" in probe.reasons:
        raise VoiceError("invalid_candidates")
    if snapshot.calibration is not None and snapshot.calibration.model != snapshot.model:
        raise VoiceError("calibration_model_mismatch")
    return chunks


def _timing_reason(segment, chunk):
    if segment.timing_precision not in {"word", "segment"}:
        return "inexact_timing"
    if (type(segment.start_ms) is not int or type(segment.end_ms) is not int
            or not chunk.offset_ms <= segment.start_ms < segment.end_ms <= chunk.offset_ms + chunk.duration_ms):
        return "invalid_timing"
    if segment.end_ms - segment.start_ms < 1000:
        return "short_clip"
    return None


def _contaminated(segments, chunks):
    # A sweep across the meeting, including chunk boundaries. Only physical
    # channel is used: two labels on one channel overlapping are contaminated.
    by_channel = {}
    for s in segments:
        if s.timing_precision in {"word", "segment"} and _timing_reason(s, chunks[s.chunk_id]) in {None, "short_clip"}:
            by_channel.setdefault(s.channel, []).append(s)
    bad = set()
    for items in by_channel.values():
        furthest = None
        for s in sorted(items, key=lambda x: (x.start_ms, x.end_ms, x.segment_id)):
            if furthest is not None and furthest.end_ms > s.start_ms:
                bad.add(s.segment_id)
                bad.add(furthest.segment_id)
            if furthest is None or s.end_ms > furthest.end_ms:
                furthest = s
    return bad


def _windows(segment, chunk, count):
    start = (segment.start_ms - chunk.offset_ms) * 16
    length = (segment.end_ms - segment.start_ms) * 16
    width = min(length, 160000)
    if count == 1 or length == width:
        return [(start + (length-width)//2, start + (length+width)//2)]
    positions = sorted({0, (length-width)//2, length-width})
    return [(start+p, start+p+width) for p in positions[:count]]


def _read_observation(reader, chunk, windows, cancel):
    clips = []
    for begin, end in windows:
        check_cancel(cancel)
        pcm = reader.read_excerpt(chunk, begin, end, cancel=cancel)
        check_cancel(cancel)
        if not technical_quality(pcm):
            raise VoiceError("bad_quality")
        clips.append(pcm)
    return clips


def _embed_matches(engine, clips, owners, snapshot, cancel):
    check_cancel(cancel)
    batch = engine.embed(tuple(clips), cancel=cancel)
    check_cancel(cancel)
    if not isinstance(batch, EmbeddingBatch):
        raise VoiceError("invalid_engine_response")
    if batch.model != snapshot.model:
        raise VoiceError("engine_model_mismatch")
    if (type(batch.vectors) not in (tuple, list) or not 1 <= len(batch.vectors) <= 32
            or len(batch.vectors) != len(clips)
            or any(not valid_vector(v) for v in batch.vectors)):
        raise VoiceError("invalid_engine_response")
    queries, winners = {}, set()
    for owner, vector in zip(owners, batch.vectors):
        check_cancel(cancel)
        queries.setdefault(owner[0], []).append(vector)
        review = match_voice((vector,), snapshot.candidates, model=snapshot.model, duration_samples=16000)
        if review.candidates and (len(review.candidates) == 1 or review.candidates[0].raw_score > review.candidates[1].raw_score):
            winners.add(review.candidates[0].profile_id)
    results = {}
    for segment_id, vectors in queries.items():
        check_cancel(cancel)
        duration = sum(n for sid, n in owners if sid == segment_id)
        result = match_voice(tuple(vectors), snapshot.candidates, model=snapshot.model,
                             calibration=snapshot.calibration, duration_samples=duration)
        results[segment_id] = replace(result, candidates=result.candidates[:5])
    # Only scores/IDs leave this call; all private inference frames die here.
    return results, winners


def identify_groups(snapshot: IdentificationInput, reader: MeetingAudioReader,
                    engine: EmbeddingEngine | None, *, cancel=None) -> Iterator[IdentificationGroupResult]:
    """Yield small results one scoped group at a time in snapshot order.

    All eligible observations are read in batches <=32 clips / 120 seconds;
    each observation uses at most three 1..10s excerpts. Identity is never copied
    from another observation. Only current batch PCM/vectors live in inference.
    Caller may stream results to its
    run store; it must not mistake partial iteration for successful completion.
    Cancellation raises VoiceError('cancelled'), including after inference.
    """
    check_cancel(cancel)
    chunks = _validate_snapshot(snapshot)
    contaminated = _contaminated(snapshot.segments, chunks)
    groups = {}
    for s in snapshot.segments:
        groups.setdefault(observation_group_id(s), []).append(s)
    processed = embedded = skipped = batches = excerpts = 0
    started = time.monotonic()
    for group_id, members in groups.items():
        check_cancel(cancel)
        reasons = {}
        eligible = []
        for s in members:
            reason = _timing_reason(s, chunks[s.chunk_id])
            if s.overlap or s.segment_id in contaminated:
                reason = "overlap"
            elif not s.quality_ok:
                reason = "bad_quality"
            if reason:
                reasons[s.segment_id] = reason
            else:
                eligible.append(s)
        matched, outcome, excerpt_winners = {}, "completed", set()
        clips, owners = [], []
        failure = None

        def flush():
            nonlocal embedded, batches, excerpts, outcome, failure
            if not clips:
                return
            check_cancel(cancel)
            batches += 1
            try:
                results, winners = _embed_matches(engine, clips, owners, snapshot, cancel)
                matched.update(results)
                excerpt_winners.update(winners)
                embedded += len(results)
                excerpts += len(clips)
            except VoiceError as error:
                if error.reason == "cancelled":
                    raise
                failure = error.reason
                outcome = "runtime_unavailable" if failure in RUNTIME_UNAVAILABLE else "error"
                for segment_id, _ in owners:
                    reasons[segment_id] = failure
            finally:
                clips.clear()
                owners.clear()

        for s in eligible:
            check_cancel(cancel)
            if not snapshot.candidates:
                reasons[s.segment_id] = "no_candidates"
                continue
            if engine is None:
                reasons[s.segment_id] = "runtime_missing"
                outcome = "runtime_unavailable"
                continue
            windows = _windows(s, chunks[s.chunk_id], 3)
            samples = sum(end-begin for begin, end in windows)
            if len(clips) + len(windows) > 32 or sum(n for _, n in owners) + samples > 120*16000:
                flush()
            if failure:
                reasons[s.segment_id] = failure
                continue
            try:
                # A rejected excerpt rejects the observation as a whole.
                observation_clips = _read_observation(reader, chunks[s.chunk_id], windows, cancel)
                clips.extend(observation_clips)
                owners.extend((s.segment_id, len(pcm)//2) for pcm in observation_clips)
                del observation_clips
            except VoiceError as error:
                if error.reason == "cancelled":
                    raise
                reasons[s.segment_id] = error.reason
                if error.reason in RUNTIME_UNAVAILABLE:
                    outcome = "runtime_unavailable"
        flush()
        del flush, clips, owners
        # Calibrated disagreement and uncalibrated unique rank disagreement
        # both require group review. No mean/dominant winner can erase them.
        winners = set()
        for result in matched.values():
            if result.candidates and (len(result.candidates) == 1 or result.candidates[0].raw_score > result.candidates[1].raw_score):
                winners.add(result.candidates[0].profile_id)
        group_conflict = len(winners | excerpt_winners) > 1 or any(r.status == "conflict" for r in matched.values())
        proposals = []
        for s in members:
            result = matched.get(s.segment_id)
            reason = reasons.get(s.segment_id)
            if reason:
                status, profile, score, codes, candidates = ("conflict" if reason == "overlap" else "unknown"), None, None, (reason,), ()
            elif result:
                status, profile, score, codes, candidates = result.status, result.proposed_profile_id, result.raw_score, result.reasons, result.candidates[:5]
            else:
                status, profile, score, codes, candidates = "unknown", None, None, ("no_usable_excerpt",), ()
            if group_conflict:
                status, profile, codes = "conflict", None, tuple(dict.fromkeys((*codes, "group_disagreement")))
            proposals.append(SegmentProposal(s.segment_id, group_id, status, profile, score, codes, candidates, s))
        processed += len(members)
        skipped = processed - embedded
        progress = IdentificationProgress(processed, embedded, skipped, batches, excerpts, time.monotonic()-started)
        check_cancel(cancel)
        yield IdentificationGroupResult(group_id, tuple(proposals), progress, outcome)
