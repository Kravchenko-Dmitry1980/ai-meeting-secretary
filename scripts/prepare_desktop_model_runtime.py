"""Write a create-only Windows ASR installation plan; execute no planned command.

Run after extracting the source and offline assets at the desktop ROOT. Supply
the installed uv executable and exact CPython 3.11.14/3.12.10 executables. This
helper only verifies the two custom wheels and writes two derived requirements
locks plus plan.json. It does not install packages, create venvs, or load models.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


CONFIG = Path("config/local-asr/model-runtime/environments")
SOURCE = Path(".runtime/source-handoff-20261026-150225/payload")
MODELS = Path(".runtime/local-asr-models-20261027")
WHEELHOUSE = Path(".runtime/model-runtime-20261027/wheelhouse")
PROFILES = {
    "asr-cpu": {
        "python_version": "3.11.14", "torch_backend": "cpu",
        "role": "GigaAM RNNT and CTC on CPU",
        "wheel": "antlr4_python3_runtime-4.9.3-py3-none-any.whl",
        "package": "antlr4-python3-runtime", "version": "4.9.3",
    },
    "nemotron": {
        "python_version": "3.12.10", "torch_backend": "cu128",
        "role": "Nemotron diarization on NVIDIA CUDA 12.8",
        "wheel": "transformers-5.18.0.dev0-py3-none-any.whl",
        "package": "transformers", "version": "5.18.0.dev0",
    },
}
FIND_LINKS = re.compile(rb"(?m)^[ \t]*--find-links[ \t]+[^\r\n]*")
PACKAGE_HASH = re.compile(rb"--hash=sha256:([a-f0-9]{64})")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def rebind_find_links(original: bytes, wheelhouse: Path) -> bytes:
    """Replace exactly the active option, retaining all other original bytes."""
    matches = list(FIND_LINKS.finditer(original))
    if len(matches) != 1:
        raise ValueError("Expected exactly one active --find-links line")
    match = matches[0]
    replacement = b"--find-links " + wheelhouse.resolve().as_uri().encode("ascii")
    result = original[:match.start()] + replacement + original[match.end():]
    if PACKAGE_HASH.findall(original) != PACKAGE_HASH.findall(result):
        raise ValueError("Package hashes changed")
    return result


def package_hashes(lock: bytes, package: str, version: str) -> set[str]:
    start = re.search(rb"(?m)^" + re.escape(f"{package}=={version}".encode())
                      + rb"(?:[ \t\\\r\n]|$)", lock)
    if start is None:
        raise ValueError(f"Pinned package is missing: {package}=={version}")
    following = lock[start.end():]
    end = re.search(rb"(?m)^[A-Za-z0-9][A-Za-z0-9_.-]*==", following)
    block = following[:end.start()] if end else following
    return {value.decode("ascii") for value in PACKAGE_HASH.findall(block)}


def absolute_executable(path: Path, label: str) -> Path:
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"{label} must name an existing absolute executable path")
    return path.resolve()


def prepare(*, root: Path, output: Path, runtime_root: Path, wheelhouse: Path,
            uv: Path, cpu_python: Path, nemotron_python: Path) -> dict:
    root, output = root.resolve(), output.resolve()
    runtime_root, wheelhouse = runtime_root.resolve(), wheelhouse.resolve()
    uv = absolute_executable(uv, "uv")
    interpreters = {
        "asr-cpu": absolute_executable(cpu_python, "CPython 3.11.14"),
        "nemotron": absolute_executable(nemotron_python, "CPython 3.12.10"),
    }
    if output.exists() or not output.parent.is_dir():
        raise ValueError("Plan output must not exist and its parent must exist")
    if runtime_root.exists():
        raise ValueError("Choose a new, absent runtime root")
    if output == runtime_root or output.is_relative_to(runtime_root) or runtime_root.is_relative_to(output):
        raise ValueError("Plan output and future runtime must be separate")

    locks = {}
    profiles = {}
    for kind, profile in PROFILES.items():
        source = root / CONFIG / f"{kind}.lock"
        original = source.read_bytes()
        wheel = wheelhouse / profile["wheel"]
        wheel_digest = sha256(wheel)
        if wheel_digest not in package_hashes(original, profile["package"], profile["version"]):
            raise ValueError(f"Custom wheel hash is not allowed by {kind}.lock")
        derived = rebind_find_links(original, wheelhouse)
        locks[kind] = derived
        environment = runtime_root / kind
        environment_python = environment / "Scripts/python.exe"
        exact_python = interpreters[kind]
        expected = tuple(int(part) for part in profile["python_version"].split("."))
        version_check = (
            "import struct,sys; "
            f"assert sys.version_info[:3] == {expected!r}, 'Exact Python version required'; "
            "assert sys.platform == 'win32' and struct.calcsize('P') == 8, 'Windows x64 required'"
        )
        profiles[kind] = {
            "role": profile["role"], "python_version": profile["python_version"],
            "base_python": str(exact_python), "environment": str(environment),
            "python": str(environment_python), "torch_backend": profile["torch_backend"],
            "original_lock_relative": (CONFIG / f"{kind}.lock").as_posix(),
            "original_lock_sha256": hashlib.sha256(original).hexdigest(),
            "derived_lock": f"{kind}.lock",
            "derived_lock_sha256": hashlib.sha256(derived).hexdigest(),
            "changed_option": "--find-links", "package_hashes_unchanged": True,
            "custom_wheel": {"path": str(wheel), "sha256": wheel_digest},
            "commands_not_executed": [
                {"purpose": "Check the exact installed Python version and architecture",
                 "argv": [str(exact_python), "-B", "-c", version_check]},
                {"purpose": "Create a standard CPython venv using the exact base interpreter",
                 "argv": [str(exact_python), "-B", "-m", "venv", "--without-pip", str(environment)]},
                {"purpose": "Install hash-locked software dependencies; registry access is required",
                 "argv": [str(uv), "--no-config", "pip", "sync", "--python", str(environment_python),
                          "--torch-backend", profile["torch_backend"], "--require-hashes",
                          "--only-binary", ":all:", "--link-mode", "copy", str(output / f"{kind}.lock")]},
                {"purpose": "Check dependency compatibility",
                 "argv": [str(uv), "--no-config", "pip", "check", "--python", str(environment_python)]},
            ],
        }
    plan = {
        "schema_version": "secretary.desktop-model-runtime-plan.v1",
        "status": "PLAN_ONLY", "platform": "Windows x64", "root": str(root),
        "runtime_root": str(runtime_root), "uv": str(uv),
        "commands_executed": False, "environments_created": False,
        "python_versions_verified": False, "model_inference": "NOT_RUN",
        "asset_paths_relative_to_root": {
            "source_payload": SOURCE.as_posix(), "model_root": MODELS.as_posix(),
            "rnnt": (MODELS / "rnnt").as_posix(), "ctc": (MODELS / "ctc").as_posix(),
            "nemotron": (MODELS / "nemotron").as_posix(),
        },
        "create_directories_before_installation": [str(runtime_root / name) for name in ("tmp", "uv-cache", "python")],
        "installation_process_environment": {
            "UV_CACHE_DIR": str(runtime_root / "uv-cache"),
            "UV_PYTHON_INSTALL_DIR": str(runtime_root / "python"),
            "TEMP": str(runtime_root / "tmp"), "TMP": str(runtime_root / "tmp"),
            "PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        },
        "profiles": profiles,
        "limitations": [
            "This plan verifies only the two custom wheel hashes, not Python versions, source payloads, or model weights.",
            "Run the version checks before creating environments; stop on any failure.",
            "Every planned command is an argv array; execute it as arguments, never as joined shell text.",
            "The two custom wheels are not a complete offline dependency mirror. Registry downloads remain necessary.",
            "Original locks and their provenance comments are unchanged. Derived locks change only --find-links.",
            "After installation, create new target-specific inventories and authority; laptop receipts are historical evidence.",
            "Model loading, GPU admission, inference, voice-bank transfer, and application activation are separate operations.",
        ],
    }
    # Validate all inputs before creating anything; never overwrite an earlier plan.
    output.mkdir()
    for kind, contents in locks.items():
        with (output / f"{kind}.lock").open("xb") as stream:
            stream.write(contents)
    with (output / "plan.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(plan, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True, help="New plan directory; parent must exist")
    parser.add_argument("--runtime-root", type=Path, required=True, help="New future environment root")
    parser.add_argument("--wheelhouse", type=Path, help="Defaults to ROOT/.runtime/model-runtime-20261027/wheelhouse")
    parser.add_argument("--uv", type=Path, required=True, help="Existing absolute uv.exe path")
    parser.add_argument("--cpu-python", type=Path, required=True, help="Existing exact CPython 3.11.14 executable")
    parser.add_argument("--nemotron-python", type=Path, required=True, help="Existing exact CPython 3.12.10 executable")
    args = parser.parse_args()
    plan = prepare(root=args.root, output=args.output, runtime_root=args.runtime_root,
                   wheelhouse=args.wheelhouse or args.root / WHEELHOUSE,
                   uv=args.uv, cpu_python=args.cpu_python, nemotron_python=args.nemotron_python)
    print(json.dumps({"status": plan["status"], "plan": str(args.output.resolve() / "plan.json"),
                      "commands_executed": False, "model_inference": "NOT_RUN"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
