"""Local filename decoding for SQLite guards; callers still enforce their root."""
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


def sqlite_path(database, *, uri=False):
    value = os.fsdecode(database)
    if value.startswith('file:'):
        if not uri: raise ValueError('fixture_sqlite_uri_required')
        parsed = urlsplit(value)
        if parsed.netloc not in ('', 'localhost') or parsed.fragment:
            raise ValueError('fixture_sqlite_nonlocal_uri')
        value = unquote(parsed.path)
        if '\x00' in value or value.startswith(('//', '\\\\')):
            raise ValueError('fixture_sqlite_ambiguous_uri')
        if os.name == 'nt' and re.match(r'^/[A-Za-z]:[/\\]', value):
            value = value[1:]
    path = Path(value)
    if path.drive.startswith('\\\\'):
        raise ValueError('fixture_sqlite_unc_forbidden')
    return path.resolve()
