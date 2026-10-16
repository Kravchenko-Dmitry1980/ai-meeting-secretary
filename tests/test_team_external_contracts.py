"""Pinned external contracts and installation refusal before any mutation."""
from __future__ import annotations

import importlib.util
import hashlib
import subprocess
from pathlib import Path
import zipfile

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_installer():
    source = ROOT / "scripts/team/install_vikunja.py"
    assert source.is_file(), "verified project-local installer not implemented"
    spec = importlib.util.spec_from_file_location("vikunja_installer", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_contract_handles_v2_items_problem_json_and_empty_204():
    source = ROOT / "backend/secretary/infrastructure/integration_contracts.py"
    assert source.is_file(), "external response contract not implemented"
    spec = importlib.util.spec_from_file_location("integration_contracts", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    page = module.decode_vikunja_response(httpx.Response(200, json={
        "items": [{"id": 1}], "total": 1, "page": 1, "per_page": 50, "total_pages": 1}))
    assert module.collection_items(page) == [{"id": 1}]
    assert module.decode_vikunja_response(httpx.Response(204)) is None
    with pytest.raises(module.ExternalContractError) as failure:
        module.decode_vikunja_response(httpx.Response(422, json={
            "type": "about:blank", "title": "Validation", "status": 422,
            "detail": "synthetic-private-payload"}))
    assert failure.value.status_code == 422
    assert "synthetic-private-payload" not in str(failure.value)
    with pytest.raises(module.ExternalContractError):
        module.collection_items({"items": {}, "total": 1})


def test_setup_rejects_hash_mismatch_and_never_overwrites_live_install(tmp_path):
    installer = load_installer()
    archive = tmp_path / "synthetic.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("vikunja.exe", b"synthetic-not-executable")
    target = tmp_path / "runtime"
    manifest = {"version": "2.7.0", "architecture": "amd64", "sha256": "0" * 64,
                "signing_key_id": "FF054DACD908493A", "signature_check": "verified"}
    with pytest.raises(installer.InstallError, match="hash_mismatch"):
        installer.install(archive, target, manifest, gpg=tmp_path / "missing-gpg",
                          key=tmp_path / "missing-key", signature=tmp_path / "missing-signature")
    assert not target.exists()
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("known-good")
    with pytest.raises(installer.InstallError, match="destination_exists"):
        installer.install(archive, target, manifest, gpg=tmp_path / "missing-gpg",
                          key=tmp_path / "missing-key", signature=tmp_path / "missing-signature")
    assert sentinel.read_text() == "known-good"


@pytest.mark.parametrize("entry", ["../escape.exe", "/absolute.exe", "C:/escape.exe", "safe/../../escape.exe"])
def test_installer_refuses_unsafe_archive_paths(tmp_path, entry):
    installer = load_installer()
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(entry, "synthetic")
    with zipfile.ZipFile(archive) as handle, pytest.raises(installer.InstallError, match="unsafe_archive"):
        installer.validate_members(handle, tmp_path / "stage")
    assert not (tmp_path / "escape.exe").exists()


def test_git_gpg_uses_msys_paths_without_touching_default_keyring(tmp_path, monkeypatch):
    installer = load_installer()
    calls = []
    def synthetic_run(args, **kwargs):
        calls.append(args)
        class Result:
            returncode = 0
            stdout = "synthetic"
        return Result()
    monkeypatch.setattr(installer.subprocess, "run", synthetic_run)
    installer._gpg(Path(r"C:\Program Files\Git\usr\bin\gpg.exe"), tmp_path,
                   "--import", str(tmp_path / "key"))
    assert calls[0][calls[0].index("--homedir") + 1].startswith("/d/")
    assert "--no-auto-key-retrieve" in calls[0]
    assert "--no-options" in calls[0]


@pytest.mark.parametrize("accepted", [False, True])
def test_install_requires_actual_pinned_signature_before_extracting(tmp_path, monkeypatch, accepted):
    installer = load_installer()
    archive = tmp_path / "synthetic.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("vikunja-fixture.exe", b"synthetic-not-executable")
    fingerprint = "7D061A4AA61436B40713D42EFF054DACD908493A"
    manifest = {"sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "signing_key_id": fingerprint[-16:], "signing_fingerprint": fingerprint,
                "signature_check": "verified"}
    gpg, key, signature = (tmp_path / name for name in ("gpg", "key", "signature"))
    for path in (gpg, key, signature):
        path.write_bytes(b"synthetic")
    def verify(*args):
        if "--verify" not in args:
            return ""
        signer = fingerprint if accepted else "0" * 40
        return f"[GNUPG:] VALIDSIG {signer} 2026-10-03 1 0 4 0 1 10 00 {signer}"
    monkeypatch.setattr(installer, "_gpg", verify)
    target = tmp_path / "install"
    if accepted:
        executable = installer.install(archive, target, manifest, gpg=gpg, key=key, signature=signature)
        assert executable.read_bytes() == b"synthetic-not-executable"
    else:
        with pytest.raises(installer.InstallError, match="signature_identity_mismatch"):
            installer.install(archive, target, manifest, gpg=gpg, key=key, signature=signature)
        assert not target.exists()


def test_contract_rejects_malformed_success_without_exposing_body():
    source = ROOT / "backend/secretary/infrastructure/integration_contracts.py"
    spec = importlib.util.spec_from_file_location("integration_contracts", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for response in (httpx.Response(200, text="private-malformed"),
                     httpx.Response(200, json="private-value"),
                     httpx.Response(302, headers={"Location": "https://example.invalid"}),
                     httpx.Response(204, content=b"unexpected")):
        with pytest.raises(module.ExternalContractError) as failure:
            module.decode_vikunja_response(response)
        assert "private" not in str(failure.value)


def test_setup_target_cannot_escape_allowed_root_via_junction(tmp_path):
    installer = load_installer()
    allowed = tmp_path / "runtime"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    junction = allowed / "redirect"
    subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)],
                   check=True, capture_output=True)
    with pytest.raises(installer.InstallError, match="outside_allowed_root"):
        installer.validate_target(junction / "install", allowed)
    assert not (outside / "install").exists()
