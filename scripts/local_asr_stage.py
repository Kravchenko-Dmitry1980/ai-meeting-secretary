"""Run the canonical RNNT/CTC CLI with an exact, hash-pinned FFmpeg executable.

The donor's arguments and audio processing stay unchanged. Its bare `ffmpeg`
subprocess call is redirected to the declared executable instead of PATH lookup.
No model or third-party module is imported before file checks complete.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import sys


PIPELINE_SHA256 = "08120d1c5c6ab6682c6b1f7affbf64f3d74319038ae56cea0e13b3f05f36e211"


def _verified_file(path: Path, expected: str):
    if (not path.is_absolute() or ".." in path.parts or str(path).startswith(("\\\\", "//"))
            or re.fullmatch(r"[a-f0-9]{64}", expected) is None):
        raise ValueError("stage_file_contract_invalid")
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("stage_file_linked")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= 512 * 1024 * 1024:
        raise ValueError("stage_file_invalid")
    before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    hasher = hashlib.sha256()
    count = 0
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        while block := stream.read(1024 * 1024):
            count += len(block)
            if count > 512 * 1024 * 1024:
                raise ValueError("stage_file_invalid")
            hasher.update(block)
        finished = os.fstat(stream.fileno())
    after = path.stat()
    signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if before != signature(opened) or before != signature(finished) or before != signature(after) or count != info.st_size:
        raise ValueError("stage_file_changed")
    if hasher.hexdigest() != expected:
        raise ValueError("stage_file_hash_mismatch")


@contextmanager
def redirect_ffmpeg(path: Path, expected_sha256: str):
    """Narrow adapter for the exact `from subprocess import run` donor call."""
    _verified_file(path, expected_sha256)
    original = subprocess.run
    def pinned_run(command, *args, **kwargs):
        if isinstance(command, (list, tuple)) and command and command[0] == "ffmpeg":
            if kwargs.get("shell"):
                raise ValueError("stage_ffmpeg_shell_forbidden")
            # Recheck at each invocation, not merely when the stage was planned.
            _verified_file(path, expected_sha256)
            command = [str(path), *command[1:]]
        return original(command, *args, **kwargs)
    subprocess.run = pinned_run
    try:
        yield
    finally:
        subprocess.run = original


def execute_pipeline(pipeline: Path, ffmpeg: Path, ffmpeg_sha256: str, arguments: list[str]):
    _verified_file(pipeline, PIPELINE_SHA256)
    # This wrapper is deliberately unavailable for alternative backends or
    # selector experiments. It changes no canonical CLI arguments.
    for flag, allowed in (("--stage", {"transcribe", "align"}), ("--backend", {"gigaam"}),
                          ("--quality-mode", {"nemotron-ctc"}), ("--asr-device", {"cpu"})):
        if arguments.count(flag) != 1:
            raise ValueError("stage_argument_contract_invalid")
        index = arguments.index(flag)
        if index + 1 >= len(arguments) or arguments[index + 1] not in allowed:
            raise ValueError("stage_argument_contract_invalid")
    saved_argv = sys.argv
    saved_path = list(sys.path)
    try:
        sys.argv = [str(pipeline), *arguments]
        sys.path.insert(0, str(pipeline.parent))
        with redirect_ffmpeg(ffmpeg, ffmpeg_sha256):
            runpy.run_path(str(pipeline), run_name="__main__")
    finally:
        sys.argv = saved_argv
        sys.path[:] = saved_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--ffmpeg-sha256", required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    remaining = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    try:
        execute_pipeline(args.pipeline, args.ffmpeg, args.ffmpeg_sha256, remaining)
    except Exception as error:
        print("LOCAL_ASR_STAGE_ERROR=" + type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
