"""URI normalization must preserve the offline runner's owner-data boundary."""
from pathlib import Path
from test_team_preflight import invoke_runner


def test_windows_sqlite_uri_accepts_only_synthetic_local_paths(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = invoke_runner(tmp_path, f'''
from pathlib import Path
import sqlite3
import pytest

def test_synthetic_uri_and_encoded_owner_paths(tmp_path):
    path = tmp_path / "database with spaces.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE synthetic(value)")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM synthetic").fetchone() == (0,)
    for uri in ({(root / 'data' / 'secretary.sqlite3').as_uri()!r},
                {(root / '.env').as_uri()!r},
                {(root / 'data' / 'secretary.sqlite3').as_uri().replace('/data/', '/%64ata/')!r},
                "file://remote.invalid/share/synthetic.sqlite3"):
        with pytest.raises(PermissionError, match="offline guard"):
            sqlite3.connect(uri + "?mode=ro", uri=True)
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_win32_extended_local_path_preserves_synthetic_write_and_owner_denial(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = invoke_runner(tmp_path, f'''
import os
from pathlib import Path
from urllib.parse import quote
import sqlite3
import pytest
import run_offline_tests as guard

@pytest.mark.skipif(os.name != "nt", reason="Windows internal Win32 spelling")
def test_extended_path_aliases_keep_same_boundary(tmp_path):
    path = tmp_path / "extended synthetic.sqlite3"
    native = chr(92)*2 + "?" + chr(92) + str(path)
    assert guard._path(native) == path.resolve()
    uri = "file:" + quote(native, safe="/:" + chr(92)) + "?mode=rwc"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.execute("CREATE TABLE synthetic(value INTEGER)")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT count(*) FROM synthetic").fetchone() == (0,)
    for path in (Path({str(root / 'data' / 'never-opened-synthetic.sqlite3')!r}),
                 Path({str(root / '.env')!r}), Path({str(root / '.env.team')!r})):
        native = chr(92)*2 + "?" + chr(92) + str(path)
        for writing in (False, True):
            with pytest.raises(PermissionError, match="offline guard"):
                guard.check_path(native, writing=writing)
        uri = "file:" + quote(native, safe="/:" + chr(92)) + "?mode=ro"
        with pytest.raises(PermissionError, match="offline guard"):
            sqlite3.connect(uri, uri=True)
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_nonlocal_extended_prefix_is_rejected_before_resolution(tmp_path):
    result = invoke_runner(tmp_path, '''
import os
from pathlib import Path
import pytest
import run_offline_tests as guard

@pytest.mark.skipif(os.name != "nt", reason="Windows internal Win32 spelling")
def test_nonlocal_prefix_does_not_touch_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Nonlocal prefix reached filesystem resolution")
    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", forbidden)
        for tail in ("UNC" + chr(92) + "synthetic.invalid" + chr(92) + "share",
                     "GLOBALROOT" + chr(92) + "Device" + chr(92) + "synthetic",
                     "C:relative"):
            raw = chr(92)*2 + "?" + chr(92) + tail
            with pytest.raises(PermissionError, match="offline guard"):
                guard._path(raw)
''')
    assert result.returncode == 0, result.stdout + result.stderr
