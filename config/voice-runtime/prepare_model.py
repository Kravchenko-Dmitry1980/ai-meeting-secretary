"""Explicitly download or verify only hash-pinned, reviewed model artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _verified(target: Path, artifact: dict) -> bool:
    if not target.is_file() or target.stat().st_size != artifact["size_bytes"]:
        return False
    with target.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest() == artifact["sha256"]


def verify_model(*, download: bool = False) -> Path:
    manifest = json.loads((Path(__file__).parent / "model-manifest.json").read_text(encoding="utf-8"))
    directory = ROOT / manifest["local_directory"]
    directory.mkdir(parents=True, exist_ok=True)
    for artifact in manifest["files"]:
        target = directory / artifact["name"]
        if _verified(target, artifact):
            continue
        if not download:
            raise RuntimeError(f"Missing or unverified model artifact: {artifact['name']}; run setup_voice.ps1 -DownloadModel")
        partial = target.with_suffix(target.suffix + ".download")
        digest = hashlib.sha256()
        received = 0
        try:
            with urllib.request.urlopen(artifact["url"], timeout=120) as response, partial.open("wb") as output:
                while block := response.read(min(1024 * 1024, artifact["size_bytes"] - received + 1)):
                    received += len(block)
                    if received > artifact["size_bytes"]:
                        raise RuntimeError(f"Model artifact exceeds pinned size: {artifact['name']}")
                    digest.update(block)
                    output.write(block)
            if partial.stat().st_size != artifact["size_bytes"] or digest.hexdigest() != artifact["sha256"]:
                raise RuntimeError(f"Model artifact checksum mismatch: {artifact['name']}")
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
    return directory


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    print(verify_model(download=args.download))
