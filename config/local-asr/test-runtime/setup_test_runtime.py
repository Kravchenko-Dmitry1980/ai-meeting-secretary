"""Create a new model-free package-test environment from the checked-in hash lock.

No existing environment is modified. Downloads use public PyPI wheels only.
This script does not run package tests, model code, services or user workloads.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def plain_path(path: Path) -> Path:
    path = path.absolute()
    for part in [*reversed(path.parents), path]:
        if part.exists() or part.is_symlink():
            value = part.lstat()
            if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("Reparse points are not accepted for the test environment")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, type=Path, help="Existing CPython 3.12.13 executable")
    parser.add_argument("--uv", required=True, type=Path, help="Existing uv executable")
    parser.add_argument("--destination", required=True, type=Path, help="New directory below this project's .runtime")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / ".runtime/local-asr-test-runtime-cache-20261026")
    args = parser.parse_args()
    destination = plain_path(args.destination)
    cache = plain_path(args.cache_dir)
    runtime_root = (ROOT / ".runtime").resolve()
    for path in (destination, cache):
        if not path.resolve().is_relative_to(runtime_root) or path.resolve() == runtime_root:
            parser.error("Environment and cache must stay below this project's .runtime directory")
    if destination.exists():
        parser.error("Destination already exists; choose a new directory")
    receipt_path = destination.parent / (destination.name + "-setup.json")
    if receipt_path.exists():
        parser.error("A setup receipt already exists; choose a new directory")
    base, uv = plain_path(args.python), plain_path(args.uv)
    if not base.is_file() or not uv.is_file():
        parser.error("Existing Python and uv executables are required")

    # Isolate registry configuration without displaying ambient values.
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("UV_", "PIP_"))
           and key.upper() not in {"VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH", "PYTHONHOME"}}
    env.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    probe = "import sys,struct,json; print(json.dumps([list(sys.version_info[:3]),sys.platform,struct.calcsize('P')*8]))"
    base_profile = json.loads(subprocess.check_output([str(base), "-I", "-c", probe], env=env, text=True))
    if base_profile != [[3, 12, 13], "win32", 64]:
        parser.error("This tested rebuild procedure requires Windows x64 CPython 3.12.13")
    lock = HERE / "requirements.lock"
    lock_sha = hashlib.sha256(lock.read_bytes()).hexdigest()
    # Reserve ownership atomically before venv can touch this directory.
    # A second concurrent setup must fail, never reuse this in-progress root.
    destination.mkdir(parents=True, exist_ok=False)
    subprocess.run([str(base), "-I", "-m", "venv", "--without-pip", str(destination)], env=env, check=True)
    python = destination / "Scripts/python.exe"
    common = ["--python", str(python), "--config-file", str(HERE / "uv.toml"), "--no-progress"]
    subprocess.run([str(uv), "pip", "sync", str(lock), "--require-hashes", "--only-binary", ":all:",
                    "--default-index", "https://pypi.org/simple", "--keyring-provider", "disabled",
                    "--no-python-downloads", "--link-mode", "copy", "--cache-dir", str(cache), *common],
                   env=env, cwd=HERE, check=True)
    subprocess.run([str(uv), "pip", "check", *common], env=env, cwd=HERE, check=True)
    probe = """
import importlib.metadata as md, importlib.util, json, sys
import pytest, jsonschema, numpy, yaml, psutil, reportlab, pypdf
blocked = ['torch', 'transformers', 'speechbrain', 'faster_whisper', 'nemo', 'pyannote']
assert not any(importlib.util.find_spec(name) is not None for name in blocked)
print(json.dumps({'python':sys.version, 'packages':{d.metadata['Name']:d.version for d in md.distributions()}, 'model_packages_present':False}))
"""
    probe_result = json.loads(subprocess.check_output([str(python), "-I", "-B", "-c", probe],
                                                     env=env, cwd=HERE, text=True))
    receipt = {"schema_version": "secretary.local-asr.test-runtime.v1", "status": "dependencies_verified",
               "environment": str(destination), "requirements_lock_sha256": lock_sha,
               "registry": "https://pypi.org/simple", "hashes_required": True,
               "wheels_only": True, "package_check": "PASS", "imports": "PASS",
               "tests_run": False, "models_loaded": False, **probe_result}
    with receipt_path.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"status": receipt["status"], "receipt": str(receipt_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
