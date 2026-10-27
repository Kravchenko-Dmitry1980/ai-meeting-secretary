"""Create deterministic full-file environment evidence without importing ML code."""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess

def digest(path: Path) -> str:
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def entry(path: Path) -> dict:
    return {'path': str(path.resolve()), 'size_bytes': path.stat().st_size,
            'sha256': digest(path)}

def files(root: Path) -> list[dict]:
    paths = []
    for path in sorted(root.rglob('*'), key=lambda p: p.relative_to(root).as_posix()):
        if path.is_symlink():
            raise RuntimeError(f'Unexpected symlink: {path}')
        if path.is_file():
            paths.append(path)
    def record(path: Path) -> dict:
        before = path.stat()
        sha = digest(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f'File changed during inventory: {path}')
        return {'relative': path.relative_to(root).as_posix(),
                'size_bytes': after.st_size, 'sha256': sha}
    with ThreadPoolExecutor(max_workers=4) as executor:
        return list(executor.map(record, paths))

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True, type=Path)
    ap.add_argument('--lock', required=True, type=Path)
    ap.add_argument('--output', required=True, type=Path)
    ap.add_argument('--snapshot-only', action='store_true',
                    help='Historical environment; lock is a source reference, not installation proof')
    args = ap.parse_args()
    root = args.root.resolve(strict=True)
    output = args.output.resolve()
    if output.exists():
        raise RuntimeError('Receipt already exists; use a new evidence path')
    if output.is_relative_to(root):
        raise RuntimeError('Receipt must be outside inventoried environment')
    interpreter = root / 'Scripts/python.exe'
    script = ('import importlib.metadata as m,json,sys; '
              'print(json.dumps({"python":sys.version,"base":sys._base_executable,'
              '"prefix":sys.prefix,"base_prefix":sys.base_prefix,'
              '"packages":sorted([{"name":d.metadata["Name"],"version":d.version}'
              ' for d in m.distributions()],key=lambda x:x["name"].lower())}))')
    data = json.loads(subprocess.check_output([str(interpreter), '-B', '-c', script],
                                             text=True, encoding='utf-8'))
    assert Path(data['prefix']).resolve() == root
    assert Path(data['base_prefix']).resolve() != root
    base_root = Path(data['base_prefix']).resolve()
    if output.is_relative_to(base_root):
        raise RuntimeError('Receipt must be outside inventoried base runtime')
    receipt = {'schema_version': 1, 'root': str(root),
               'environment_status': 'historical_snapshot_only' if args.snapshot_only else 'hash_locked_installation',
               'lock_applied': not args.snapshot_only,
               'interpreter': entry(interpreter),
               'base_interpreter': entry(Path(data['base'])),
               'base_runtime': {'root': str(base_root), 'files': files(base_root)},
               'python': data['python'], 'packages': data['packages'],
               'requirements_lock': entry(args.lock), 'files': files(root)}
    receipt['requirements_lock']['role'] = ('source_reference_only' if args.snapshot_only
                                             else 'resolved_install_lock')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps({'receipt': entry(args.output), 'files': len(receipt['files']),
                      'base_files': len(receipt['base_runtime']['files'])}, indent=2))

if __name__ == '__main__':
    main()
