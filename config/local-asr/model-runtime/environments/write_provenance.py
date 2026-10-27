"""Seal environment evidence after successful installation and import checks."""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
RUNTIME = ROOT / '.runtime/model-runtime-20261027'
EVIDENCE = RUNTIME / 'evidence'
CONFIG = Path(__file__).resolve().parent

def pin(path: Path) -> dict:
    with path.open('rb') as handle:
        sha = hashlib.file_digest(handle, 'sha256').hexdigest()
    return {'path': str(path.resolve()), 'size_bytes': path.stat().st_size, 'sha256': sha}

def main() -> None:
    environments = {}
    for kind in ('asr-cpu', 'nemotron'):
        inventory = EVIDENCE / f'{kind}.inventory-v2.json'
        data = json.loads(inventory.read_text(encoding='utf-8'))
        probe = EVIDENCE / f'{kind}-probe-v2.json'
        check = json.loads(probe.read_text(encoding='utf-8'))
        assert check['status'] == 'PASS_IMPORTS_ONLY'
        assert check['weights_loaded'] is False and check['inference_executed'] is False
        assert 'All installed packages are compatible' in (EVIDENCE / f'{kind}-pip-check-v2.log').read_text(encoding='utf-8')
        assert inventory.stat().st_size <= 8 * 1024 * 1024
        environments[kind] = {'status': 'PASS_IMPORTS_ONLY', 'root': data['root'],
                              'inventory': pin(inventory), 'probe': pin(probe),
                              'pip_check': pin(EVIDENCE / f'{kind}-pip-check-v2.log'),
                              'requirements_lock': pin(CONFIG / f'{kind}.lock'),
                              'wheel_sources': pin(EVIDENCE / f'{kind}.wheels.json'),
                              'packages': len(data['packages']), 'environment_files': len(data['files']),
                              'base_runtime_files': len(data['base_runtime']['files']),
                              'interpreter': data['interpreter'], 'base_interpreter': data['base_interpreter']}
    source = ROOT / '.runtime/source-handoff-20261026-150225/payload'
    receipt = {'schema_version': 'secretary.model-environments.v1',
               'runtime_root': str(RUNTIME), 'environments': environments,
               'launcher': {'status': 'historical_snapshot_only',
                            'inventory': pin(EVIDENCE / 'launcher.inventory.json'),
                            'lock_role': 'source_reference_only', 'environment_modified': False},
               'source_inputs': [pin(source / 'environments/rnnt-ctc-cpu/requirements.frozen.txt'),
                                 pin(source / 'environments/nemotron/runtime-requirements.frozen.txt'),
                                 pin(EVIDENCE / 'transformers-build-source.json'),
                                 pin(EVIDENCE / 'antlr-build-source.json')],
               'transformers_source_commit': '89b6b17574892ec0770551537a3fe69d6886703e',
               'build_lock': pin(CONFIG / 'build.lock'),
               'local_wheels': [pin(p) for p in sorted((RUNTIME / 'wheelhouse').glob('*.whl'))],
               'uv': {'version': '0.12.3', **pin(Path(r'C:\Users\user\AppData\Local\hermes\bin\uv.exe'))},
               'limitations': ['No model weights loaded or inference executed by environment setup.',
                               'CPU hf-xet 1.6.0 completes an omitted mandatory Windows dependency using the donor Nemotron pin.',
                               'Transformers emits a non-fatal image_like_kwargs docstring diagnostic during import.',
                               'Application launcher is a historical snapshot; its existing uv.lock is not proof of environment reproduction.']}
    output = EVIDENCE / 'environment-provenance.json'
    with output.open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(pin(output), indent=2))

if __name__ == '__main__':
    main()
