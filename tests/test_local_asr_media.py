import hashlib
import os

import pytest

from secretary.application.local_asr_media import LocalAsrMediaError, verified_meeting_input
from secretary.application.local_asr_media import copy_verified_meeting_input
from secretary.application.local_asr_media import lock_verified_input_copy
from secretary.infrastructure.database import Database


@pytest.fixture
def source(tmp_path):
    db = Database(tmp_path / "data" / "test.sqlite3")
    meeting = db.create_meeting("Synthetic")
    root = tmp_path / "data"
    path = root / "uploads" / meeting["id"] / "original.media"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic source bytes")
    db.update_meeting(meeting["id"], media_path=str(path))
    return db, root, meeting["id"], path


def test_streaming_owned_input_and_no_db_changes(source):
    db, root, meeting, path = source
    before = db.rows("SELECT * FROM meetings")
    assert verified_meeting_input(db, root, meeting, max_bytes=100) == hashlib.sha256(path.read_bytes()).hexdigest()
    assert db.rows("SELECT * FROM meetings") == before
    assert db.rows("SELECT * FROM jobs") == []


def test_execution_copy_uses_verified_source_and_refuses_replacement(source):
    db, root, meeting, path = source
    target = root / "local-asr-work" / "attempt" / "input.media"
    target.parent.mkdir(parents=True)
    original = path.read_bytes()
    expected = hashlib.sha256(original).hexdigest()
    assert copy_verified_meeting_input(db, root, meeting, target, max_bytes=100,
                                       expected_sha256=expected) == target
    assert target.read_bytes() == original == path.read_bytes()
    with pytest.raises(LocalAsrMediaError, match="destination_exists"):
        copy_verified_meeting_input(db, root, meeting, target, max_bytes=100,
                                    expected_sha256=expected)


def test_execution_copy_rejects_wrong_plan_and_outside_destination(source):
    db, root, meeting, path = source
    target = root / "local-asr-work" / "attempt" / "input.media"
    target.parent.mkdir(parents=True)
    with pytest.raises(LocalAsrMediaError, match="hash_mismatch"):
        copy_verified_meeting_input(db, root, meeting, target, max_bytes=100,
                                    expected_sha256="0" * 64)
    with pytest.raises(LocalAsrMediaError, match="destination_invalid"):
        copy_verified_meeting_input(db, root, meeting, root / "outside.media", max_bytes=100,
                                    expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert not (root / "outside.media").exists()


def test_native_input_copy_lock_allows_read_but_denies_write_delete_and_swap(source):
    if os.name != 'nt':
        pytest.skip('Windows native input sharing contract')
    db, root, meeting, path = source
    target = root / 'local-asr-work' / 'attempt' / 'input.media'
    target.parent.mkdir(parents=True)
    original = path.read_bytes()
    expected = hashlib.sha256(original).hexdigest()
    copy_verified_meeting_input(db, root, meeting, target, max_bytes=100, expected_sha256=expected)
    with lock_verified_input_copy(target, expected_sha256=expected, max_bytes=100):
        assert target.read_bytes() == original
        with pytest.raises(OSError):
            target.write_bytes(b'corrupt')
        with pytest.raises(OSError):
            target.unlink()
        substitute = target.with_suffix('.replacement')
        substitute.write_bytes(b'corrupt')
        with pytest.raises(OSError):
            substitute.replace(target)
    target.write_bytes(original)
    assert path.read_bytes() == original


def test_invalid_missing_active_and_bounded_input(source):
    db, root, meeting, path = source
    for value in ("../outside", "invalid", meeting.upper()):
        with pytest.raises(LocalAsrMediaError, match="invalid_meeting"):
            verified_meeting_input(db, root, value, max_bytes=100)
    with pytest.raises(LocalAsrMediaError, match="size_invalid"):
        verified_meeting_input(db, root, meeting, max_bytes=1)
    db.update_meeting(meeting, recording=1)
    with pytest.raises(LocalAsrMediaError, match="recording_in_progress"):
        verified_meeting_input(db, root, meeting, max_bytes=100)
    db.update_meeting(meeting, recording=0)
    path.unlink()
    with pytest.raises(LocalAsrMediaError, match="unavailable"):
        verified_meeting_input(db, root, meeting, max_bytes=100)


def test_path_in_database_cannot_escape_allowed_meeting(source):
    db, root, meeting, path = source
    other = root / "unrelated-private-file"
    other.write_bytes(b"not an audio input")
    db.update_meeting(meeting, media_path=str(other))
    with pytest.raises(LocalAsrMediaError, match="outside_meeting"):
        verified_meeting_input(db, root, meeting, max_bytes=100)
    db.update_meeting(meeting, media_path=str(path.parent / ".." / ".." / other.name))
    with pytest.raises(LocalAsrMediaError):
        verified_meeting_input(db, root, meeting, max_bytes=100)


def test_single_chunk_hash_verified_and_multichunk_blocked(source):
    db, root, meeting, _ = source
    db.update_meeting(meeting, media_path=None)
    with pytest.raises(LocalAsrMediaError, match="canonical_input_required"):
        verified_meeting_input(db, root, meeting, max_bytes=100)
    chunk = root / "audio" / meeting / "first.wav"
    chunk.parent.mkdir(parents=True)
    chunk.write_bytes(b"synthetic wav bytes")
    digest = hashlib.sha256(chunk.read_bytes()).hexdigest()
    db.add_chunk(meeting, dict(sequence=0, channel="import", path=str(chunk), offset_ms=0, duration_ms=10, sha256=digest))
    assert verified_meeting_input(db, root, meeting, max_bytes=100) == digest
    chunk.write_bytes(b"different bytes")
    with pytest.raises(LocalAsrMediaError, match="hash_mismatch"):
        verified_meeting_input(db, root, meeting, max_bytes=100)
    db.add_chunk(meeting, dict(sequence=1, channel="import", path=str(chunk), offset_ms=10, duration_ms=10, sha256=digest))
    with pytest.raises(LocalAsrMediaError, match="canonical_input_required"):
        verified_meeting_input(db, root, meeting, max_bytes=100)


def test_extra_hardlink_is_not_accepted_as_owned_input(source):
    db, root, meeting, path = source
    os.link(path, path.with_suffix('.linked'))
    with pytest.raises(LocalAsrMediaError, match="unavailable"):
        verified_meeting_input(db, root, meeting, max_bytes=100)
