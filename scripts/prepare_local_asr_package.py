"""Build a traceable, isolated test derivative of the verified desktop package.

Only process ownership is adapted. Frozen tests, model pins and resource guards
remain unchanged. This does not create a model runtime or deploy an application.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from secretary.infrastructure.local_asr_source import verify_source_bundle


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf8")


def replace_process_function(original: bytes, replacement: bytes) -> bytes:
    source, wrapper = original.decode("utf8"), replacement.decode("utf8")
    nodes = [node for node in ast.parse(source).body
             if isinstance(node, ast.FunctionDef) and node.name == "run_stage_process"]
    patch_nodes = ast.parse(wrapper).body
    if (len(nodes) != 1 or len(patch_nodes) != 1
            or not isinstance(patch_nodes[0], ast.FunctionDef)
            or patch_nodes[0].name != "run_stage_process"):
        raise ValueError("process_patch_boundary_mismatch")
    node = nodes[0]
    lines = source.splitlines(keepends=True)
    changed = "".join(lines[:node.lineno - 1]) + wrapper.rstrip() + "\n" + "".join(lines[node.end_lineno:])
    ast.parse(changed)
    # Every unrelated top-level function/class/constant must stay identical.
    without_process = lambda tree: [ast.dump(n) for n in tree.body
                                    if not (isinstance(n, ast.FunctionDef) and n.name == "run_stage_process")]
    if without_process(ast.parse(source)) != without_process(ast.parse(changed)):
        raise ValueError("process_patch_changed_other_code")
    return changed.encode("utf8")


def prepare(destination: Path) -> dict:
    destination = destination.absolute()
    scratch = ROOT / ".runtime/team-rollout"
    if (destination.exists() or not destination.parent.resolve().is_relative_to(scratch.resolve())
            or destination.name != "package"):
        raise ValueError("new_package_inside_test_scratch_required")
    source = json.loads((ROOT / "config/local-asr/runtime-manifest.json").read_text("utf8"))["source_package"]
    original_root = ROOT / source["locally_extracted_relative_path"]
    delivery_path = ROOT / source["delivery_manifest_relative_path"]
    verified = verify_source_bundle(
        original_root, delivery_path, delivery_manifest_sha256=source["delivery_manifest_sha256"],
        package_manifest_sha256=source["verified_source_digest"],
        expected_source_files=source["reported_source_files"],
    )
    delivery = json.loads(delivery_path.read_text("utf8"))
    overlay = ROOT / "config/local-asr/package-overlay"
    helper = (overlay / "dev_asr_process.py").read_bytes()
    wrapper = (overlay / "run_stage_process.py.txt").read_bytes()
    original_pipeline = (original_root / "scripts/dev_asr_pipeline.py").read_bytes()
    pipeline = replace_process_function(original_pipeline, wrapper)
    ast.parse(helper.decode("utf8"))
    destination.mkdir(parents=True, exist_ok=False)
    copied_rows = {}
    def record(relative: str, data: bytes) -> None:
        copied_rows[relative] = {"path": relative, "size_bytes": len(data), "sha256": digest(data)}
    for entry in delivery["files"]:
        relative = entry["relative_path"]
        data = (original_root / relative).read_bytes()
        if len(data) != entry["size_bytes"] or digest(data) != entry["sha256"]:
            raise ValueError("source_changed_during_copy")
        target = destination / ("FILES_MANIFEST.original.json" if relative == "FILES_MANIFEST.json" else relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        record(target.relative_to(destination).as_posix(), data)
    (destination / "scripts/dev_asr_pipeline.py").write_bytes(pipeline)
    (destination / "scripts/dev_asr_process.py").write_bytes(helper)
    record("scripts/dev_asr_pipeline.py", pipeline)
    record("scripts/dev_asr_process.py", helper)
    manifest_path = destination / "contracts/dev-asr-package.v1.json"
    manifest = json.loads(manifest_path.read_text("utf8"))
    original_tests = json_bytes(manifest["tests"])
    matching = [row for row in manifest["files"] if row["path"] == "scripts/dev_asr_pipeline.py"]
    if len(matching) != 1:
        raise ValueError("package_pipeline_entry_missing")
    matching[0]["sha256"] = digest(pipeline)
    manifest["files"].append({"path": "scripts/dev_asr_process.py", "sha256": digest(helper)})
    manifest["adaptation"] = {
        "schema_version": "secretary.local-asr.process-adaptation.v1",
        "base_package_manifest_sha256": source["verified_source_digest"],
        "scope": "windows_owned_process_cleanup_only",
        "runtime_qualified": False,
    }
    if json_bytes(manifest["tests"]) != original_tests:
        raise ValueError("frozen_tests_changed")
    updated_manifest_bytes = json_bytes(manifest)
    manifest_path.write_bytes(updated_manifest_bytes)
    record("contracts/dev-asr-package.v1.json", updated_manifest_bytes)
    patch = "".join(difflib.unified_diff(original_pipeline.decode().splitlines(True),
                                       pipeline.decode().splitlines(True),
                                       fromfile="original/scripts/dev_asr_pipeline.py",
                                       tofile="adapted/scripts/dev_asr_pipeline.py"))
    (destination.parent / "process-adaptation.patch").write_text(patch, encoding="utf8")
    receipt = {
        "schema_version": "secretary.local-asr.test-derivative.v1",
        "base_verification": verified, "frozen_test_selection_sha256": digest(original_tests),
        "python_cases": len(manifest["tests"]["python_cases"]),
        "powershell_tests": len(manifest["tests"]["powershell_files"]),
        "modified_files": {
            "scripts/dev_asr_pipeline.py": {"before": digest(original_pipeline), "after": digest(pipeline)},
            "contracts/dev-asr-package.v1.json": {"before": source["verified_source_digest"],
                                                   "after": digest(manifest_path.read_bytes())},
        },
        "added_files": {"scripts/dev_asr_process.py": digest(helper)},
        "overlay_wrapper_sha256": digest(wrapper), "model_inference": "NOT_RUN",
        "runtime_qualified": False,
    }
    (destination.parent / "derivative-receipt.json").write_bytes(json_bytes(receipt))
    # Pin the already verified bytes while copying. Acceptance reads them again
    # after tests; do not hash the whole 3378-file library a third time here.
    rows = [copied_rows[name] for name in sorted(copied_rows)]
    (destination.parent / "derivative-files.json").write_bytes(json_bytes({
        "schema_version": "secretary.local-asr.test-derivative-files.v1", "files": rows,
    }))
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    result = prepare(args.destination)
    print(json.dumps({key: result[key] for key in ("python_cases", "powershell_tests", "runtime_qualified")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
