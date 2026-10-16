"""Fixture containment must parse SQLite URI paths without allowing remote IO."""
import importlib
import importlib.util
from pathlib import Path
from urllib.parse import quote

import pytest


def parser():
    assert importlib.util.find_spec('sqlite_test_paths'), 'SQLite fixture URI parser is missing'
    return importlib.import_module('sqlite_test_paths').sqlite_path


@pytest.mark.parametrize('form', ['plain', 'as_uri', 'sqlite_drive', 'localhost'])
def test_local_sqlite_uri_resolves_to_the_same_scoped_path(tmp_path, form):
    path = tmp_path / 'folder with spaces' / 'name#percent%.sqlite3'
    encoded = quote(path.as_posix(), safe='/:')
    value = {'plain': path, 'as_uri': path.as_uri() + '?mode=ro&immutable=1',
             'sqlite_drive': 'file:' + encoded + '?mode=ro',
             'localhost': 'file://localhost/' + encoded + '?mode=ro'}[form]
    assert parser()(value, uri=form != 'plain') == path.resolve()


@pytest.mark.parametrize('value', [
    'file://foreign.invalid/share/data.sqlite3',
    'file:////foreign.invalid/share/data.sqlite3',
    pytest.param('file://user:password@foreign.invalid/share/data.sqlite3', id='nonlocal_userinfo'),
    'file:///C:/safe/%00hidden.sqlite3',
    'file:///C:/safe/data.sqlite3#fragment',
])
def test_nonlocal_or_ambiguous_sqlite_uri_is_rejected_before_resolution(value):
    with pytest.raises(ValueError): parser()(value, uri=True)


def test_encoded_parent_escape_still_fails_fixture_containment(tmp_path):
    value = (tmp_path / '%2e%2e' / 'outside.sqlite3').as_uri().replace('%252e', '%2e') + '?mode=ro'
    assert not parser()(value, uri=True).is_relative_to(tmp_path.resolve())


def test_uri_is_not_accepted_as_an_ordinary_filename(tmp_path):
    with pytest.raises(ValueError): parser()((tmp_path / 'source.sqlite3').as_uri(), uri=False)
