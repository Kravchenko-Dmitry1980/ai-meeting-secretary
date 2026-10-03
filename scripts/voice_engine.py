"""Single private offline batch. stdout is the bounded worker protocol, not a log."""
from __future__ import annotations

import array
import base64
import json
import os
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "config/voice-runtime"))


def main():
    raw = sys.stdin.buffer.read(5200001)
    if len(raw) > 5200000:
        raise ValueError
    request = json.loads(raw)
    if (set(request) != {"version", "request_id", "operation", "sample_rate_hz", "clips"}
            or type(request["version"]) is not int or request["version"] != 1 or request["operation"] != "embed"
            or type(request["sample_rate_hz"]) is not int or request["sample_rate_hz"] != 16000
            or str(UUID(request["request_id"])) != request["request_id"]
            or not isinstance(request["clips"], list) or not 1 <= len(request["clips"]) <= 32):
        raise ValueError
    clips = []
    total = 0
    for encoded in request["clips"]:
        if not isinstance(encoded, str) or len(encoded) > 426668:
            raise ValueError
        pcm = base64.b64decode(encoded, validate=True)
        if not 32000 <= len(pcm) <= 320000 or len(pcm) % 2:
            raise ValueError
        total += len(pcm)//2
        if total > 1920000:
            raise ValueError
        clips.append(pcm)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    from tensor_loader import load_pinned
    torch, model, features, normalize, manifest = load_pinned()
    vectors = []
    with torch.inference_mode():
        for pcm in clips:
            values = array.array("h")
            values.frombytes(pcm)
            signal = torch.tensor(values, dtype=torch.float32).div_(32768).unsqueeze(0)
            vector = model(normalize(features(signal), torch.ones(1))).reshape(-1)
            if vector.numel() != 192 or not bool(torch.isfinite(vector).all()) or float(vector.norm()) <= 1e-10:
                raise ValueError
            vectors.append(vector.tolist())
    weight = next(a for a in manifest["files"] if a["name"] == "embedding_model.ckpt")
    response = {"version": 1, "request_id": request["request_id"],
                "model": {"model_id": manifest["repository"], "revision": manifest["revision"],
                          "weights_sha256": weight["sha256"]}, "vectors": vectors}
    output = json.dumps(response, separators=(",", ":"), allow_nan=False).encode("ascii")
    if len(output) > 200000:
        raise ValueError
    sys.stdout.buffer.write(output)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # No traceback, tensor dump, path or input material escapes stderr.
        sys.stderr.write("voice_runtime_failure\n")
        raise SystemExit(2)
