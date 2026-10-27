"""Identify each unique locked wheel matching installed distribution tags.

uv's hash-required installation validates downloaded bytes. This separate
receipt records URLs and exact wheel hashes using public index metadata; it
does not claim to independently re-download the wheel bytes.
"""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from html.parser import HTMLParser
import importlib.metadata
import json
from pathlib import Path
import re
import urllib.parse
import urllib.request

class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.extend(v for k, v in attrs if k == 'href' and v)

def get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=90) as response:
        return response.read()

def wheel_tags(filename: str) -> set[str]:
    py, abi, platform = filename.removesuffix('.whl').rsplit('-', 3)[-3:]
    return {f'{p}-{a}-{s}' for p in py.split('.') for a in abi.split('.') for s in platform.split('.')}

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--lock', type=Path, required=True)
    ap.add_argument('--wheelhouse', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    locks = {}
    current = None
    for line in args.lock.read_text(encoding='utf-8').splitlines():
        match = re.match(r'^([A-Za-z0-9_.-]+)==([^\s\\]+)', line)
        if match:
            current = (re.sub('[-_.]+', '-', match[1]).lower(), match[2])
            locks[current] = set()
        match = re.search(r'--hash=sha256:([a-f0-9]{64})', line)
        if match and current:
            locks[current].add(match[1])
    distributions = list(importlib.metadata.distributions())
    def resolve(dist) -> dict:
        name = re.sub('[-_.]+', '-', dist.metadata['Name']).lower()
        version = dist.version
        allowed = locks[(name, version)]
        tags = set(re.findall(r'^Tag: (.+)$', dist.read_text('WHEEL') or '', re.M))
        candidates = []
        if name in {'antlr4-python3-runtime', 'transformers'}:
            prefix = name.replace('-', '_') + '-' + version + '-'
            for path in args.wheelhouse.glob(prefix + '*.whl'):
                if tags == wheel_tags(path.name):
                    candidates.append({'filename': path.name, 'url': path.resolve().as_uri(),
                                       'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                       'size_bytes': path.stat().st_size, 'source': 'local-audited-build'})
        if not candidates and name in {'torch', 'torchaudio'}:
            backend = version.split('+')[1]
            index = f'https://download.pytorch.org/whl/{backend}/{name}/'
            parser = Links()
            parser.feed(get(index).decode('utf-8'))
            for href in parser.links:
                url = urllib.parse.urljoin(index, href)
                parts = urllib.parse.urlsplit(url)
                filename = urllib.parse.unquote(parts.path.rsplit('/', 1)[-1])
                if not filename.startswith(name + '-' + version + '-') or not filename.endswith('.whl'):
                    continue
                if tags != wheel_tags(filename):
                    continue
                sha = urllib.parse.parse_qs(parts.fragment).get('sha256', [''])[0]
                candidates.append({'filename': filename, 'url': url.split('#')[0],
                                   'sha256': sha, 'source': index})
        if not candidates:
            metadata_url = f'https://pypi.org/pypi/{name}/{version}/json'
            metadata = json.loads(get(metadata_url))
            for artifact in metadata['urls']:
                if artifact['packagetype'] == 'bdist_wheel' and tags == wheel_tags(artifact['filename']):
                    candidates.append({'filename': artifact['filename'], 'url': artifact['url'],
                                       'sha256': artifact['digests']['sha256'],
                                       'size_bytes': artifact['size'], 'source': metadata_url})
        candidates = [c for c in candidates if c['sha256'] in allowed]
        if len(candidates) != 1:
            raise RuntimeError(f'Expected one locked matching wheel for {name}=={version}, found {len(candidates)}')
        return {'name': name, 'version': version, 'installed_tags': sorted(tags), **candidates[0]}
    with ThreadPoolExecutor(max_workers=6) as executor:
        items = sorted(executor.map(resolve, distributions), key=lambda x: x['name'])
    result = {'schema_version': 1, 'environment': sys.prefix,
              'selection_basis': 'unique locked wheel matching installed WHEEL tags',
              'artifact_byte_verification': 'uv pip sync --require-hashes --only-binary :all:',
              'wheels': items}
    with args.output.open('x', encoding='utf-8') as f:
        f.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'receipt': str(args.output), 'wheel_count': len(items)}, indent=2))

if __name__ == '__main__':
    main()
