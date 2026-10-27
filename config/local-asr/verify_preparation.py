"""Read-only preparation inventory. Never imports a model or opens user databases.

Exit 0 means locally recorded preparation checks match; exit 2 means required
runtime/source inputs are missing or differ. Neither outcome qualifies inference.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from secretary.infrastructure.local_asr_source import SourceVerificationError, verify_source_bundle


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def contained_file(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError("manifest_path_outside_root")
    return candidate


def missing_source_artifacts(package: Path, required: list[str]) -> list[str]:
    missing = []
    for relative in required:
        try:
            available = contained_file(package, relative).is_file()
        except (OSError, ValueError):
            # Keep a bounded report even when a path resolves outside the
            # package. The source verifier provides the precise refusal code.
            available = False
        if not available:
            missing.append(relative)
    return missing


def inspect(root: Path, package: Path | None = None, benchmark: Path | None = None,
            delivery_manifest: Path | None = None) -> dict:
    manifest = json.loads((root / "config/local-asr/runtime-manifest.json").read_text(encoding="utf-8"))
    if manifest["schema_version"] != "secretary.local-asr.runtime-preparation.v1":
        raise ValueError("unsupported_preparation_schema")
    checks = []
    for name in ("application", "identity"):
        environment = manifest["environments"][name]
        path = contained_file(root, environment["lock"])
        checks.append({"artifact": environment["lock"], "exists": path.is_file(),
                       "sha256_matches": path.is_file() and sha256(path) == environment["lock_sha256"]})
    identity = manifest["environments"]["identity"]
    model_manifest_path = contained_file(root, identity["model_manifest"])
    checks.append({"artifact": identity["model_manifest"], "exists": model_manifest_path.is_file(),
                   "sha256_matches": model_manifest_path.is_file() and sha256(model_manifest_path) == identity["model_manifest_sha256"]})
    for relative, key in (("config/voice-runtime/tensor_loader.py", "loader_sha256"),
                          ("scripts/voice_engine.py", "worker_sha256")):
        path = contained_file(root, relative)
        checks.append({"artifact": relative, "exists": path.is_file(),
                       "sha256_matches": path.is_file() and sha256(path) == identity[key]})
    model_checks = []
    if model_manifest_path.is_file():
        model_manifest = json.loads(model_manifest_path.read_text(encoding="utf-8"))
        for artifact in model_manifest["files"]:
            relative = str(Path(model_manifest["local_directory"]) / artifact["name"])
            path = contained_file(root, relative)
            model_checks.append({"artifact": artifact["name"], "exists": path.is_file(),
                                 "size_matches": path.is_file() and path.stat().st_size == artifact["size_bytes"],
                                 "sha256_matches": path.is_file() and sha256(path) == artifact["sha256"]})
    source = manifest["source_package"]
    package = package or (root / source["locally_extracted_relative_path"]
                          if source.get("locally_extracted_relative_path") else Path(source["reported_path"]))
    benchmark = benchmark or Path(manifest["benchmark"]["reported_path"])
    missing_source = missing_source_artifacts(package, source["required_artifacts"])
    local_preparation_ok = (all(item["sha256_matches"] for item in checks)
                            and len(model_checks) == identity["local_artifacts_verified"]
                            and all(item["sha256_matches"] and item["size_matches"] for item in model_checks))
    # Verify bytes again; a saved intake receipt is not evidence of current files.
    unresolved = list(manifest["blockers"])
    source_integrity = {"status": "not_recorded"}
    if source.get("delivery_manifest_sha256") and source.get("verified_source_digest"):
        delivery_manifest = delivery_manifest or root / source["delivery_manifest_relative_path"]
        try:
            source_integrity = verify_source_bundle(
                package, delivery_manifest,
                delivery_manifest_sha256=source["delivery_manifest_sha256"],
                package_manifest_sha256=source["verified_source_digest"],
                expected_source_files=source["reported_source_files"],
            )
        except SourceVerificationError as error:
            source_integrity = {"status": "failed", "reason": str(error)}
            unresolved.append("source_bundle_integrity_not_verified")
    if missing_source:
        unresolved.append("source_package_artifacts_missing")
    return {
        "schema_version": "secretary.local-asr.preparation-check.v1",
        "status": "blocked" if unresolved or not local_preparation_ok else "preparation_matches",
        "local_preparation_hashes_match": local_preparation_ok,
        "locks_and_code": checks,
        "identity_artifacts": model_checks,
        "source_package_directory_exists": package.is_dir(),
        "missing_source_artifacts": missing_source,
        "benchmark_directory_exists": benchmark.is_dir(),
        "canonical_package_integrity": source_integrity,
        # This command verifies source/preparation bytes. Model environment and
        # hardware evidence are separate receipts, not executed by this check.
        "fresh_model_runtime_rebuild": "not_checked_by_this_verifier",
        "model_inference": "not_executed_by_this_verifier",
        "quality": "not_evaluated",
        "live_server_deployment": "not_run",
        "unresolved_recorded_blockers": unresolved,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secretary-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--package-root", type=Path)
    parser.add_argument("--benchmark-root", type=Path)
    parser.add_argument("--delivery-manifest", type=Path)
    args = parser.parse_args()
    result = inspect(args.secretary_root.resolve(), args.package_root, args.benchmark_root,
                     args.delivery_manifest)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 2 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
