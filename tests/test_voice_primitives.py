"""Synthetic private boundaries; no user audio, provider, DB or device access."""
import array
import base64
import importlib.util
import io
import json
import math
import os
import subprocess
import sys
import threading
import time
import wave
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from secretary.application.voice_matching import CalibrationSnapshot, CandidateMaterial, cosine, match_voice
from secretary.domain.voice import MAX_PAYLOAD, ModelStamp, PrivateMaterial, VoiceError
from secretary.infrastructure.voice_audio import EnrollmentDecoder, screen_pcm, wav_bytes
from secretary.infrastructure.voice_engine import LocalVoiceEngine, pinned_stamp
from secretary.infrastructure.voice_process import ProcessOutput, run_owned
from secretary.infrastructure.voice_store import DPAPICipher, VoiceStore, MAX_CIPHERTEXT

MODEL = ModelStamp("speechbrain/spkrec-ecapa-voxceleb", "0"*40, "1"*64)
VECTOR = (1.0,) + (0.0,)*191
OTHER = (0.0,1.0) + (0.0,)*190


def pcm(seconds=15):
    return array.array("h", [int(4000*math.sin(2*math.pi*173*i/16000)) for i in range(int(seconds*16000))]).tobytes()


@pytest.fixture
def material():
    audio = screen_pcm(pcm())
    return PrivateMaterial(str(uuid4()), str(uuid4()), 1, MODEL, audio,
                           tuple(VECTOR for _ in audio.excerpts))


class FakeCipher:
    # Reversible test cipher with plaintext absent from persisted byte sequence.
    def protect(self, value):
        return b"fake" + bytes(x^0xA5 for x in value)
    def unprotect(self, value):
        if not value.startswith(b"fake"):
            raise VoiceError("dpapi_failed")
        return bytes(x^0xA5 for x in value[4:])


def test_store_roundtrip_immutable_scoped_delete_and_no_plaintext(tmp_path, material):
    store = VoiceStore(tmp_path, cipher=FakeCipher())
    store.write(material)
    files = list(tmp_path.rglob("*"))
    saved = [p for p in files if p.is_file()]
    assert len(saved) == 1 and saved[0].suffix == ".dpapi"
    assert material.audio.pcm16 not in saved[0].read_bytes()
    assert b'"pcm16"' not in saved[0].read_bytes()
    assert store.read(material.profile_id, material.enrollment_id) == material
    with pytest.raises(VoiceError, match="material_already_exists"):
        store.write(material)
    assert store.delete(material.profile_id, material.enrollment_id)
    assert not store.delete(material.profile_id, material.enrollment_id)


def test_store_can_protect_review_draft_before_runtime_available(tmp_path,material):
    draft = replace(material,embeddings=())
    store = VoiceStore(tmp_path,cipher=FakeCipher())
    store.write(draft)
    assert store.read(draft.profile_id,draft.enrollment_id) == draft


def test_store_fails_closed_and_rejects_arbitrary_path(tmp_path, material):
    class Failing:
        def protect(self, _):
            raise OSError("secret diagnostic")
    with pytest.raises(VoiceError, match="dpapi_failed"):
        VoiceStore(tmp_path, cipher=Failing()).write(material)
    assert not list(tmp_path.rglob("*.dpapi"))
    store = VoiceStore(tmp_path, cipher=FakeCipher())
    for bad in ["../escape", str(uuid4()).upper(), "https://host/a", "C:\\private"]:
        with pytest.raises(VoiceError, match="invalid_identifier"):
            store.read(bad, material.enrollment_id)


def test_store_envelope_ownership_and_bounded_read(tmp_path, material):
    store = VoiceStore(tmp_path, cipher=FakeCipher())
    store.write(material)
    own = tmp_path / "voice/profiles" / material.profile_id / (material.enrollment_id+".dpapi")
    other = str(uuid4())
    target = own.parent / (other+".dpapi")
    target.write_bytes(own.read_bytes())
    with pytest.raises(VoiceError, match="owner_mismatch"):
        store.delete(material.profile_id, other)
    assert target.exists()
    target.write_bytes(b"x"*(MAX_CIPHERTEXT+1))
    with pytest.raises(VoiceError, match="private_material_limit"):
        store.read(material.profile_id, other)


def test_store_rejects_hardlink_and_owner_mismatch(tmp_path,material,monkeypatch):
    store = VoiceStore(tmp_path,cipher=FakeCipher())
    store.write(material)
    own = tmp_path/"voice/profiles"/material.profile_id/(material.enrollment_id+".dpapi")
    link = tmp_path/"linked"
    os.link(own,link)
    with pytest.raises(VoiceError,match="unsafe_private_path"):
        store.delete(material.profile_id,material.enrollment_id)
    link.unlink()
    def rejected(_):
        raise VoiceError("private_path_owner_mismatch")
    monkeypatch.setattr("secretary.infrastructure.voice_store._current_user_owned",rejected)
    with pytest.raises(VoiceError,match="private_path_owner_mismatch"):
        store.read(material.profile_id,material.enrollment_id)
    assert own.exists()


def test_store_finalize_failure_cleans_cipher_partial(tmp_path,material,monkeypatch):
    def denied(*_):
        raise PermissionError("private path")
    monkeypatch.setattr("secretary.infrastructure.voice_store.os.link",denied)
    with pytest.raises(VoiceError,match="private_store_write_failed"):
        VoiceStore(tmp_path,cipher=FakeCipher()).write(material)
    assert not [p for p in tmp_path.rglob("*") if p.is_file()]


def test_store_reparse_junction_rejected(tmp_path, material):
    external = tmp_path / "outside"
    external.mkdir()
    voice = tmp_path / "voice"
    if os.name == "nt":
        # Create only an isolated temporary junction; no recursive deletion.
        import ctypes
        # cmd mklink has no deletion/move and paths are test-controlled.
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(voice), str(external)], capture_output=True)
        assert result.returncode == 0
    else:
        voice.symlink_to(external, target_is_directory=True)
    try:
        with pytest.raises(VoiceError, match="unsafe_private_path"):
            VoiceStore(tmp_path, cipher=FakeCipher()).write(material)
        assert not list(external.rglob("*.dpapi"))
    finally:
        os.rmdir(voice) if os.name == "nt" else voice.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows synthetic DPAPI only")
def test_real_dpapi_synthetic_only():
    cipher = DPAPICipher()
    plain = b"synthetic-private-test"*100
    sealed = cipher.protect(plain)
    assert plain not in sealed and cipher.unprotect(sealed) == plain
    with pytest.raises(VoiceError, match="dpapi_failed"):
        cipher.unprotect(b"not-dpapi")


@pytest.mark.parametrize("value,reason", [(b"\0"*480000,"silent_audio"), (b"\0"*100,"insufficient_usable_speech"),
    (b"\0"*960002,"overlong_audio"), (b"\0"*480001,"truncated_audio"),
    (array.array("h",[32767]*240000).tobytes(), "clipped_audio")], ids=["silence","short","overlong","odd-frame","clipped"])
def test_screen_rejects_quality_and_size(value, reason):
    with pytest.raises(VoiceError, match=reason):
        screen_pcm(value)


def test_screen_excerpt_bounds_review_and_actual_active_energy():
    audio = screen_pcm(pcm(30))
    assert len(audio.excerpts) == 3 and audio.usable_samples == 480000
    assert "single_speaker_review_required" in audio.review_reasons
    assert all(e.end_sample-e.start_sample == 160000 for e in audio.excerpts)
    with pytest.raises(VoiceError, match="insufficient_usable_speech"):
        screen_pcm((pcm(2)+b"\0"*96000)*5)
    paused = screen_pcm((pcm(4)+b"\0"*32000)*5)
    assert paused.usable_samples == 20*16000


def test_decoder_payload_and_truncation_rejected_before_process():
    def forbidden(*a, **k):
        raise AssertionError("must reject before decoding")
    decoder = EnrollmentDecoder(runner=forbidden)
    for source, reason in [(b"x"*(MAX_PAYLOAD+1),"audio_payload_limit"),
                           (wav_bytes(pcm())[:-2],"truncated_audio"), ("C:/audio.wav","invalid_audio")]:
        with pytest.raises(VoiceError, match=reason):
            decoder.decode(source)


def test_real_ffmpeg_stereo_30s_and_compressed_overlong():
    stereo = io.BytesIO()
    mono = array.array("h")
    mono.frombytes(pcm(30))
    data = array.array("h", [v for sample in mono for v in (sample,sample)])
    with wave.open(stereo,"wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(data.tobytes())
    audio = EnrollmentDecoder().decode(stereo.getvalue())
    assert len(audio.pcm16) == 960000
    encoded = run_owned(["ffmpeg","-hide_banner","-loglevel","error","-nostdin","-i","pipe:0",
                         "-f","flac","pipe:1"], input_bytes=wav_bytes(pcm(31)), stdout_limit=2000000)
    with pytest.raises(VoiceError, match="overlong_audio"):
        EnrollmentDecoder().decode(encoded.stdout)


def test_real_ffmpeg_malformed_truncated_and_silent_compressed():
    decoder = EnrollmentDecoder()
    with pytest.raises(VoiceError):
        decoder.decode(b"not an audio stream")
    encoded = run_owned(["ffmpeg","-hide_banner","-loglevel","error","-nostdin","-i","pipe:0",
                         "-f","flac","pipe:1"], input_bytes=wav_bytes(pcm(20)),stdout_limit=2000000)
    with pytest.raises(VoiceError):
        decoder.decode(encoded.stdout[:-100])
    silent = run_owned(["ffmpeg","-hide_banner","-loglevel","error","-nostdin","-i","pipe:0",
                        "-f","flac","pipe:1"],input_bytes=wav_bytes(b"\0"*640000),stdout_limit=2000000)
    with pytest.raises(VoiceError,match="silent_audio"):
        decoder.decode(silent.stdout)


@pytest.mark.parametrize("script,reason,options", [
    ("import time; time.sleep(10)", "process_timeout", {"timeout":0.1}),
    ("import sys; sys.stdout.buffer.write(b'x'*100000)","process_output_limit",{}),
    ("import sys; sys.stderr.buffer.write(b'x'*100000)","process_output_limit",{}),
    ("raise RuntimeError('private diagnostic')","process_failed",{}),
    ("import time; time.sleep(10)","process_memory_limit",{"rss_limit":1})])
def test_process_limits_without_diagnostic_leak(script, reason, options):
    with pytest.raises(VoiceError, match=reason) as error:
        run_owned([sys.executable,"-c",script],stdout_limit=64,stderr_limit=1024,**options)
    assert "private diagnostic" not in str(error.value)


def test_process_cancel_and_concurrent_pipes_owned_child_cleanup(tmp_path):
    import psutil
    marker = tmp_path / "child.txt"
    script = ("import subprocess,sys,time; from pathlib import Path; "
              "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
              f"Path({str(marker)!r}).write_text(str(p.pid)); "
              "sys.stderr.buffer.write(b'x'*60000);sys.stderr.flush();time.sleep(30)")
    cancel = threading.Event()
    errors = []
    def execute():
        try:
            run_owned([sys.executable,"-c",script],stdout_limit=64,cancel=cancel)
        except VoiceError as error:
            errors.append(error.reason)
    thread = threading.Thread(target=execute)
    thread.start()
    deadline = time.monotonic()+5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.025)
    assert marker.exists()
    time.sleep(.1)  # Permit descendant identity sampling before cancellation.
    child = int(marker.read_text())
    cancel.set()
    thread.join(8)
    assert not thread.is_alive() and errors == ["cancelled"]
    assert not psutil.pid_exists(child)
    assert run_owned([sys.executable,"-c","import sys;sys.stderr.write('x'*60000);print('ok')"],
                     stdout_limit=64).stdout.strip() == b"ok"


@pytest.fixture
def fake_engine(tmp_path):
    python = tmp_path / ".venv-voice/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    def runner(command, **kwargs):
        request = json.loads(kwargs["input_bytes"])
        assert kwargs["timeout"] == 120 and kwargs["rss_limit"] == 2*1024**3
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        return ProcessOutput(json.dumps({"version":1,"request_id":request["request_id"],
            "model": {"model_id":MODEL.model_id,"revision":MODEL.revision,"weights_sha256":MODEL.weights_sha256},
            "vectors": [VECTOR for _ in request["clips"]]}).encode(),0,123)
    return LocalVoiceEngine(tmp_path, runner=runner, verifier=lambda _: MODEL)


def test_engine_batch_protocol_private_repr_and_shape(fake_engine):
    result = fake_engine.embed([pcm(2),pcm(3)])
    assert len(result.vectors) == 2 and result.model == MODEL
    assert "1.0" not in repr(result)
    for clips in [[],[b"a"],[pcm(2)]*33,[pcm(10)]*13]:
        with pytest.raises(VoiceError,match="invalid_engine_batch"):
            fake_engine.embed(clips)


@pytest.mark.parametrize("output", [b"not-json", b"{}", b'[]', b'null'])
def test_engine_malformed_output(fake_engine,output):
    fake_engine.runner = lambda *a, **k: ProcessOutput(output,0,0)
    with pytest.raises(VoiceError,match="invalid_engine_response"):
        fake_engine.embed([pcm(2)])


def test_engine_invalid_nonfinite_vectors(fake_engine):
    original = fake_engine.runner
    for vector in [[float("nan")]*192,[0.0]*192,[1.0]*191,[True]*192]:
        def runner(*args, **kwargs):
            response = json.loads(original(*args,**kwargs).stdout)
            response["vectors"] = [vector]
            return ProcessOutput(json.dumps(response).encode(),0,0)
        fake_engine.runner = runner
        with pytest.raises(VoiceError,match="invalid_engine_response"):
            fake_engine.embed([pcm(2)])


def test_engine_missing_runtime_and_hash_failure(tmp_path,fake_engine):
    with pytest.raises(VoiceError,match="runtime_missing"):
        LocalVoiceEngine(tmp_path / "missing").embed([pcm(2)])
    def bad_hash(_):
        raise VoiceError("runtime_unverified")
    fake_engine.verifier = bad_hash
    with pytest.raises(VoiceError,match="runtime_unverified"):
        fake_engine.embed([pcm(2)])


def test_tensor_loader_hashes_real_pinned_metadata_with_substitution(tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("test_tensor_loader",root/"config/voice-runtime/tensor_loader.py")
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    manifest = json.loads((root/"config/voice-runtime/model-manifest.json").read_bytes())
    config = tmp_path/"config/voice-runtime"
    config.mkdir(parents=True)
    (config/"model-manifest.json").write_text(json.dumps(manifest))
    model = tmp_path/manifest["local_directory"]
    model.mkdir(parents=True)
    artifact = manifest["files"][0]
    (model/artifact["name"]).write_bytes(b"x"*artifact["size_bytes"])
    with pytest.raises(ValueError,match="model_hash_failure"):
        loader.verify_pinned(tmp_path)


def candidate(vector=VECTOR):
    return CandidateMaterial(str(uuid4()),1,MODEL,(vector,))


def test_match_uncalibrated_and_single_candidate_never_confirmed():
    first, second = candidate(), candidate(OTHER)
    result = match_voice([VECTOR],[first,second],model=MODEL,duration_samples=32000)
    assert result.status == "unknown" and result.proposed_profile_id is None
    assert result.candidates[0].profile_id == first.profile_id and "uncalibrated" in result.reasons
    calibration = CalibrationSnapshot("cal-1","heldout-1",MODEL,.8,.1)
    result = match_voice([VECTOR],[first],model=MODEL,calibration=calibration,duration_samples=32000)
    assert result.status == "unknown" and result.second_best_margin is None
    result = match_voice([VECTOR],[first,second],model=MODEL,calibration=calibration,duration_samples=32000)
    assert result.status == "proposed" and result.proposed_profile_id == first.profile_id
    assert "human_review_required" in result.reasons and result.raw_score == 1.0


@pytest.mark.parametrize("options,reason,status", [({"duration_samples":100},"short_clip","unknown"),
    ({"overlap":True},"overlap","conflict"),({"quality_ok":False},"bad_quality","unknown")])
def test_match_eligibility(options,reason,status):
    settings = {"duration_samples":32000,**options}
    result = match_voice([VECTOR],[candidate()],model=MODEL,**settings)
    assert result.status == status and result.reasons == (reason,)


def test_match_invalid_vectors_ties_disagreement_and_model_mismatch():
    cal = CalibrationSnapshot("cal-1","heldout-1",MODEL,.2,.1)
    first, second = candidate(), candidate(OTHER)
    for vector in [[float("nan")]*192,[0]*192,[1]*193]:
        assert match_voice([vector],[first],model=MODEL,duration_samples=32000).reasons == ("invalid_vector",)
    tie = match_voice([VECTOR],[first,candidate()],model=MODEL,calibration=cal,duration_samples=32000)
    assert tie.status == "conflict"
    # Mean score can pass yet excerpts disagree, requiring human handling.
    query = [VECTOR,VECTOR,OTHER]
    assert match_voice(query,[first,second],model=MODEL,calibration=cal,duration_samples=96000).reasons == ("excerpt_disagreement",)
    other_model = ModelStamp(MODEL.model_id,"2"*40,MODEL.weights_sha256)
    assert match_voice([VECTOR],[first,second],model=MODEL,
        calibration=CalibrationSnapshot("c","e",other_model,.8,.1),duration_samples=32000).status == "unknown"


@pytest.mark.parametrize("threshold,margin",[(float("nan"),.1),(.8,float("inf")),(1.1,.1),(.8,-1)])
def test_invalid_calibration_rejected(threshold,margin):
    with pytest.raises(VoiceError,match="invalid_calibration"):
        CalibrationSnapshot("c","e",MODEL,threshold,margin)


def test_cosine_huge_finite_vectors_and_private_repr(material):
    assert cosine((1e300,)+(0.0,)*191,VECTOR) == 1
    assert "pcm16" not in repr(material) and "embeddings" not in repr(material)


def test_excerpt_tie_is_order_independent_conflict():
    first, second = candidate(),candidate(OTHER)
    tied = (1.0,1.0)+(0.0,)*190
    calibration = CalibrationSnapshot("test-only","synthetic-repro",MODEL,.8,.1)
    results = [match_voice([VECTOR,tied],order,model=MODEL,calibration=calibration,duration_samples=32000)
               for order in ([first,second],[second,first])]
    assert results[0] == results[1]
    assert results[0].status == "conflict" and results[0].proposed_profile_id is None
    assert results[0].reasons == ("ambiguous_excerpt",)


def test_overflow_integer_vector_is_unknown_and_invalid_engine_response(fake_engine):
    huge = [10**400]+[0]*191
    result = match_voice([huge],[candidate()],model=MODEL,duration_samples=32000)
    assert result.status == "unknown" and result.reasons == ("invalid_vector",)
    malformed_candidate = candidate(tuple(huge))
    assert match_voice([VECTOR],[malformed_candidate],model=MODEL,duration_samples=32000).reasons == ("invalid_candidates",)
    original = fake_engine.runner
    def runner(*args,**kwargs):
        response = json.loads(original(*args,**kwargs).stdout)
        response["vectors"] = [huge]
        return ProcessOutput(json.dumps(response).encode(),0,0)
    fake_engine.runner = runner
    with pytest.raises(VoiceError,match="invalid_engine_response"):
        fake_engine.embed([pcm(2)])
    assert cosine((1e300,)+(0.0,)*191,VECTOR) == 1
    assert cosine((10**300,)+(0.0,)*191,VECTOR) == 1
    with pytest.raises(VoiceError,match="invalid_calibration"):
        CalibrationSnapshot("test-only","synthetic-repro",MODEL,10**400,.1)
    with pytest.raises(VoiceError,match="invalid_calibration"):
        CalibrationSnapshot("test-only","synthetic-repro",MODEL,.8,10**400)


@pytest.mark.parametrize("finalize_succeeds",[True,False],ids=["finalized","unfinalized"])
def test_locked_owned_partial_is_sanitized_and_retryable(tmp_path,material,monkeypatch,finalize_succeeds):
    store = VoiceStore(tmp_path,cipher=FakeCipher())
    real_unlink,real_link = Path.unlink,os.link
    locked = True
    def unlink(path,*args,**kwargs):
        if path.suffix == ".cipherpart" and locked:
            raise PermissionError("sensitive absolute private path")
        return real_unlink(path,*args,**kwargs)
    def link(*args,**kwargs):
        if not finalize_succeeds:
            raise PermissionError("sensitive finalize path")
        return real_link(*args,**kwargs)
    monkeypatch.setattr(Path,"unlink",unlink)
    monkeypatch.setattr("secretary.infrastructure.voice_store.os.link",link)
    with pytest.raises(VoiceError,match="private_cleanup_pending") as error:
        store.write(material)
    assert str(error.value) == "private_cleanup_pending"
    parts = list(tmp_path.rglob("*.cipherpart"))
    assert len(parts) == 1
    with pytest.raises(VoiceError,match="private_cleanup_pending"):
        store.read(material.profile_id,material.enrollment_id)
    locked = False
    if finalize_succeeds:
        assert store.read(material.profile_id,material.enrollment_id) == material
        assert store.delete(material.profile_id,material.enrollment_id)
    else:
        monkeypatch.setattr("secretary.infrastructure.voice_store.os.link",real_link)
        store.write(material)
        assert store.read(material.profile_id,material.enrollment_id) == material
    assert not list(tmp_path.rglob("*.cipherpart"))


def test_pending_cleanup_does_not_accept_an_extra_external_hardlink(tmp_path,material,monkeypatch):
    store = VoiceStore(tmp_path,cipher=FakeCipher())
    real_unlink = Path.unlink
    def locked(path,*args,**kwargs):
        if path.suffix == ".cipherpart":
            raise PermissionError
        return real_unlink(path,*args,**kwargs)
    monkeypatch.setattr(Path,"unlink",locked)
    with pytest.raises(VoiceError,match="private_cleanup_pending"):
        store.write(material)
    partial = next(tmp_path.rglob("*.cipherpart"))
    external = tmp_path/"external"
    os.link(partial,external)
    monkeypatch.setattr(Path,"unlink",real_unlink)
    with pytest.raises(VoiceError,match="unsafe_private_path"):
        store.read(material.profile_id,material.enrollment_id)
    assert partial.exists() and external.exists()
    external.unlink()
    assert store.read(material.profile_id,material.enrollment_id) == material
