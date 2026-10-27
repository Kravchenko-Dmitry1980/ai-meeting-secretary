"""Assemble already-produced package artifacts; never instantiate model classes.

This standalone worker uses only the standard library and the hash-verified
donor bridge's pure composition function. It starts no HTTP service.
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_MANIFEST_SHA256 = "33e0a9a59d6278e9b2363e822b2ce7f875c3252b8d333270ffa139a3c45d7109"
MAX_RESULT_BYTES = 8 * 1024 * 1024


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _plain(path: Path):
    _require(path.is_absolute() and ".." not in path.parts
             and not str(path).startswith(("\\\\", "//")), "assemble_path_invalid")
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        _require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400,
                 "assemble_path_linked")
    return info


def _load(path: Path):
    info = _plain(path)
    _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_RESULT_BYTES,
             "assemble_input_invalid")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "assemble_duplicate_key")
            result[key] = value
        return result
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("assemble_nonfinite")))


def assemble(source_root: Path, delivery_manifest: Path, delivery_sha256: str,
             workspace: Path, input_sha256: str) -> Path:
    _require(re.fullmatch(r"[a-f0-9]{64}", input_sha256) is not None, "assemble_input_hash_invalid")
    _require(stat.S_ISDIR(_plain(workspace).st_mode), "assemble_workspace_invalid")
    _require(not workspace.is_relative_to(source_root) and not source_root.is_relative_to(workspace),
             "assemble_workspace_overlaps_source")
    _require(os.environ.get("NEUROHR_ASR_EXPERIMENTAL_GROUP_GAP_SECONDS", "1.5") == "1.5"
             and os.environ.get("NEUROHR_ASR_EXPERIMENTAL_NEMOTRON_TURN_END_PAD_MS", "0") == "0",
             "assemble_profile_override")
    sys.path.insert(0, str(ROOT / "backend"))
    from secretary.infrastructure.local_asr_source import verify_source_bundle
    verify_source_bundle(source_root, delivery_manifest, delivery_manifest_sha256=delivery_sha256,
                         package_manifest_sha256=PACKAGE_MANIFEST_SHA256, expected_source_files=65)
    transcript = _load(workspace / "align.json")
    turns = _load(workspace / "diarize.json")
    normalization = _load(workspace / "normalize.json")
    _require(isinstance(transcript, dict) and transcript.get("provider") == "gigaam-v3-e2e-rnnt+gigaam-ctc"
             and transcript.get("diarization_provider") == "nemotron-3-diarization", "assemble_provider_invalid")
    duration = normalization.get("media_duration_seconds") if isinstance(normalization, dict) else None
    _require(type(duration) in (int, float) and math.isfinite(duration) and 0 < duration <= 14400,
             "assemble_duration_invalid")
    _require(isinstance(turns, list) and 0 < len(turns) <= 500000, "assemble_turns_invalid")
    for turn in turns:
        _require(isinstance(turn, list) and len(turn) == 3
                 and all(type(value) in (int, float) and math.isfinite(value) for value in turn[:2])
                 and 0 <= turn[0] < turn[1] <= duration
                 and isinstance(turn[2], str) and 0 < len(turn[2]) <= 200,
                 "assemble_turns_invalid")
    # A fresh worker must not reuse donor names previously imported from an
    # unrelated checkout. This also makes accidental in-process usage fail closed.
    for name in ("local_asr_bridge", "local_speaker_diarizer", "dev_meeting_asr_stages"):
        _require(name not in sys.modules, "assemble_module_already_loaded")
    sys.path.insert(0, str(source_root / "scripts"))
    bridge = importlib.import_module("local_asr_bridge")
    diarizer = importlib.import_module("local_speaker_diarizer")
    payload = bridge.build_response(transcript, input_sha256, [diarizer.DiarizationTurn(*turn) for turn in turns])
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    _require(len(encoded) <= MAX_RESULT_BYTES, "assemble_result_too_large")
    target = workspace / "result.json"
    with target.open("xb") as output:
        output.write(encoded)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-root", "delivery-manifest", "workspace"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--delivery-sha256", required=True)
    parser.add_argument("--input-sha256", required=True)
    args = parser.parse_args()
    try:
        assemble(args.source_root, args.delivery_manifest, args.delivery_sha256, args.workspace, args.input_sha256)
    except Exception as error:
        # Never emit transcript, paths, native exceptions, or a full traceback.
        print("LOCAL_ASR_ASSEMBLE_ERROR=" + type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
