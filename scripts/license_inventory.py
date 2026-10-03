"""Record installed distribution licenses and preserve their shipped notice text."""
from __future__ import annotations

import importlib.metadata as metadata
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
out = root / 'docs' / 'licenses'
out.mkdir(parents=True, exist_ok=True)
python_packages = []
for dist in sorted(metadata.distributions(), key=lambda item: item.metadata['Name'].lower()):
    name = dist.metadata['Name']
    declared = dist.metadata.get('License-Expression') or dist.metadata.get('License')
    classifiers = [value for value in dist.metadata.get_all('Classifier', []) if value.startswith('License ::')]
    saved = []
    for entry in dist.files or []:
        filename = str(entry)
        if '.dist-info/' not in filename or not any(token in filename.lower().split('/')[-1] for token in ('license', 'copying', 'notice')):
            continue
        path = Path(dist.locate_file(entry))
        if not path.is_file() or path.stat().st_size > 256000:
            continue
        try:
            content = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        target = out / 'python' / name / Path(filename).name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
        saved.append(str(target.relative_to(root)).replace('\\', '/'))
    python_packages.append({'name': name, 'version': dist.version, 'license': declared, 'classifiers': classifiers, 'preserved_notices': saved})
npm_packages = []
lock = json.loads((root / 'frontend' / 'package-lock.json').read_text(encoding='utf-8'))
for relative, package in lock.get('packages', {}).items():
    if not relative:
        continue
    name = relative.rsplit('node_modules/', 1)[-1]
    package_dir = root / 'frontend' / relative
    saved = []
    if package_dir.is_dir():
        for path in package_dir.iterdir():
            if not path.is_file() or not path.name.lower().startswith(('license', 'licence', 'notice', 'copying')) or path.stat().st_size > 256000:
                continue
            try:
                content = path.read_text(encoding='utf-8')
            except UnicodeDecodeError:
                continue
            target = out / 'npm' / name.replace('/', '__') / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
            saved.append(str(target.relative_to(root)).replace('\\', '/'))
    npm_packages.append({'name': name, 'version': package.get('version'), 'license': package.get('license'), 'dev': package.get('dev', False), 'optional': package.get('optional', False), 'preserved_notices': saved})
inventory = {'scope': 'actual Python environment and npm lock; license metadata is not legal qualification of all bundled code', 'python': python_packages, 'npm': npm_packages}
target = out / 'inventory.json'
target.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding='utf-8')
print(f'{len(python_packages)} Python distributions; {len(npm_packages)} npm lock entries; {target}')
