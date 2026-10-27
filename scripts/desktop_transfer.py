"""Create, verify and extract explicitly selected transfer archives.

No discovery of private data, installation, Git writes, deletion, or execution.
Extraction is create-only and requires a separately supplied archive SHA-256.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

MANIFEST = 'TRANSFER-MANIFEST.json'
MAX_FILES = 100_000
MAX_FILE_BYTES = 8 * 1024**3
MAX_TOTAL_BYTES = 128 * 1024**3
MAX_MANIFEST_BYTES = 32 * 1024**2
BLOCK = 1024**2


def require(value, message):
    if not value:
        raise ValueError(message)


def relative_name(value):
    require(isinstance(value, str) and value and '\\' not in value and ':' not in value,
            'Invalid archive path')
    path = PurePosixPath(value)
    require(not path.is_absolute() and str(path) == value, 'Invalid archive path')
    for part in value.split('/'):
        require(part not in ('', '.', '..') and not part.endswith((' ', '.'))
                and not any(ord(c) < 32 for c in part), 'Invalid archive path')
        require(not re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part),
                'Reserved archive path')
    return value


def plain(path, *, directory=False):
    path = Path(path).absolute()
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
                'Linked path refused')
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
            'Regular local file/directory required')
    return info


def sha256(path):
    before = plain(path)
    with Path(path).open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    after = plain(path)
    require(signature(before) == signature(after), 'Source changed during hashing')
    return digest


def signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink


def create_archive(destination, entries, *, kind, metadata=None):
    """Entries are explicit (source_path, archive_relative_path) pairs."""
    destination = Path(destination).absolute()
    plain(destination.parent, directory=True)
    require(not destination.exists(), 'Archive already exists')
    require(0 < len(entries) <= MAX_FILES, 'Invalid file count')
    checked, seen, total = [], set(), 0
    for source, name in entries:
        name = relative_name(name)
        require(name.casefold() not in seen and name.casefold() != MANIFEST.casefold(), 'Duplicate archive path')
        seen.add(name.casefold())
        source = Path(source).absolute()
        info = plain(source)
        require(info.st_size <= MAX_FILE_BYTES, 'File size limit')
        total += info.st_size
        require(total <= MAX_TOTAL_BYTES, 'Archive size limit')
        checked.append((source, name, info))
    rows = []
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for source, name, before in checked:
            hasher, count = hashlib.sha256(), 0
            with source.open('rb') as incoming, archive.open(name, 'w', force_zip64=True) as output:
                opened = os.fstat(incoming.fileno())
                require(signature(before) == signature(opened), 'Source changed before archive')
                while block := incoming.read(BLOCK):
                    count += len(block)
                    require(count <= before.st_size, 'Source size changed')
                    output.write(block)
                    hasher.update(block)
                finished = os.fstat(incoming.fileno())
            require(count == before.st_size and signature(before) == signature(finished) == signature(plain(source)),
                    'Source changed during archive')
            rows.append(dict(path=name, bytes=count, sha256=hasher.hexdigest()))
        manifest = dict(schema_version='secretary.desktop-transfer.v1', kind=kind,
                        metadata=metadata or {}, files=rows, total_bytes=total)
        encoded = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
        require(len(encoded) <= MAX_MANIFEST_BYTES, 'Manifest size limit')
        archive.writestr(MANIFEST, encoded)
    return dict(file=destination.name, sha256=sha256(destination), bytes=destination.stat().st_size,
                files=len(rows), payload_bytes=total, kind=kind)


def _manifest(archive):
    entries = archive.infolist()
    require(1 < len(entries) <= MAX_FILES + 1, 'Invalid archive entry count')
    infos = {}
    folded = set()
    for info in entries:
        name = relative_name(info.filename)
        require(name.casefold() not in folded, 'Duplicate archive path')
        folded.add(name.casefold())
        require(not info.is_dir() and not info.flag_bits & 1, 'Unexpected archive entry')
        mode = info.external_attr >> 16
        require(stat.S_IFMT(mode) in (0, stat.S_IFREG), 'Non-regular archive entry')
        require(0 <= info.file_size <= MAX_FILE_BYTES, 'Archive file size limit')
        infos[name] = info
    require(MANIFEST in infos and infos[MANIFEST].file_size <= MAX_MANIFEST_BYTES, 'Missing/oversize manifest')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate manifest key')
            result[key] = value
        return result
    manifest = json.loads(archive.read(MANIFEST), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite manifest')))
    require(manifest.get('schema_version') == 'secretary.desktop-transfer.v1', 'Unsupported manifest')
    rows = manifest.get('files')
    require(isinstance(rows, list) and 0 < len(rows) <= MAX_FILES, 'Invalid manifest files')
    names, folded, total = set(), set(), 0
    for row in rows:
        name = relative_name(row['path'])
        require(name.casefold() not in folded and name != MANIFEST, 'Duplicate manifest path')
        names.add(name)
        folded.add(name.casefold())
        size = row['bytes']
        require(type(size) is int and 0 <= size <= MAX_FILE_BYTES, 'Invalid manifest size')
        require(re.fullmatch('[0-9a-f]{64}', row.get('sha256', '')), 'Invalid file digest')
        require(name in infos and infos[name].file_size == size, 'Manifest file mismatch')
        total += size
    require(names | {MANIFEST} == set(infos) and total == manifest.get('total_bytes')
            and total <= MAX_TOTAL_BYTES, 'Manifest closure mismatch')
    # Prevent a file from also being another member's parent, regardless of case.
    for name in names:
        require(not any(str(p).casefold() in folded for p in PurePosixPath(name).parents if str(p) != '.'),
                'Archive parent/file conflict')
    return manifest


def verify_archive(path, expected_sha256):
    require(re.fullmatch('[0-9a-f]{64}', expected_sha256 or ''), 'Archive SHA-256 required')
    require(sha256(path) == expected_sha256, 'Archive SHA-256 mismatch')
    with zipfile.ZipFile(path) as archive:
        manifest = _manifest(archive)
        for row in manifest['files']:
            hasher, count = hashlib.sha256(), 0
            with archive.open(row['path']) as source:
                while block := source.read(BLOCK):
                    count += len(block)
                    require(count <= row['bytes'], 'Uncompressed size limit')
                    hasher.update(block)
            require(count == row['bytes'] and hasher.hexdigest() == row['sha256'], 'File integrity mismatch')
    return manifest


def extract_archive(path, expected_sha256, destination, *, allow_existing_directory=False):
    manifest = verify_archive(path, expected_sha256)
    destination = Path(destination).absolute()
    plain(destination.parent, directory=True)
    if destination.exists():
        require(allow_existing_directory, 'Extraction target must not exist')
        plain(destination, directory=True)
    # Preflight the entire set before writing any member; no existing file can
    # be replaced even in the explicit add-files-to-existing-directory mode.
    for row in manifest['files']:
        target = destination.joinpath(*PurePosixPath(row['path']).parts)
        require(not target.exists(), 'Extraction file already exists')
        for parent in target.parents:
            if parent.exists():
                plain(parent, directory=True)
            if parent == destination:
                break
    destination.mkdir(exist_ok=allow_existing_directory)
    # Recheck before opening; every extracted file is also verified while writing.
    require(sha256(path) == expected_sha256, 'Archive changed before extraction')
    with zipfile.ZipFile(path) as archive:
        for row in manifest['files']:
            target = destination.joinpath(*PurePosixPath(row['path']).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            plain(target.parent, directory=True)
            hasher, count = hashlib.sha256(), 0
            with archive.open(row['path']) as source, target.open('xb') as output:
                while block := source.read(BLOCK):
                    count += len(block)
                    require(count <= row['bytes'], 'Uncompressed size limit')
                    hasher.update(block)
                    output.write(block)
            require(count == row['bytes'] and hasher.hexdigest() == row['sha256'], 'Extraction integrity mismatch')
    return dict(status='verified_extracted', files=len(manifest['files']), destination=str(destination))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['verify', 'extract'])
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--allow-existing-directory', action='store_true',
                        help='Add missing files only; all existing files remain protected')
    args = parser.parse_args()
    if args.command == 'extract':
        require(args.destination is not None, 'Destination required')
        result = extract_archive(args.archive, args.sha256, args.destination,
                                 allow_existing_directory=args.allow_existing_directory)
    else:
        manifest = verify_archive(args.archive, args.sha256)
        result = dict(status='verified', kind=manifest['kind'], files=len(manifest['files']), bytes=manifest['total_bytes'])
    print(json.dumps(result))


if __name__ == '__main__':
    main()
