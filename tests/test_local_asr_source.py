"""Source intake tampering checks; synthetic text files only."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from secretary.infrastructure.local_asr_source import SourceVerificationError, verify_source_bundle


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build_bundle(tmp_path, *, admission=False, canonical_hash=None, inner_hash=None):
    package = tmp_path / "payload"
    package.mkdir()
    code = b"raise RuntimeError('must never execute')\n"
    files = {"scripts/example.py": code}
    canonical = {
        "schema_version": "neurohr.dev-asr-package.v1", "inference_allowed": admission,
        "platform_admissible": False, "model_free_only": True,
        "files": [{"path": "scripts/example.py", "sha256": canonical_hash or digest(code)}],
    }
    files["contracts/dev-asr-package.v1.json"] = encoded(canonical)

    def rows(items):
        return [{"relative_path": path, "size_bytes": len(data), "sha256": digest(data)}
                for path, data in items.items()]

    inner = {"schema_version": "neurosecretary.handoff-files.v1", "algorithm": "SHA-256",
             "file_count": len(files), "files": rows(files)}
    if inner_hash:
        inner["files"][0]["sha256"] = inner_hash
    files["FILES_MANIFEST.json"] = encoded(inner)
    for path, data in files.items():
        target = package / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    delivery = {"schema_version": "neurosecretary.delivery-files.v1", "algorithm": "SHA-256",
                "archive_sha256": "a" * 64, "file_count": len(files), "files": rows(files)}
    manifest = tmp_path / "delivery.json"
    manifest.write_bytes(encoded(delivery))
    return package, manifest, {
        "delivery_manifest_sha256": digest(manifest.read_bytes()),
        "package_manifest_sha256": digest(files["contracts/dev-asr-package.v1.json"]),
        "expected_source_files": 1,
    }


def test_integrity_never_executes_source_or_authorizes_runtime(tmp_path):
    package, manifest, pins = build_bundle(tmp_path)
    before = {path: path.read_bytes() for path in package.rglob("*") if path.is_file()}
    result = verify_source_bundle(package, manifest, **pins)
    assert result["status"] == "source_integrity_verified"
    assert result["verified_files"] == 3 and result["canonical_source_files"] == 1
    for field in ("donor_code_executed", "model_weights_verified", "inference_authorized", "runtime_qualified"):
        assert result[field] is False
    assert before == {path: path.read_bytes() for path in package.rglob("*") if path.is_file()}


@pytest.mark.parametrize("mode", ["changed", "missing", "extra", "changed_manifest", "wrong_pin"])
def test_file_and_manifest_tampering_refused(tmp_path, mode):
    package, manifest, pins = build_bundle(tmp_path)
    if mode == "changed":
        (package / "scripts/example.py").write_bytes(b"changed")
    elif mode == "missing":
        (package / "scripts/example.py").unlink()
    elif mode == "extra":
        (package / "unexpected.py").write_bytes(b"extra")
    elif mode == "changed_manifest":
        manifest.write_bytes(manifest.read_bytes() + b" ")
    else:
        pins["package_manifest_sha256"] = "b" * 64
    with pytest.raises(SourceVerificationError):
        verify_source_bundle(package, manifest, **pins)


@pytest.mark.parametrize("changes,reason", [
    ({"admission": True}, "invalid_package_admission"),
    ({"canonical_hash": "b" * 64}, "canonical_source_hash_mismatch"),
    ({"inner_hash": "b" * 64}, "inner_manifest_mismatch"),
])
def test_repacked_internal_inconsistency_refused(tmp_path, changes, reason):
    package, manifest, pins = build_bundle(tmp_path, **changes)
    with pytest.raises(SourceVerificationError, match=reason):
        verify_source_bundle(package, manifest, **pins)


@pytest.mark.parametrize("path", ["../outside", "/absolute", "C:/outside", "x\\y", "x:stream", "CON.txt", "a./b"])
def test_untrusted_paths_rejected_before_access(tmp_path, path):
    package, manifest, pins = build_bundle(tmp_path)
    document = json.loads(manifest.read_bytes())
    document["files"][0]["relative_path"] = path
    manifest.write_bytes(encoded(document))
    pins["delivery_manifest_sha256"] = digest(manifest.read_bytes())
    with pytest.raises(SourceVerificationError, match="relative_path"):
        verify_source_bundle(package, manifest, **pins)


def test_windows_case_collision_is_refused(tmp_path):
    package, manifest, pins = build_bundle(tmp_path)
    document = json.loads(manifest.read_bytes())
    document["files"].append({**document["files"][0], "relative_path": "SCRIPTS/EXAMPLE.py"})
    manifest.write_bytes(encoded(document))
    pins["delivery_manifest_sha256"] = digest(manifest.read_bytes())
    with pytest.raises(SourceVerificationError, match="duplicate_manifest_path"):
        verify_source_bundle(package, manifest, **pins)


def test_hard_link_cannot_alias_an_unrelated_file(tmp_path):
    package, manifest, pins = build_bundle(tmp_path)
    source = package / "scripts/example.py"
    (tmp_path / "alias.py").hardlink_to(source)
    with pytest.raises(SourceVerificationError, match="source_not_plain_file"):
        verify_source_bundle(package, manifest, **pins)


def test_missing_extraction_is_a_bounded_diagnostic(tmp_path):
    with pytest.raises(SourceVerificationError, match="source_io_unavailable"):
        verify_source_bundle(tmp_path / "absent", tmp_path / "absent.json",
                             delivery_manifest_sha256="a" * 64, package_manifest_sha256="b" * 64)


def test_preparation_presence_scan_does_not_crash_on_escaping_path(tmp_path):
    script = Path(__file__).resolve().parents[1] / "config/local-asr/verify_preparation.py"
    spec = importlib.util.spec_from_file_location("local_asr_preparation", script)
    preparation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preparation)
    outside = tmp_path / "outside.py"
    outside.write_text("synthetic", encoding="utf8")
    package = tmp_path / "package"
    package.mkdir()
    assert preparation.missing_source_artifacts(package, ["../outside.py", "missing.py"]) == [
        "../outside.py", "missing.py"]
