"""Installed architecture + hash-pinned tensor-only offline loader shared by smoke/engine."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE = {"input_size": 80, "channels": [1024, 1024, 1024, 1024, 3072],
                "kernel_sizes": [5, 3, 3, 3, 1], "dilations": [1, 2, 3, 4, 1],
                "attention_channels": 128, "lin_neurons": 192}


def verify_pinned(root: Path = ROOT) -> tuple[Path, dict]:
    manifest_path = root / "config/voice-runtime/model-manifest.json"
    if manifest_path.stat().st_size > 16384:
        raise ValueError("invalid_manifest")
    manifest = json.loads(manifest_path.read_bytes())
    if (manifest["architecture"] != ARCHITECTURE or manifest["sample_rate_hz"] != 16000
            or manifest["channels"] != 1 or manifest["embedding_dimension"] != 192
            or manifest["repository"] != "speechbrain/spkrec-ecapa-voxceleb"
            or manifest["revision"] != "0f99f2d0ebe89ac095bcc5903c4dd8f72b367286"):
        raise ValueError("incompatible_manifest")
    expected_dir = ".runtime/voice/models/spkrec-ecapa-voxceleb/" + manifest["revision"]
    if manifest["local_directory"] != expected_dir:
        raise ValueError("invalid_manifest")
    directory = root / expected_dir
    for artifact in manifest["files"]:
        if Path(artifact["name"]).name != artifact["name"]:
            raise ValueError("invalid_manifest")
        target = directory / artifact["name"]
        for path in [*target.parents, target]:
            if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
                raise ValueError("unsafe_model_path")
        if target.stat().st_size != artifact["size_bytes"]:
            raise ValueError("model_hash_failure")
        with target.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != artifact["sha256"]:
                raise ValueError("model_hash_failure")
    weight = next(a for a in manifest["files"] if a["name"] == "embedding_model.ckpt")
    if weight["sha256"] != "0575cb64845e6b9a10db9bcb74d5ac32b326b8dc90352671d345e2ee3d0126a2":
        raise ValueError("model_hash_failure")
    return directory, manifest


def load_pinned(root: Path = ROOT):
    directory, manifest = verify_pinned(root)
    import torch
    from speechbrain.lobes.features import Fbank
    from speechbrain.lobes.models.ECAPA_TDNN import ECAPA_TDNN
    from speechbrain.processing.features import InputNormalization
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    model = ECAPA_TDNN(**ARCHITECTURE).eval()
    weights = torch.load(directory / "embedding_model.ckpt", map_location="cpu", weights_only=True)
    model.load_state_dict(weights, strict=True)
    del weights
    return torch, model, Fbank(n_mels=80), InputNormalization(norm_type="sentence", std_norm=False), manifest
