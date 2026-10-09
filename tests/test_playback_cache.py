"""Reader-safe and bounded playback cache contract tests."""
from pathlib import Path

import pytest

import secretary.infrastructure.storage_space as storage_space
from secretary.infrastructure.playback_cache import PlaybackCache
from secretary.infrastructure.storage_space import StorageSpaceError


def test_cache_evicts_least_recently_used_instead_of_oldest_created(tmp_path):
    cache = PlaybackCache(tmp_path / "data", max_bytes=250)
    first = cache.root / "first" / "playback-system-a.wav"
    second = cache.root / "second" / "playback-system-b.wav"
    third = cache.root / "third" / "playback-system-c.wav"

    first_lease = cache.get_or_create(first, 100, lambda: first.write_bytes(b"a" * 100))
    first_lease.release()
    second_lease = cache.get_or_create(second, 100, lambda: second.write_bytes(b"b" * 100))
    second_lease.release()

    reused_lease = cache.get_or_create(first, 100, lambda: pytest.fail("existing cache should be reused"))
    reused_lease.release()
    third_lease = cache.get_or_create(third, 100, lambda: third.write_bytes(b"c" * 100))
    third_lease.release()

    assert first.is_file() and third.is_file()
    assert not second.exists(), "the cache not used most recently should be evicted"


def test_cache_rebuilds_an_inactive_truncated_snapshot(tmp_path):
    cache = PlaybackCache(tmp_path / "data", max_bytes=1024)
    path = cache.root / "meeting" / "playback-system-snapshot.wav"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"truncated")

    lease = cache.get_or_create(path, 64, lambda: path.write_bytes(b"r" * 64))
    lease.release()

    assert path.read_bytes() == b"r" * 64


def test_disk_usage_probe_failure_does_not_evict_valid_cache(tmp_path, monkeypatch):
    cache = PlaybackCache(tmp_path / "data", max_bytes=1024)
    existing = cache.root / "existing" / "playback-system-old.wav"
    existing_lease = cache.get_or_create(existing, 64, lambda: existing.write_bytes(b"o" * 64))
    existing_lease.release()
    target = cache.root / "new" / "playback-system-new.wav"

    def unavailable(_path):
        raise OSError("synthetic disk-usage inspection failure")

    monkeypatch.setattr(storage_space.shutil, "disk_usage", unavailable)
    with pytest.raises(StorageSpaceError):
        cache.get_or_create(target, 64, lambda: target.write_bytes(b"n" * 64))

    assert existing.is_file()
    assert not target.exists()
