"""Prepare audited build copies. Does not execute downloaded code."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[4]
RUNTIME = ROOT / '.runtime/model-runtime-20261027'
SOURCE = ROOT / '.runtime/source-handoff-20261026-150225/payload/external/transformers'

def digest(path: Path) -> str:
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def inventory(root: Path) -> list[dict]:
    return [dict(relative=p.relative_to(root).as_posix(), size_bytes=p.stat().st_size,
                 sha256=digest(p)) for p in sorted(root.rglob('*')) if p.is_file()]

def main() -> None:
    sources = RUNTIME / 'build-sources'
    sources.mkdir(exist_ok=True)
    dest = sources / 'transformers'
    if dest.exists():
        raise RuntimeError('Build source already exists; refusing to overwrite')
    before = inventory(SOURCE)
    shutil.copytree(SOURCE, dest)
    assert inventory(dest) == before
    assert inventory(SOURCE) == before
    record = {'schema_version': 1, 'source': str(SOURCE),
              'source_commit': '89b6b17574892ec0770551537a3fe69d6886703e',
              'build_copy': str(dest), 'files': before}
    (RUNTIME / 'evidence/transformers-build-source.json').write_text(
        json.dumps(record, indent=2) + '\n', encoding='utf-8')
    url = 'https://pypi.org/pypi/antlr4-python3-runtime/4.9.3/json'
    with urllib.request.urlopen(url, timeout=60) as response:
        metadata = json.load(response)
    candidates = [a for a in metadata['urls'] if a['packagetype'] == 'sdist']
    assert len(candidates) == 1
    item = candidates[0]
    assert item['url'].startswith('https://files.pythonhosted.org/')
    archive = sources / item['filename']
    with urllib.request.urlopen(item['url'], timeout=60) as response, archive.open('xb') as output:
        shutil.copyfileobj(response, output)
    assert archive.stat().st_size == item['size']
    assert digest(archive) == item['digests']['sha256']
    with tarfile.open(archive, 'r:gz') as tar:
        tar.extractall(sources, filter='data')
    record = {'schema_version': 1, 'metadata_url': url, 'url': item['url'],
              'archive': str(archive), 'size_bytes': item['size'],
              'sha256': item['digests']['sha256'], 'license': metadata['info']['license']}
    (RUNTIME / 'evidence/antlr-build-source.json').write_text(
        json.dumps(record, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'transformers_files': len(before), 'antlr': record}, indent=2))

if __name__ == '__main__':
    main()
