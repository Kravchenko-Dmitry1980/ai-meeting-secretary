"""Private batch embedding protocol. The result must never enter ordinary logs."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from secretary.domain.voice import ModelStamp, VoiceError, valid_vector
from secretary.infrastructure.voice_process import run_owned

MAX_CLIPS = 32
MAX_CLIP_SAMPLES = 10 * 16000
MAX_BATCH_SAMPLES = 120 * 16000
MAX_INPUT_BYTES = 5200000
MAX_OUTPUT_BYTES = 200000


@dataclass(frozen=True, repr=False)
class EmbeddingBatch:
    model: ModelStamp
    vectors: tuple[tuple[float, ...], ...]
    peak_rss_bytes: int


def pinned_stamp(project_dir: Path) -> ModelStamp:
    source = project_dir / "config/voice-runtime/tensor_loader.py"
    try:
        spec = importlib.util.spec_from_file_location("secretary_voice_tensor_loader", source)
        loader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loader)
        _, manifest = loader.verify_pinned(project_dir)
        weight = next(a for a in manifest["files"] if a["name"] == "embedding_model.ckpt")
        return ModelStamp(manifest["repository"], manifest["revision"], weight["sha256"])
    except FileNotFoundError:
        raise VoiceError("runtime_missing") from None
    except (ValueError, KeyError, StopIteration, OSError, TypeError):
        raise VoiceError("runtime_unverified") from None


class LocalVoiceEngine:
    def __init__(self, project_dir: Path, *, runner=run_owned, verifier=pinned_stamp):
        self.project_dir = Path(project_dir).absolute()
        self.runner, self.verifier = runner, verifier

    def embed(self, clips: tuple[bytes, ...] | list[bytes], *, cancel=None) -> EmbeddingBatch:
        if (not isinstance(clips, (list, tuple)) or not 1 <= len(clips) <= MAX_CLIPS
                or any(not isinstance(c, bytes) or not 32000 <= len(c) <= MAX_CLIP_SAMPLES * 2
                       or len(c) % 2 for c in clips)
                or sum(len(c)//2 for c in clips) > MAX_BATCH_SAMPLES):
            raise VoiceError("invalid_engine_batch")
        python = self.project_dir / ".venv-voice/Scripts/python.exe"
        if not python.is_file():
            raise VoiceError("runtime_missing")
        model = self.verifier(self.project_dir)
        request_id = str(uuid4())
        request = {"version": 1, "request_id": request_id, "operation": "embed",
                   "sample_rate_hz": 16000, "clips": [base64.b64encode(c).decode("ascii") for c in clips]}
        payload = json.dumps(request, separators=(",", ":")).encode("ascii")
        if len(payload) > MAX_INPUT_BYTES:
            raise VoiceError("engine_input_limit")
        env = dict(os.environ)
        env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
                    "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2"})
        for key, folder in {"HF_HOME": "hf-cache", "TORCH_HOME": "torch-cache", "XDG_CACHE_HOME": "cache"}.items():
            env[key] = str(self.project_dir / ".runtime/voice" / folder)
        try:
            result = self.runner([str(python), str(self.project_dir / "scripts/voice_engine.py")],
                                 input_bytes=payload, stdout_limit=MAX_OUTPUT_BYTES,
                                 stderr_limit=65536, timeout=120, rss_limit=2*1024**3,
                                 cancel=cancel, env=env)
        except VoiceError as error:
            if error.reason in {"process_failed", "process_unavailable"}:
                raise VoiceError("runtime_incompatible") from None
            raise
        try:
            response = json.loads(result.stdout)
            if (set(response) != {"version", "request_id", "model", "vectors"}
                    or type(response["version"]) is not int or response["version"] != 1
                    or response["request_id"] != request_id or ModelStamp(**response["model"]) != model
                    or not isinstance(response["vectors"], list) or len(response["vectors"]) != len(clips)
                    or any(not valid_vector(v) for v in response["vectors"])):
                raise ValueError
            return EmbeddingBatch(model, tuple(tuple(v) for v in response["vectors"]), result.peak_rss_bytes)
        except (ValueError, TypeError, KeyError, VoiceError):
            raise VoiceError("invalid_engine_response") from None
