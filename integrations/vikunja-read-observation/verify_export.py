"""Read-only verification of the source export and optional exact upstream base.

Never applies patches, invokes Go, imports the archived tooling, or uses network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


HERE = Path(__file__).resolve().parent


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_in_memory(original: str, diff: str) -> str:
    """Require every context/deletion line at its declared original position."""
    source = original.replace("\r\n", "\n").splitlines(keepends=True)
    lines = diff.splitlines(keepends=True)
    result: list[str] = []
    position = 0
    index = 2
    while index < len(lines):
        header = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", lines[index])
        if not header:
            raise ValueError("invalid_patch_hunk")
        start = int(header.group(1)) - 1
        if start < position or start > len(source):
            raise ValueError("invalid_patch_position")
        result.extend(source[position:start])
        position = start
        old_count = new_count = 0
        index += 1
        while index < len(lines) and not lines[index].startswith("@@ "):
            prefix, text = lines[index][:1], lines[index][1:]
            if prefix not in {" ", "+", "-"}:
                raise ValueError("invalid_patch_line")
            if prefix in {" ", "-"}:
                if position >= len(source) or source[position] != text:
                    raise ValueError("patch_context_mismatch")
                position += 1
                old_count += 1
            if prefix in {" ", "+"}:
                result.append(text)
                new_count += 1
            index += 1
        if old_count != int(header.group(2) or 1) or new_count != int(header.group(4) or 1):
            raise ValueError("patch_hunk_count_mismatch")
    result.extend(source[position:])
    return "".join(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, help="Existing local unmodified Vikunja source; read only")
    args = parser.parse_args()
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        path = HERE / entry["path"]
        if digest(path) != entry["sha256"] or path.stat().st_size != entry["bytes"]:
            raise ValueError("export_file_mismatch: " + entry["path"])
    patch = HERE / manifest["patch"]["path"]
    if digest(patch) != manifest["patch"]["sha256"]:
        raise ValueError("export_patch_mismatch")
    if digest(HERE / "LICENSE") != manifest["upstream"]["license_sha256"]:
        raise ValueError("export_license_mismatch")
    tooling = json.loads((HERE / "tooling/manifest.json").read_text(encoding="utf-8"))
    for entry in tooling["files"]:
        if digest(HERE / "tooling" / entry["path"]) != entry["original_sha256"]:
            raise ValueError("archived_tooling_mismatch: " + entry["path"])
    checked_targets = 0
    if args.upstream:
        base = args.upstream.resolve(strict=True)
        for name, key in (("go.mod", "go_mod_sha256"), ("go.sum", "go_sum_sha256")):
            if digest(base / name) != manifest["upstream"][key]:
                raise ValueError("upstream_input_mismatch: " + name)
        diffs = re.split(r"(?=^--- a/)", patch.read_text(encoding="utf-8"), flags=re.MULTILINE)
        diffs = [item for item in diffs if item]
        if len(diffs) != len(manifest["patch"]["targets"]):
            raise ValueError("patch_target_count_mismatch")
        for target, diff in zip(manifest["patch"]["targets"], diffs, strict=True):
            if not diff.startswith("--- a/" + target["path"] + "\n+++ b/" + target["path"] + "\n"):
                raise ValueError("patch_target_mismatch")
            source = base / target["path"]
            if digest(source) != target["before_sha256"]:
                raise ValueError("upstream_target_mismatch: " + target["path"])
            rendered = apply_in_memory(source.read_text(encoding="utf-8"), diff)
            if hashlib.sha256(rendered.encode("utf-8")).hexdigest() != target["normalized_after_sha256"]:
                raise ValueError("patch_postimage_mismatch: " + target["path"])
            checked_targets += 1
        for entry in manifest["files"]:
            if (base / entry["target_path"]).exists():
                raise ValueError("upstream_addition_already_exists: " + entry["target_path"])
    print(json.dumps({"status": "SOURCE_EXPORT_VERIFIED", "overlay_files": len(manifest["files"]),
                      "patch_targets_checked": checked_targets, "archived_tooling_files": len(tooling["files"]),
                      "upstream_written": False, "go_executed": False, "network_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
