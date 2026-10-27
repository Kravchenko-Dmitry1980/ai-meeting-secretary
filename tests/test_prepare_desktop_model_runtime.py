"""A portable plan must retain pinned identities without invoking installers."""
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import urllib.request

import pytest


spec = importlib.util.spec_from_file_location(
    "desktop_runtime_plan", Path(__file__).resolve().parents[1] / "scripts/prepare_desktop_model_runtime.py")
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    root = tmp_path / "Secretary with spaces"
    config = root / planner.CONFIG
    config.mkdir(parents=True)
    wheelhouse = root / "offline assets with spaces"
    wheelhouse.mkdir()
    for kind, profile in planner.PROFILES.items():
        contents = f"synthetic {kind} wheel".encode()
        (wheelhouse / profile["wheel"]).write_bytes(contents)
        digest = hashlib.sha256(contents).hexdigest()
        original = (
            "# historical provenance D:/old source --find-links file:///D:/old/wheels\r\n"
            "--index-url https://pypi.org/simple\r\n"
            "--find-links file:///D:/old/wheels\r\n\r\n"
            f"{profile['package']}=={profile['version']} \\\r\n"
            f"    --hash=sha256:{digest}\r\n"
            "    # from file:///D:/old/wheels\r\n"
        ).encode()
        (config / f"{kind}.lock").write_bytes(original)
    executables = {}
    for name in ("uv", "cpu_python", "nemotron_python"):
        path = tmp_path / f"{name} not executable.exe"
        path.write_bytes(b"fixture only - must never execute")
        executables[name] = path
    def denied(*args, **kwargs):
        raise AssertionError("Plan preparation attempted process or network execution")
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(urllib.request, "urlopen", denied)
    return {"root": root, "output": tmp_path / "new plan with spaces",
            "runtime_root": root / ".runtime/new desktop environments", "wheelhouse": wheelhouse,
            **executables}


def test_plan_preserves_lock_bytes_except_find_links_and_never_installs(inputs):
    originals = {kind: (inputs["root"] / planner.CONFIG / f"{kind}.lock").read_bytes()
                 for kind in planner.PROFILES}
    plan = planner.prepare(**inputs)
    assert plan["status"] == "PLAN_ONLY" and not plan["commands_executed"]
    assert not plan["environments_created"] and not plan["python_versions_verified"]
    assert plan["model_inference"] == "NOT_RUN"
    assert not inputs["runtime_root"].exists()
    for kind, original in originals.items():
        derived = (inputs["output"] / f"{kind}.lock").read_bytes()
        assert planner.FIND_LINKS.sub(b"OPTION", derived) == planner.FIND_LINKS.sub(b"OPTION", original)
        assert b"%20" in planner.FIND_LINKS.search(derived).group()
        assert planner.PACKAGE_HASH.findall(derived) == planner.PACKAGE_HASH.findall(original)
        assert (inputs["root"] / planner.CONFIG / f"{kind}.lock").read_bytes() == original
        commands = plan["profiles"][kind]["commands_not_executed"]
        assert commands[1]["argv"][1:5] == ["-B", "-m", "venv", "--without-pip"]
        install = commands[2]["argv"]
        assert all(flag in install for flag in ("--require-hashes", "--only-binary", "--link-mode", "--torch-backend"))
        assert install[-1] == str(inputs["output"] / f"{kind}.lock")
    assert len(list(inputs["output"].iterdir())) == 3


@pytest.mark.parametrize("damage", ["wrong_wheel", "duplicate_option", "missing_option", "missing_package"])
def test_bad_inputs_create_no_output(inputs, damage):
    lock = inputs["root"] / planner.CONFIG / "asr-cpu.lock"
    if damage == "wrong_wheel":
        (inputs["wheelhouse"] / planner.PROFILES["asr-cpu"]["wheel"]).write_bytes(b"substituted")
    elif damage == "duplicate_option":
        lock.write_bytes(lock.read_bytes() + b"--find-links file:///extra\r\n")
    elif damage == "missing_option":
        lock.write_bytes(planner.FIND_LINKS.sub(b"# removed", lock.read_bytes()))
    else:
        lock.write_bytes(lock.read_bytes().replace(b"antlr4-python3-runtime==", b"other-package=="))
    with pytest.raises(ValueError):
        planner.prepare(**inputs)
    assert not inputs["output"].exists()
    assert not inputs["runtime_root"].exists()


@pytest.mark.parametrize("existing", ["output", "runtime_root"])
def test_existing_destinations_are_preserved(inputs, existing):
    inputs[existing].mkdir(parents=True)
    marker = inputs[existing] / "keep.txt"
    marker.write_bytes(b"existing user content")
    with pytest.raises(ValueError):
        planner.prepare(**inputs)
    assert marker.read_bytes() == b"existing user content"


def test_hash_of_another_package_cannot_authorize_custom_wheel(inputs):
    lock = inputs["root"] / planner.CONFIG / "asr-cpu.lock"
    valid = planner.PACKAGE_HASH.search(lock.read_bytes()).group(1)
    forged = lock.read_bytes().replace(valid, b"0" * 64)
    forged += b"unrelated==1.0 \\\r\n    --hash=sha256:" + valid + b"\r\n"
    lock.write_bytes(forged)
    with pytest.raises(ValueError, match="not allowed"):
        planner.prepare(**inputs)
    assert not inputs["output"].exists()
