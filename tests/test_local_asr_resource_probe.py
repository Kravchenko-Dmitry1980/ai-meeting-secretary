import math
import os

from secretary.infrastructure import local_asr_resource_probe as probe
from secretary.infrastructure.local_asr_resources import ResourceProfile, check_admission


def test_missing_native_measurement_fails_closed(monkeypatch):
    def missing(*args, **kwargs):
        raise OSError('synthetic absent system DLL')
    monkeypatch.setattr(probe.ctypes, 'WinDLL', missing, raising=False)
    snapshot = probe.sample_resources('GPU-synthetic')
    assert snapshot.free_ram_mib is None
    assert snapshot.free_commit_mib is None
    assert snapshot.gpu_free_mib == {'GPU-synthetic': None}
    profile = ResourceProfile(asr_device='cpu', diarization_device='cpu')
    assert not check_admission(profile, snapshot).allowed


def test_probe_never_queries_gpu_without_physical_identity(monkeypatch):
    monkeypatch.setattr(probe, '_gpu_memory', lambda _: (_ for _ in ()).throw(AssertionError('Unexpected GPU query')))
    snapshot = probe.sample_resources()
    assert snapshot.gpu_free_mib == {}
    if os.name == 'nt':
        assert snapshot.free_ram_mib is not None and math.isfinite(snapshot.free_ram_mib)
        assert snapshot.free_commit_mib is not None and math.isfinite(snapshot.free_commit_mib)


def test_ordinal_and_mig_are_not_physical_gpu_measurements():
    assert probe._gpu_memory('0') is None
    assert probe._gpu_memory('MIG-GPU-synthetic/1/0') is None
