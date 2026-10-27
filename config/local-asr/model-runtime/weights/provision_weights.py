"""Provision hash-pinned Stage 2 model files without importing model libraries.

Only writes the selected model/evidence directories. Original handoff stays read-only.
No Hugging Face token, package installation, inference, or application activation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import stat
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

REVISIONS = {
    "rnnt": "7655ad717f8122257385bb4b2f373db3697e8680",
    "ctc": "cec030b4c4f35d928e4a9044a3bdb29ebd499fac",
    "nemotron": "f667ed73aee57d40cc39428eb768b4fd87a0a29e",
}
OLD_RNNT = "7655ad717f8122257385bb4b2f373db3697e8686"
CONTRACT_NAMES = {
    "rnnt": "dev-gigaam-model-manifest.json",
    "ctc": "dev-gigaam-ctc-model-manifest.json",
    "nemotron": "dev-nemotron3-model-manifest.json",
}
WEIGHTS = {"rnnt": "pytorch_model.bin", "ctc": "pytorch_model.bin", "nemotron": "model.safetensors"}
DELIVERY_SHA256 = "b87031fd9bbb97c7cd9ae7865fc0618618da2c9ae511d37e55a50b8c2712d4a1"
CHUNK_BYTES = 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identity(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Missing or linked file: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_BYTES):
            size += len(chunk)
            digest.update(chunk)
    return {"size_bytes": size, "sha256": digest.hexdigest()}


def verify(path: Path, expected: dict) -> dict:
    actual = identity(path)
    if actual != {key: expected[key] for key in ("size_bytes", "sha256")}:
        raise ValueError(f"Size or SHA-256 mismatch: {path}")
    return actual


def plain_output_path(path: Path, boundary: Path) -> None:
    """Reject Windows junctions/reparse points as well as symbolic links."""
    if ".." in path.parts or not path.is_relative_to(boundary):
        raise ValueError("Output path escapes its declared boundary")
    for candidate in (boundary, *(boundary / Path(*path.relative_to(boundary).parts[:i])
                                  for i in range(1, len(path.relative_to(boundary).parts) + 1))):
        if os.path.lexists(candidate):
            metadata = candidate.lstat()
            if candidate.is_symlink() or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError(f"Output path contains a link or reparse point: {candidate}")
    if not path.resolve().is_relative_to(boundary.resolve()):
        raise ValueError("Resolved output path escapes its declared boundary")


def source_inventory(source: Path, delivery: dict) -> dict:
    expected_paths = set()
    for item in delivery["files"]:
        relative = Path(item["relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe delivery manifest path")
        target = source / relative
        if not target.resolve().is_relative_to(source):
            raise ValueError("Delivery manifest escapes source root")
        verify(target, item)
        expected_paths.add(relative.as_posix())
    actual_paths = {path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file()}
    if actual_paths != expected_paths or len(expected_paths) != 3378:
        raise ValueError("Original handoff file inventory changed")
    return {"status": "PASS", "verified_files": len(expected_paths)}


def assert_https(url: str) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Only credential-free HTTPS on port 443 is permitted")
    if not (host == "huggingface.co" or host.endswith(".huggingface.co") or host == "hf.co" or host.endswith(".hf.co")):
        raise ValueError("Download redirect is outside official Hugging Face hosts")


class BoundedRedirect(HTTPRedirectHandler):
    max_redirections = 5

    def __init__(self, deadline: float):
        self.deadline = deadline

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_https(newurl)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Download deadline exceeded during redirect")
        req.timeout = min(45, remaining)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, destination: Path, expected: dict, deadline_seconds: int) -> dict:
    assert_https(url)
    plain_output_path(destination, destination.parent.parent)
    if destination.exists():
        return {**verify(destination, expected), "action": "existing_verified", "source_url": url}
    partial = destination.with_name(destination.name + ".part")
    plain_output_path(partial, destination.parent.parent)
    if partial.exists():
        raise FileExistsError(f"Preserved incomplete download requires review: {partial}")
    started = time.monotonic()
    deadline = started + deadline_seconds
    digest = hashlib.sha256()
    size = 0
    request = Request(url, headers={"User-Agent": "Secretary-Stage2-Weight-Provisioner/1", "Accept-Encoding": "identity"})
    opener = build_opener(BoundedRedirect(deadline), HTTPSHandler(context=ssl.create_default_context()))
    with opener.open(request, timeout=min(45, deadline_seconds)) as response:
        assert_https(response.url)
        if response.status != 200:
            raise ValueError("Model download did not return HTTP 200")
        content_length = response.headers.get("Content-Length")
        if content_length is not None and int(content_length) != expected["size_bytes"]:
            raise ValueError("HTTP Content-Length does not match pinned size")
        final_host = urlsplit(response.url).hostname
        next_progress = 64 * CHUNK_BYTES
        with partial.open("xb") as output:
            while response.fp is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Per-file download deadline exceeded")
                transport = getattr(getattr(response.fp, "raw", None), "_sock", None)
                if transport is None:
                    raise RuntimeError("Cannot enforce remaining-time timeout on HTTPS stream")
                transport.settimeout(min(45, remaining))
                # One transport read prevents a slow trickle from filling a large buffered read indefinitely.
                chunk = response.read1(CHUNK_BYTES)
                if not chunk:
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("Per-file download deadline exceeded")
                size += len(chunk)
                if size > expected["size_bytes"]:
                    raise ValueError("Download exceeded pinned byte length")
                output.write(chunk)
                digest.update(chunk)
                if size >= next_progress:
                    print(json.dumps({"event": "download_progress", "role": destination.parent.name, "bytes": size}), flush=True)
                    next_progress += 64 * CHUNK_BYTES
            output.flush()
            os.fsync(output.fileno())
    actual = {"size_bytes": size, "sha256": digest.hexdigest()}
    if actual != expected:
        raise ValueError("Downloaded bytes do not match the pinned weight contract")
    # Re-read from disk before making the weight filename available to loaders.
    verify(partial, expected)
    partial.rename(destination)
    return {**actual, "action": "downloaded_verified", "source_url": url,
            "http_status": 200, "final_host": final_host,
            "elapsed_seconds": round(time.monotonic() - started, 3)}


def copy_verified(source: Path, destination: Path, expected: dict) -> dict:
    plain_output_path(destination, destination.parent.parent)
    verify(source, expected)
    if destination.exists():
        return verify(destination, expected)
    with destination.open("xb") as output, source.open("rb") as input_file:
        shutil.copyfileobj(input_file, output)
    return verify(destination, expected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument("--model-root", type=Path, default=Path(".runtime/local-asr-models-20261027"))
    parser.add_argument("--evidence-dir", type=Path, default=Path(".runtime/model-pilot-evidence-20261027"))
    parser.add_argument("--download-deadline-seconds", type=int, default=1800)
    parser.add_argument("--verify-only", action="store_true", help="No download or overlay changes; write a fresh verification receipt")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    source = workspace / ".runtime/source-handoff-20261026-150225/payload"
    model_root = workspace / args.model_root
    evidence_dir = workspace / args.evidence_dir
    runtime_root = workspace / ".runtime"
    for target in (model_root, evidence_dir):
        plain_output_path(target, runtime_root)
        if not target.is_relative_to(runtime_root) or target == runtime_root or target.is_relative_to(source):
            raise ValueError("Output directories must be isolated below workspace .runtime")
    if not 1 <= args.download_deadline_seconds <= 3600:
        raise ValueError("Download deadline must be between 1 and 3600 seconds")
    delivery_path = source.parent / "delivery-manifest.json"
    delivery_hash = identity(delivery_path)
    if delivery_hash["sha256"] != DELIVERY_SHA256:
        raise ValueError("Delivery manifest identity changed")
    delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
    delivery_files = {item["relative_path"]: item for item in delivery["files"]}
    receipt = {"schema_version": "secretary.local-asr.weights-provisioning.v1", "started_utc": utc_now(),
               "status": "IN_PROGRESS", "mode": "verify_only" if args.verify_only else "provision",
               "source_root": str(source), "model_root": str(model_root),
               "contracts_root": str(model_root), "delivery_manifest": delivery_hash,
               "provisioner": {"path": str(Path(__file__).resolve()), **identity(Path(__file__).resolve())},
               "model_inference": "NOT_RUN", "application_activation": "NOT_RUN", "models": {}}
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    receipt_path = evidence_dir / f"weights-{stamp}.json"
    try:
        receipt["source_before"] = source_inventory(source, delivery)
        print(json.dumps({"event": "source_verified", "files": 3378}), flush=True)
        contracts = {role: json.loads((source / "contracts" / name).read_text(encoding="utf-8")) for role, name in CONTRACT_NAMES.items()}
        minimum_free = sum(contracts[role]["files"][WEIGHTS[role]]["size_bytes"] for role in REVISIONS) + 128 * CHUNK_BYTES
        if not args.verify_only and shutil.disk_usage(runtime_root).free < minimum_free:
            raise OSError("Insufficient disk space for all pinned weights and reserve")
        for role, revision in REVISIONS.items():
            manifest = contracts[role]
            if manifest["revision"] != (OLD_RNNT if role == "rnnt" else revision):
                raise ValueError(f"Unexpected original revision for {role}")
            destination = model_root / role
            plain_output_path(destination, model_root)
            allowed_names = {entry.name for entry in (source / "model-metadata" / role).iterdir()} | {WEIGHTS[role]}
            if destination.exists() and {entry.name for entry in destination.iterdir()} - allowed_names:
                raise ValueError(f"Unexpected files in model directory: {role}")
            if not args.verify_only:
                destination.mkdir(parents=True, exist_ok=True)
            entries = {}
            for metadata in sorted((source / "model-metadata" / role).iterdir()):
                if not metadata.is_file():
                    raise ValueError("Unexpected nested model metadata")
                expected = delivery_files[metadata.relative_to(source).as_posix()]
                entries[metadata.name] = (verify(destination / metadata.name, expected) if args.verify_only
                                          else copy_verified(metadata, destination / metadata.name, expected))
            weight = WEIGHTS[role]
            url = f"https://huggingface.co/{manifest['repository']}/resolve/{revision}/{weight}"
            entries[weight] = (verify(destination / weight, manifest["files"][weight]) if args.verify_only
                               else download(url, destination / weight, manifest["files"][weight], args.download_deadline_seconds))
            for filename, expected in manifest["files"].items():
                verify(destination / filename, expected)
            if {entry.name for entry in destination.iterdir()} != allowed_names:
                raise ValueError(f"Model directory file inventory changed: {role}")
            if role == "nemotron" and identity(destination / "PROVENANCE.json")["sha256"] != manifest["source_provenance_sha256"]:
                raise ValueError("Nemotron provenance identity mismatch")
            receipt["models"][role] = {"repository": manifest["repository"], "revision": revision, "status": "PASS", "files": entries}
            print(json.dumps({"event": "model_verified", "role": role}), flush=True)
        # Only after all three model trees have passed verification, materialize a contracts overlay.
        overlay = model_root / "contracts"
        plain_output_path(overlay, model_root)
        if not args.verify_only:
            overlay.mkdir(exist_ok=True)
        receipt["contracts_overlay"] = {}
        for original in sorted((source / "contracts").iterdir()):
            content = original.read_bytes()
            changed = original.name == CONTRACT_NAMES["rnnt"]
            if changed:
                if content.count(OLD_RNNT.encode("ascii")) != 1:
                    raise ValueError("Expected exactly one RNNT revision occurrence")
                content = content.replace(OLD_RNNT.encode("ascii"), REVISIONS["rnnt"].encode("ascii"), 1)
            target = overlay / original.name
            plain_output_path(target, model_root)
            if not args.verify_only and not target.exists():
                with target.open("xb") as output:
                    output.write(content)
            if target.read_bytes() != content:
                raise ValueError(f"Contracts overlay identity mismatch: {original.name}")
            receipt["contracts_overlay"][original.name] = {**identity(target), "revision_only_change": changed,
                                                           "original_sha256": identity(original)["sha256"]}
        receipt["source_after"] = source_inventory(source, delivery)
        receipt["total_weight_bytes"] = sum(contracts[role]["files"][WEIGHTS[role]]["size_bytes"] for role in REVISIONS)
        receipt["status"] = "PASS"
    except Exception as exc:
        receipt["status"] = "FAIL"
        # Do not persist redirect URLs, signed query strings, tokens, or network exception details.
        receipt["failure"] = {"type": type(exc).__name__}
        if isinstance(exc, HTTPError):
            receipt["failure"]["http_status"] = exc.code
        elif not isinstance(exc, OSError):
            receipt["failure"]["message"] = str(exc)
        raise
    finally:
        receipt["completed_utc"] = utc_now()
        with receipt_path.open("x", encoding="utf-8") as output:
            json.dump(receipt, output, ensure_ascii=False, indent=2)
            output.write("\n")
        print(json.dumps({"event": "receipt", "status": receipt["status"], "path": str(receipt_path)}), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"event": "failed", "error_type": type(exc).__name__}), file=sys.stderr, flush=True)
        sys.exit(1)
