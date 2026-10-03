"""Pinned model preparation rejects substituted and oversized artifacts."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest


@pytest.fixture
def preparation(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "config/voice-runtime/prepare_model.py"
    spec = importlib.util.spec_from_file_location("voice_model_preparation", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(module, "__file__", str(config / "prepare_model.py"))
    (config / "model-manifest.json").write_text(json.dumps({
        "local_directory": "private-model", "files": [{
            "name": "embedding_model.ckpt", "size_bytes": 4,
            "sha256": hashlib.sha256(b"good").hexdigest(),
            "url": "https://synthetic.invalid/pinned-model"}]}), encoding="utf-8")
    return module, tmp_path / "private-model/embedding_model.ckpt"


@pytest.mark.parametrize("payload,error", [(b"evil", "checksum mismatch"), (b"good-extra", "exceeds pinned size")])
def test_download_rejects_hash_or_size_and_cleans_partial(preparation, monkeypatch, payload, error):
    module, target = preparation
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(payload))
    with pytest.raises(RuntimeError, match=error):
        module.verify_model(download=True)
    assert not target.exists()
    assert not target.with_suffix(".ckpt.download").exists()


def test_offline_verification_never_downloads_missing_artifact(preparation, monkeypatch):
    module, target = preparation
    def denied(*args, **kwargs):
        raise AssertionError("Offline verification made a network request")
    monkeypatch.setattr(module.urllib.request, "urlopen", denied)
    with pytest.raises(RuntimeError, match="Missing or unverified"):
        module.verify_model()
    target.write_bytes(b"good")
    assert module.verify_model() == target.parent
