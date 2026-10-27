import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

spec = importlib.util.spec_from_file_location('desktop_transfer', Path(__file__).parents[1] / 'scripts/desktop_transfer.py')
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)


def test_verified_roundtrip_keeps_bytes_and_refuses_existing_destination(tmp_path):
    source = tmp_path / 'input.bin'
    source.write_bytes(bytes(range(256)) * 100)
    bundle = tmp_path / 'transfer.zip'
    receipt = transfer.create_archive(bundle, [(source, 'папка с пробелом/a.bin')], kind='synthetic')
    manifest = transfer.verify_archive(bundle, receipt['sha256'])
    assert manifest['total_bytes'] == source.stat().st_size
    output = tmp_path / 'new copy'
    transfer.extract_archive(bundle, receipt['sha256'], output)
    assert (output / 'папка с пробелом/a.bin').read_bytes() == source.read_bytes()
    with pytest.raises(ValueError, match='must not exist'):
        transfer.extract_archive(bundle, receipt['sha256'], output)
    with pytest.raises(ValueError, match='already exists'):
        transfer.create_archive(bundle, [(source, 'a')], kind='synthetic')


@pytest.mark.parametrize('path', ['../a', '/a', 'a/../b', 'C:/x', 'a\\b', 'a//b', 'a/./b', 'NUL', 'a/CON.txt', 'a.', 'a '])
def test_unsafe_windows_paths_are_rejected(path):
    with pytest.raises(ValueError):
        transfer.relative_name(path)


def test_hash_failure_does_not_create_target(tmp_path):
    source = tmp_path / 'a'
    source.write_text('source')
    archive = tmp_path / 'a.zip'
    transfer.create_archive(archive, [(source, 'a')], kind='synthetic')
    target = tmp_path / 'target'
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        transfer.extract_archive(archive, '0' * 64, target)
    assert not target.exists()


@pytest.mark.parametrize('second', ['A', 'a/child'])
def test_duplicates_and_parent_file_conflicts_fail_before_extraction(tmp_path, second):
    archive = tmp_path / 'malformed.zip'
    rows = [{'path': name, 'bytes': 1, 'sha256': '0' * 64} for name in ('a', second)]
    with zipfile.ZipFile(archive, 'x') as out:
        for row in rows:
            out.writestr(row['path'], b'x')
        out.writestr(transfer.MANIFEST, json.dumps({'schema_version': 'secretary.desktop-transfer.v1',
            'kind': 'synthetic', 'files': rows, 'total_bytes': 2}))
    target = tmp_path / 'target'
    with pytest.raises(ValueError):
        transfer.extract_archive(archive, transfer.sha256(archive), target)
    assert not target.exists()


def test_extra_zip_member_is_rejected_even_with_correct_outer_hash(tmp_path):
    source = tmp_path / 'source'
    source.write_bytes(b'x')
    archive = tmp_path / 'bundle.zip'
    transfer.create_archive(archive, [(source, 'a')], kind='synthetic')
    with zipfile.ZipFile(archive, 'a') as out:
        out.writestr('unlisted', b'bad')
    with pytest.raises(ValueError, match='closure mismatch'):
        transfer.verify_archive(archive, transfer.sha256(archive))


def test_inner_digest_is_verified(tmp_path):
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'x') as out:
        out.writestr('a', b'x')
        out.writestr(transfer.MANIFEST, json.dumps({'schema_version': 'secretary.desktop-transfer.v1',
            'kind': 'synthetic', 'files': [{'path': 'a', 'bytes': 1, 'sha256': '0' * 64}], 'total_bytes': 1}))
    with pytest.raises(ValueError, match='integrity mismatch'):
        transfer.verify_archive(archive, transfer.sha256(archive))


def test_adding_assets_to_checkout_is_create_only_and_preflighted(tmp_path):
    source = tmp_path / 'input'
    source.write_bytes(b'payload')
    archive = tmp_path / 'assets.zip'
    receipt = transfer.create_archive(archive, [(source, 'new.bin'), (source, 'existing.bin')], kind='synthetic')
    root = tmp_path / 'checkout'
    root.mkdir()
    (root / 'existing.bin').write_bytes(b'keep')
    with pytest.raises(ValueError, match='file already exists'):
        transfer.extract_archive(archive, receipt['sha256'], root, allow_existing_directory=True)
    assert not (root / 'new.bin').exists()
    assert (root / 'existing.bin').read_bytes() == b'keep'
    other = tmp_path / 'other-checkout'
    other.mkdir()
    transfer.extract_archive(archive, receipt['sha256'], other, allow_existing_directory=True)
    assert (other / 'new.bin').read_bytes() == b'payload'
