"""Synthetic normalized WAV + fake inference only; no people or runtime assets."""
import dataclasses
import hashlib
import io
import os
import shutil
import struct
import threading
import wave
from pathlib import Path
from uuid import uuid4

import pytest

from secretary.application.identification import (IdentificationInput, SegmentSnapshot,
    identify_groups, observation_group_id)
from secretary.application.voice_matching import CandidateMaterial, CalibrationSnapshot
from secretary.domain.voice import ModelStamp, VoiceError
from secretary.infrastructure.meeting_voice_audio import ChunkAudioSnapshot, MeetingAudioReader
from secretary.infrastructure.voice_audio import wav_bytes
from secretary.infrastructure.voice_engine import EmbeddingBatch
from secretary.infrastructure.voice_process import ProcessOutput, run_owned

MODEL = ModelStamp("speechbrain/spkrec-ecapa-voxceleb", "a"*40, "b"*64)
VECTOR_A = (1., 0.) + (0.,)*190
VECTOR_B = (0., 1.) + (0.,)*190


class FakeEngine:
    def __init__(self, vectors=None, action=None):
        self.vectors, self.action, self.calls = vectors, action, []

    def embed(self, clips, *, cancel=None):
        self.calls.append(tuple(len(x)//2 for x in clips))
        if self.action:
            self.action(cancel)
        return EmbeddingBatch(MODEL, tuple(self.vectors or [VECTOR_A]*len(clips)), 0)


@pytest.fixture
def setup(tmp_path):
    meeting = str(uuid4())
    samples = struct.pack("<h", 2000)*16000*40
    payload = wav_bytes(samples)
    path = tmp_path / "chunk.wav"
    path.write_bytes(payload)
    chunk = ChunkAudioSnapshot(str(uuid4()), meeting, path, hashlib.sha256(payload).hexdigest(), 120000, 40000, "import")
    profiles = tuple(CandidateMaterial(str(uuid4()), 1, MODEL, (v,)) for v in (VECTOR_A, VECTOR_B))
    speaker = str(uuid4())

    def segment(start=120000, end=122000, **changes):
        return dataclasses.replace(SegmentSnapshot(str(uuid4()), meeting, 1, chunk.chunk_id,
                                   speaker, "import", start, end, "segment", "Original text"), **changes)

    def snapshot(segments=None, **changes):
        return dataclasses.replace(IdentificationInput(meeting, 1, MODEL, (chunk,), tuple(segments or [segment()]), profiles), **changes)

    return tmp_path, chunk, segment, snapshot, profiles


def run(setup, *, segments=None, engine=None, **changes):
    root, _, _, snapshot, _ = setup
    engine = engine or FakeEngine()
    reader = MeetingAudioReader(root)
    output = list(identify_groups(snapshot(segments, **changes), reader, engine))
    return output, reader, engine


def test_exact_offset_source_and_uncalibrated_review(setup):
    root, chunk, segment, snapshot, profiles = setup
    # Different second distinguishes absolute meeting ms from chunk-local ms.
    payload = wav_bytes(struct.pack("<h", 2000)*16000 + struct.pack("<h", 4000)*16000*39)
    chunk.path.write_bytes(payload)
    chunk = dataclasses.replace(chunk, sha256=hashlib.sha256(payload).hexdigest())
    source = segment(121000, 122000, text="Do not alter", timing_precision="word")
    class CheckingEngine(FakeEngine):
        def embed(self, clips, **kwargs):
            assert clips == (struct.pack("<h", 4000)*16000,)
            return super().embed(clips, **kwargs)
    output = list(identify_groups(snapshot([source], chunks=(chunk,)), MeetingAudioReader(root), CheckingEngine()))
    p = output[0].proposals[0]
    assert p.source is source and p.status == "unknown" and p.proposed_profile_id is None
    assert p.candidates[0].profile_id == profiles[0].profile_id and p.candidates[0].raw_score == 1
    assert "uncalibrated" in p.reason_codes and "confirmed" not in repr(output)
    assert "Do not alter" not in repr(p) and str(root) not in repr(output)
    assert output[0].progress.observations_processed == 1


def test_scoped_groups_versions_chunks_channels_and_null(setup):
    _, _, segment, _, _ = setup
    s = segment()
    group = observation_group_id(s)
    assert group == observation_group_id(dataclasses.replace(s, segment_id=str(uuid4())))
    for changes in ({"meeting_id":str(uuid4())}, {"transcript_version":2}, {"chunk_id":str(uuid4())}, {"channel":"microphone"}):
        assert observation_group_id(dataclasses.replace(s, **changes)) != group
    null = dataclasses.replace(s, raw_speaker_id=None)
    assert observation_group_id(null) != observation_group_id(dataclasses.replace(null, segment_id=str(uuid4())))


@pytest.mark.parametrize("changes,reason", [({"timing_precision":"chunk"},"inexact_timing"),
    ({"timing_precision":"unknown","start_ms":None,"end_ms":None},"inexact_timing"),
    ({"start_ms":120000.,"end_ms":122000},"invalid_timing"),
    ({"start_ms":float("nan")},"invalid_timing"),
    ({"start_ms":119000},"invalid_timing"), ({"end_ms":161000},"invalid_timing"),
    ({"end_ms":120500},"short_clip"), ({"quality_ok":False},"bad_quality"), ({"overlap":True},"overlap")])
def test_unusable_never_infers(setup, changes, reason):
    _, _, segment, _, _ = setup
    output, reader, engine = run(setup, segments=[segment(**changes)])
    assert output[0].proposals[0].reason_codes == (reason,)
    assert not engine.calls and reader.counters.bytes_hashed == 0
    assert output[0].progress.observations_skipped == 1


def test_overlap_detected_without_flag_and_touching_is_clean(setup):
    _, _, segment, _, _ = setup
    output, _, engine = run(setup, segments=[segment(), segment(121000,123000,raw_speaker_id=str(uuid4()))])
    assert all(g.proposals[0].reason_codes == ("overlap",) for g in output)
    assert not engine.calls
    output, _, engine = run(setup, segments=[segment(), segment(122000,124000)])
    assert engine.calls == [(32000, 32000)]


def test_overlap_across_chunks_same_channel(setup):
    root, chunk, segment, snapshot, _ = setup
    other = dataclasses.replace(chunk, chunk_id=str(uuid4()))
    output = list(identify_groups(snapshot([segment(),segment(121000,123000,chunk_id=other.chunk_id)], chunks=(chunk,other)), MeetingAudioReader(root), FakeEngine()))
    assert all(g.proposals[0].status == "conflict" for g in output)


def test_group_disagreement_including_uncalibrated(setup):
    _, _, segment, _, _ = setup
    output, _, _ = run(setup, segments=[segment(),segment(123000,125000)], engine=FakeEngine([VECTOR_A,VECTOR_B]))
    assert all(p.status == "conflict" and p.proposed_profile_id is None for p in output[0].proposals)
    assert all("group_disagreement" in p.reason_codes and p.candidates for p in output[0].proposals)


def test_long_observation_disagreement_is_not_averaged_away(setup):
    _, _, segment, _, _ = setup
    output, _, engine = run(setup, segments=[segment(120000,160000)], engine=FakeEngine([VECTOR_A,VECTOR_B,VECTOR_A]))
    assert engine.calls == [(160000,160000,160000)]
    assert output[0].proposals[0].status == "conflict"


def test_bounded_representatives_hash_once_and_real_progress(setup):
    _, _, segment, _, _ = setup
    segments = [segment(120000+i*2000,122000+i*2000) for i in range(20)]
    output, reader, engine = run(setup, segments=segments)
    assert engine.calls == [(32000,)*20]
    assert reader.counters.chunks_hashed == 1 and reader.counters.excerpts_read == 20
    assert output[0].progress.observations_processed == 20
    assert output[0].progress.observations_embedded == 20 and output[0].progress.observations_skipped == 0
    assert all(p.candidates and "uncalibrated" in p.reason_codes for p in output[0].proposals)
    assert all(len(batch)<=32 and sum(batch)<=120*16000 for batch in engine.calls)


def test_hash_once_across_groups(setup):
    _, chunk, segment, _, _ = setup
    output, reader, _ = run(setup, segments=[segment(raw_speaker_id=None),segment(123000,125000,raw_speaker_id=None)])
    assert len(output) == 2 and reader.counters.chunks_hashed == 1
    assert reader.counters.bytes_hashed == chunk.path.stat().st_size


@pytest.mark.parametrize("problem,reason", [("missing","audio_missing"),("hash","audio_hash_mismatch"),
    ("truncated","truncated_audio"),("outside","unsafe_audio_path"),("hardlink","unsafe_audio_path"),
    ("format","invalid_pcm_audio"),("duration","audio_duration_mismatch"),("silent","bad_quality"),("clipped","bad_quality")])
def test_audio_failures_sanitized(setup, problem, reason):
    root, chunk, _, snapshot, _ = setup
    if problem == "missing": chunk.path.unlink()
    elif problem == "hash": chunk = dataclasses.replace(chunk,sha256="c"*64)
    elif problem == "outside": chunk = dataclasses.replace(chunk,path=root.parent / "outside.wav")
    elif problem == "hardlink": (root / "alias.wav").hardlink_to(chunk.path)
    elif problem == "duration": chunk = dataclasses.replace(chunk,duration_ms=39998)
    else:
        payload = chunk.path.read_bytes()
        if problem == "truncated": payload = payload[:-10]
        elif problem == "format": payload = payload[:22] + struct.pack("<H",2) + payload[24:]
        elif problem == "silent": payload = wav_bytes(b"\0\0"*16000*40)
        elif problem == "clipped": payload = wav_bytes(struct.pack("<h",32767)*16000*40)
        chunk.path.write_bytes(payload)
        chunk = dataclasses.replace(chunk,sha256=hashlib.sha256(payload).hexdigest())
    output = list(identify_groups(snapshot(chunks=(chunk,)),MeetingAudioReader(root),FakeEngine()))
    assert output[0].proposals[0].reason_codes == (reason,)
    assert str(root) not in repr(output)


def test_chunk_changed_after_initial_hash(setup):
    root, chunk, _, _, _ = setup
    reader = MeetingAudioReader(root)
    reader.read_excerpt(chunk,0,16000)
    chunk.path.write_bytes(wav_bytes(struct.pack("<h",3000)*16000*40))
    with pytest.raises(VoiceError,match="^audio_changed$"):
        reader.read_excerpt(chunk,0,16000)


def test_no_candidates_does_not_need_audio_or_runtime(setup):
    root, chunk, _, snapshot, _ = setup
    chunk.path.unlink()
    output = list(identify_groups(snapshot(candidates=()),MeetingAudioReader(root),None))
    assert output[0].outcome == "completed" and output[0].proposals[0].reason_codes == ("no_candidates",)


def test_runtime_missing_is_typed_distinct(setup):
    root, _, _, snapshot, _ = setup
    output = list(identify_groups(snapshot(),MeetingAudioReader(root),None))
    assert output[0].outcome == "runtime_unavailable"
    def missing(cancel): raise VoiceError("runtime_missing")
    output, _, _ = run(setup,engine=FakeEngine(action=missing))
    assert output[0].outcome == "runtime_unavailable"


def test_cancel_before_read_during_streaming_and_after_inference(setup):
    root, chunk, _, snapshot, _ = setup
    event = threading.Event(); event.set()
    engine = FakeEngine()
    with pytest.raises(VoiceError,match="^cancelled$"):
        list(identify_groups(snapshot(),MeetingAudioReader(root),engine,cancel=event))
    assert not engine.calls
    class CountingCancel:
        calls = 0
        def is_set(self):
            self.calls += 1
            return self.calls >= 5
    reader = MeetingAudioReader(root)
    with pytest.raises(VoiceError,match="^cancelled$"):
        reader.read_excerpt(chunk,0,16000,cancel=CountingCancel())
    assert 0 < reader.counters.bytes_hashed < chunk.path.stat().st_size
    event.clear()
    with pytest.raises(VoiceError,match="^cancelled$"):
        list(identify_groups(snapshot(),MeetingAudioReader(root),FakeEngine(action=lambda c:c.set()),cancel=event))


@pytest.mark.parametrize("problem",["scope","duplicate_segment","duplicate_chunk","duplicate_profile","model","mutable","calibration"])
def test_invalid_scope_and_material_rejected_before_inference(setup,problem):
    root, chunk, segment, snapshot, profiles = setup
    s = segment(); changes = {}
    if problem == "scope": s=dataclasses.replace(s,meeting_id=str(uuid4()))
    if problem == "duplicate_segment": changes["segments"]=(s,s)
    if problem == "duplicate_chunk": changes["chunks"]=(chunk,chunk)
    if problem == "duplicate_profile": changes["candidates"]=(profiles[0],profiles[0])
    if problem == "model": changes["candidates"]=(dataclasses.replace(profiles[0],model=dataclasses.replace(MODEL,revision="c"*40)),)
    if problem == "mutable": changes["candidates"]=(dataclasses.replace(profiles[0],vectors=[VECTOR_A]),)
    if problem == "calibration": changes["calibration"]=CalibrationSnapshot("v","e",dataclasses.replace(MODEL,revision="c"*40),.5,.1)
    engine = FakeEngine()
    with pytest.raises(VoiceError):
        list(identify_groups(snapshot(changes.pop("segments", [s]),**changes),MeetingAudioReader(root),engine))
    assert not engine.calls


def test_calibrated_proposal_never_confirmed_and_engine_stamp_mismatch(setup):
    output, _, _ = run(setup,calibration=CalibrationSnapshot("v","e",MODEL,.5,.1))
    assert output[0].proposals[0].status == "proposed"
    class WrongEngine(FakeEngine):
        def embed(self,clips,**kwargs):
            return EmbeddingBatch(dataclasses.replace(MODEL,revision="c"*40),(VECTOR_A,)*len(clips),0)
    output, _, _ = run(setup,engine=WrongEngine())
    assert output[0].outcome == "error" and output[0].proposals[0].reason_codes == ("engine_model_mismatch",)


def test_unsafe_symlink_and_reparse_ancestor(setup,monkeypatch):
    root, chunk, _, _, _ = setup
    original = Path.lstat
    class ReparseStat:
        st_mode = 0
        st_file_attributes = 0x400
    def lstat(path, *args, **kwargs):
        return ReparseStat() if path == root else original(path,*args,**kwargs)
    monkeypatch.setattr(Path,"lstat",lstat)
    with pytest.raises(VoiceError,match="^unsafe_audio_path$"):
        MeetingAudioReader(root).read_excerpt(chunk,0,16000)


def test_live_symlink_rejected_if_supported(setup):
    root, chunk, _, _, _ = setup
    link = root / "link.wav"
    try:
        link.symlink_to(chunk.path)
    except OSError:
        pytest.skip("Host does not permit local symlink creation")
    with pytest.raises(VoiceError,match="^unsafe_audio_path$"):
        MeetingAudioReader(root).read_excerpt(dataclasses.replace(chunk,path=link),0,16000)


def test_change_during_stream_hash_even_with_mtime_restored(setup):
    root, chunk, _, _, _ = setup
    initial = chunk.path.stat()
    class MutatingCancel:
        calls = 0
        def is_set(self):
            self.calls += 1
            if self.calls == 5:
                with chunk.path.open("r+b") as file:
                    file.seek(44); file.write(b"\0\0")
                os.utime(chunk.path,ns=(initial.st_atime_ns,initial.st_mtime_ns))
            return False
    with pytest.raises(VoiceError,match="^audio_changed$"):
        MeetingAudioReader(root).read_excerpt(chunk,0,16000,cancel=MutatingCancel())


def test_invalid_intervals_hash_and_malformed_engine_are_sanitized(setup):
    root, chunk, _, _, _ = setup
    reader = MeetingAudioReader(root)
    for begin,end in ((0,15999),(-1,16000),(0,160001),(0.,16000),(700000,716000)):
        with pytest.raises(VoiceError,match="^invalid_audio_interval$"):
            reader.read_excerpt(chunk,begin,end)
    with pytest.raises(VoiceError,match="^invalid_audio_hash$"):
        reader.read_excerpt(dataclasses.replace(chunk,sha256="not a hash"),0,16000)
    class BrokenEngine(FakeEngine):
        def embed(self,*args,**kwargs): return None
    output, _, _ = run(setup,engine=BrokenEngine())
    assert output[0].outcome == "error"
    assert output[0].proposals[0].reason_codes == ("invalid_engine_response",)


def test_cancel_between_groups_and_private_audio_not_retained_at_yield(setup):
    root, _, segment, snapshot, _ = setup
    event = threading.Event()
    iterator = identify_groups(snapshot([segment(raw_speaker_id=None),segment(123000,125000,raw_speaker_id=None)]),MeetingAudioReader(root),FakeEngine(),cancel=event)
    next(iterator)
    values = iterator.gi_frame.f_locals
    assert all(values.get(k) is None for k in ("pcm","batch","vectors","vector","queries"))
    assert "clips" not in values and "owners" not in values
    event.set()
    with pytest.raises(VoiceError,match="^cancelled$"):
        next(iterator)


@pytest.mark.parametrize("duration,count,expected", [(1,70,[32,32,6]),(20,8,[12,12])])
def test_all_observations_across_clip_and_seconds_batch_limits(setup,duration,count,expected):
    root, chunk, segment, snapshot, _ = setup
    seconds = duration*count
    payload = wav_bytes(struct.pack("<h",2000)*16000*seconds)
    chunk.path.write_bytes(payload)
    chunk = dataclasses.replace(chunk,duration_ms=seconds*1000,sha256=hashlib.sha256(payload).hexdigest())
    segments = [segment(120000+i*duration*1000,120000+(i+1)*duration*1000) for i in range(count)]
    class BoundedEngine(FakeEngine):
        def embed(self,clips,**kwargs):
            assert len(clips)<=32 and sum(len(c)//2 for c in clips)<=120*16000
            return super().embed(clips,**kwargs)
    engine = BoundedEngine()
    output = list(identify_groups(snapshot(segments,chunks=(chunk,)),MeetingAudioReader(root),engine))
    assert [len(batch) for batch in engine.calls] == expected
    assert output[0].progress.observations_embedded == count
    assert output[0].progress.observations_skipped == 0
    assert all(p.candidates for p in output[0].proposals)


def test_cancel_after_reader_before_inference(setup):
    root, _, _, snapshot, _ = setup
    event = threading.Event()
    class CancellingReader(MeetingAudioReader):
        def read_excerpt(self,*args,**kwargs):
            pcm = super().read_excerpt(*args,**kwargs)
            event.set()
            return pcm
    engine = FakeEngine()
    with pytest.raises(VoiceError,match="^cancelled$"):
        list(identify_groups(snapshot(),CancellingReader(root),engine,cancel=event))
    assert not engine.calls


def test_overlap_sweep_long_nested_intervals_marks_every_member(setup):
    _, _, segment, _, _ = setup
    segments = [segment(120000,160000),segment(121000,122000),segment(123000,124000)]
    output, _, engine = run(setup,segments=segments)
    assert all(p.reason_codes == ("overlap",) for p in output[0].proposals)
    assert not engine.calls


def native_wav(pcm,rate,channels):
    output = io.BytesIO()
    with wave.open(output,"wb") as audio:
        audio.setnchannels(channels); audio.setsampwidth(2); audio.setframerate(rate)
        audio.writeframes(pcm)
    return output.getvalue()


def make_native(setup, rate=48000,channels=2,seconds=3):
    root, chunk, _, _, _ = setup
    frame = struct.pack("<h",2000)*channels
    payload = native_wav(frame*rate*seconds,rate,channels)
    chunk.path.write_bytes(payload)
    return dataclasses.replace(chunk,duration_ms=seconds*1000,sha256=hashlib.sha256(payload).hexdigest())


def test_native_stereo_offset_and_bounded_owned_conversion_contract(setup):
    root, chunk, _, _, _ = setup
    first = struct.pack("<hh",1000,3000)*48000
    second = struct.pack("<hh",5000,7000)*48000
    payload = native_wav(first+second,48000,2)
    chunk.path.write_bytes(payload)
    chunk = dataclasses.replace(chunk,duration_ms=2000,sha256=hashlib.sha256(payload).hexdigest())
    calls = []
    def runner(command,**kwargs):
        calls.append(command)
        assert "pipe:0" in command and "pipe:1" in command and str(chunk.path) not in command
        assert kwargs["stdout_limit"] == 32000 and kwargs["stderr_limit"] == 65536
        assert kwargs["timeout"] == 30 and kwargs["rss_limit"] == 2*1024**3
        with wave.open(io.BytesIO(kwargs["input_bytes"]),"rb") as audio:
            assert (audio.getframerate(),audio.getnchannels(),audio.getnframes()) == (48000,2,48000)
            assert audio.readframes(48000) == second
        return ProcessOutput(struct.pack("<h",6000)*16000,0,0)
    reader = MeetingAudioReader(root,runner=runner)
    assert reader.read_excerpt(chunk,16000,32000) == struct.pack("<h",6000)*16000
    assert len(calls) == 1 and reader.counters.samples_read == 16000
    assert reader.counters.bytes_hashed == len(payload)


def test_real_owned_ffmpeg_synthetic_48k_stereo_resample_and_downmix(setup):
    assert shutil.which("ffmpeg"), "Existing project ffmpeg required for this synthetic acceptance check"
    root, chunk, _, _, _ = setup
    first = struct.pack("<hh",1000,3000)*48000
    second = struct.pack("<hh",5000,7000)*48000
    payload = native_wav(first+second,48000,2)
    chunk.path.write_bytes(payload)
    chunk = dataclasses.replace(chunk,duration_ms=2000,sha256=hashlib.sha256(payload).hexdigest())
    pcm = MeetingAudioReader(root).read_excerpt(chunk,16000,32000)
    assert len(pcm) == 32000
    values = struct.unpack("<16000h",pcm)
    assert all(abs(x-6000)<=2 for x in values[200:-200])


def test_normalized_fast_path_never_invokes_ffmpeg(setup):
    root, chunk, _, _, _ = setup
    def forbidden(*args,**kwargs): raise AssertionError("No conversion required")
    assert len(MeetingAudioReader(root,runner=forbidden).read_excerpt(chunk,0,16000)) == 32000


@pytest.mark.parametrize("rate,channels",[(7999,1),(192001,1),(48000,9),(48000,0)])
def test_native_header_rate_channels_limits(setup,rate,channels):
    root, chunk, _, _, _ = setup
    payload = bytearray(wav_bytes(struct.pack("<h",2000)*16000*40))
    struct.pack_into("<HHIIHH",payload,20,1,channels,rate,rate*max(1,channels)*2,max(1,channels)*2,16)
    chunk.path.write_bytes(payload)
    chunk = dataclasses.replace(chunk,sha256=hashlib.sha256(payload).hexdigest())
    with pytest.raises(VoiceError,match="^invalid_pcm_audio$"):
        MeetingAudioReader(root).read_excerpt(chunk,0,16000)


@pytest.mark.parametrize("failure,reason",[("process_failed","audio_conversion_failed"),
    ("process_unavailable","audio_conversion_unavailable"),("cancelled","cancelled"),
    ("process_timeout","process_timeout"),("process_output_limit","process_output_limit")])
def test_native_conversion_owned_failure_and_cancel_are_sanitized(setup,failure,reason):
    root, _, _, _, _ = setup
    chunk = make_native(setup)
    def failed(*args,**kwargs): raise VoiceError(failure)
    with pytest.raises(VoiceError,match="^"+reason+"$"):
        MeetingAudioReader(root,runner=failed).read_excerpt(chunk,0,16000)


def test_native_cancel_after_conversion_discards_result(setup):
    root, _, _, _, _ = setup
    chunk = make_native(setup)
    event = threading.Event()
    def runner(*args,**kwargs):
        kwargs["cancel"].set()
        return ProcessOutput(struct.pack("<h",2000)*16000,0,0)
    with pytest.raises(VoiceError,match="^cancelled$"):
        MeetingAudioReader(root,runner=runner).read_excerpt(chunk,0,16000,cancel=event)


def test_native_max_interval_is_bounded_and_bad_output_is_rejected(setup):
    root, _, _, _, _ = setup
    chunk = make_native(setup,rate=192000,channels=8,seconds=10)
    def runner(command,**kwargs):
        assert len(kwargs["input_bytes"]) == 10*192000*8*2 + 44
        assert kwargs["stdout_limit"] == 320000
        return ProcessOutput(struct.pack("<h",2000)*160000,0,0)
    reader = MeetingAudioReader(root,runner=runner)
    assert len(reader.read_excerpt(chunk,0,160000)) == 320000
    with pytest.raises(VoiceError,match="^invalid_audio_interval$"):
        reader.read_excerpt(chunk,0,160001)
    def truncated(command,**kwargs): return ProcessOutput(b"\0\0",0,0)
    with pytest.raises(VoiceError,match="^audio_conversion_length_mismatch$"):
        MeetingAudioReader(root,runner=truncated).read_excerpt(chunk,0,16000)


def test_native_missing_converter_is_distinct_run_outcome(setup):
    root, _, segment, snapshot, _ = setup
    chunk = make_native(setup)
    def missing(*args,**kwargs): raise VoiceError("process_unavailable")
    output = list(identify_groups(snapshot([segment()],chunks=(chunk,)),MeetingAudioReader(root,runner=missing),FakeEngine()))
    assert output[0].outcome == "runtime_unavailable"
    assert output[0].proposals[0].reason_codes == ("audio_conversion_unavailable",)


def test_native_real_nonzero_exit_is_sanitized(setup):
    import sys
    root, _, _, _, _ = setup
    chunk = make_native(setup)
    # Exercise real owned nonzero process cleanup without a fixture executable,
    # network or native device. The conversion runner injection changes only
    # this isolated subprocess command; ordinary operation still uses FFmpeg.
    def nonzero(command,**kwargs):
        return run_owned([sys.executable,"-c","raise SystemExit(7)"],**kwargs)
    with pytest.raises(VoiceError,match="^audio_conversion_failed$"):
        MeetingAudioReader(root,runner=nonzero).read_excerpt(chunk,0,16000)


def test_native_cancel_before_process_dispatch(setup):
    root, _, _, _, _ = setup
    chunk = make_native(setup)
    event = threading.Event()
    class CancellingReader(MeetingAudioReader):
        def _normalize(self,pcm,channels,rate,target_samples,cancel):
            event.set()
            return super()._normalize(pcm,channels,rate,target_samples,cancel)
    def forbidden(*args,**kwargs): raise AssertionError("Cancelled conversion dispatched")
    with pytest.raises(VoiceError,match="^cancelled$"):
        CancellingReader(root,runner=forbidden).read_excerpt(chunk,0,16000,cancel=event)


@pytest.mark.parametrize("native",[False,True])
def test_revalidate_unchanged_originals_no_rehash_or_conversion(setup,native):
    root, chunk, _, _, _ = setup
    if native:
        chunk = make_native(setup)
    calls = []
    def runner(command,**kwargs):
        calls.append(command)
        return ProcessOutput(struct.pack("<h",2000)*16000,0,0)
    reader = MeetingAudioReader(root,runner=runner)
    reader.read_excerpt(chunk,0,16000)
    counters = reader.counters
    call_count = len(calls)
    assert reader.revalidate() is None
    assert reader.counters == counters and len(calls) == call_count


def test_revalidate_detects_change_after_last_read_and_inference(setup):
    root, chunk, _, snapshot, _ = setup
    reader = MeetingAudioReader(root)
    output = list(identify_groups(snapshot(),reader,FakeEngine()))
    assert output[0].progress.observations_embedded == 1
    initial = chunk.path.stat()
    with chunk.path.open("r+b") as file:
        file.seek(44); file.write(b"\0\0")
    # Descriptor change-time check must still notice this after mtime restore.
    os.utime(chunk.path,ns=(initial.st_atime_ns,initial.st_mtime_ns))
    counters = reader.counters
    with pytest.raises(VoiceError,match="^audio_changed$"):
        reader.revalidate()
    assert reader.counters == counters


def test_revalidate_cancel_empty_before_and_during_verified_checks(setup):
    root, chunk, _, _, _ = setup
    reader = MeetingAudioReader(root)
    assert reader.revalidate() is None
    event = threading.Event(); event.set()
    with pytest.raises(VoiceError,match="^cancelled$"):
        reader.revalidate(cancel=event)
    reader.read_excerpt(chunk,0,16000)
    class CountingCancel:
        calls = 0
        def is_set(self):
            self.calls += 1
            return self.calls >= 4
    with pytest.raises(VoiceError,match="^cancelled$"):
        reader.revalidate(cancel=CountingCancel())


@pytest.mark.parametrize("change,reason",[("missing","audio_missing"),("hardlink","unsafe_audio_path"),
                                         ("replacement","audio_changed")])
def test_revalidate_reuses_scoped_path_guards(setup,change,reason):
    root, chunk, _, _, _ = setup
    reader = MeetingAudioReader(root)
    reader.read_excerpt(chunk,0,16000)
    if change == "missing":
        chunk.path.unlink()
    elif change == "hardlink":
        (root / "alias.wav").hardlink_to(chunk.path)
    else:
        replacement = root / "replacement.wav"
        replacement.write_bytes(chunk.path.read_bytes())
        os.replace(replacement,chunk.path)
    with pytest.raises(VoiceError,match="^"+reason+"$"):
        reader.revalidate()


@pytest.mark.parametrize("native,frames,expected_samples",[(False,31997,31997),(False,32003,32000),
                                                          (True,95990,31996),(True,96010,32000)])
def test_review_f1_rounded_eof_keeps_real_audio_and_source(setup,native,frames,expected_samples):
    root, chunk, segment, snapshot, _ = setup
    rate, channels = (48000,2) if native else (16000,1)
    payload = native_wav(struct.pack("<h",2000)*channels*frames,rate,channels)
    chunk.path.write_bytes(payload)
    chunk = dataclasses.replace(chunk,duration_ms=2000,sha256=hashlib.sha256(payload).hexdigest())
    source = segment(120000,122000)
    engine = FakeEngine()
    reader = MeetingAudioReader(root)
    output = list(identify_groups(snapshot([source],chunks=(chunk,)),reader,engine))
    assert engine.calls == [(expected_samples,)]
    assert output[0].progress.observations_embedded == 1
    assert output[0].proposals[0].source is source
    assert "uncalibrated" in output[0].proposals[0].reason_codes
    assert reader.counters.samples_read == expected_samples
    assert chunk.path.read_bytes() == payload


@pytest.mark.parametrize("native,frames",[(False,31997),(True,95990)])
def test_review_f1_clamped_actual_subsecond_is_honestly_short(setup,native,frames):
    root, chunk, segment, snapshot, _ = setup
    rate, channels = (48000,2) if native else (16000,1)
    payload = native_wav(struct.pack("<h",2000)*channels*frames,rate,channels)
    chunk.path.write_bytes(payload)
    chunk = dataclasses.replace(chunk,duration_ms=2000,sha256=hashlib.sha256(payload).hexdigest())
    engine = FakeEngine()
    output = list(identify_groups(snapshot([segment(121000,122000)],chunks=(chunk,)),MeetingAudioReader(root),engine))
    assert output[0].proposals[0].reason_codes == ("short_clip",)
    assert not engine.calls


@pytest.mark.parametrize("vectors",[None,1,"private response",{0:VECTOR_A},(),(VECTOR_A,VECTOR_A)])
def test_review_f2_malformed_typed_batch_is_sanitized(setup,vectors):
    root, _, _, snapshot, _ = setup
    class Malformed(FakeEngine):
        def embed(self,*args,**kwargs): return EmbeddingBatch(MODEL,vectors,0)
    iterator = identify_groups(snapshot(),MeetingAudioReader(root),Malformed())
    output = next(iterator)
    assert output.outcome == "error" and output.progress.observations_embedded == 0
    assert output.proposals[0].status == "unknown"
    assert output.proposals[0].reason_codes == ("invalid_engine_response",)
    assert "private response" not in repr(output)
    assert "clips" not in iterator.gi_frame.f_locals


@pytest.mark.parametrize("native",[False,True])
def test_review_f1_true_out_of_scope_eof_and_truncation_still_rejected(setup,native):
    root, _, _, _, _ = setup
    chunk = make_native(setup,rate=48000 if native else 16000,channels=2 if native else 1,seconds=2)
    reader = MeetingAudioReader(root)
    # The snapshot's exact window cannot be enlarged by rounding tolerance.
    with pytest.raises(VoiceError,match="^invalid_audio_interval$"):
        reader.read_excerpt(chunk,16016,32016)
    # >1ms WAV-vs-snapshot discrepancy is not acceptable rounding.
    with pytest.raises(VoiceError,match="^audio_duration_mismatch$"):
        reader.read_excerpt(dataclasses.replace(chunk,duration_ms=2002),0,32000)
    payload = chunk.path.read_bytes()[:-100]
    chunk.path.write_bytes(payload)
    chunk = dataclasses.replace(chunk,sha256=hashlib.sha256(payload).hexdigest())
    with pytest.raises(VoiceError,match="^truncated_audio$"):
        MeetingAudioReader(root).read_excerpt(chunk,0,32000)


def test_review_f1_matcher_duration_counts_actual_pcm(setup,monkeypatch):
    import secretary.application.identification as core
    root, chunk, segment, snapshot, _ = setup
    payload = wav_bytes(struct.pack("<h",2000)*31997)
    chunk.path.write_bytes(payload)
    chunk = dataclasses.replace(chunk,duration_ms=2000,sha256=hashlib.sha256(payload).hexdigest())
    durations = []
    original = core.match_voice
    def capture(*args,**kwargs):
        durations.append(kwargs["duration_samples"])
        return original(*args,**kwargs)
    monkeypatch.setattr(core,"match_voice",capture)
    list(core.identify_groups(snapshot([segment()],chunks=(chunk,)),MeetingAudioReader(root),FakeEngine()))
    assert durations[-1] == 31997 and 32000 not in durations
