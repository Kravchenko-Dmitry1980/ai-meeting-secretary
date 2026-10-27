"""Offline import/version checks. Never loads weights or performs inference."""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--kind', choices=('asr-cpu', 'nemotron'), required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    scratch = Path(__file__).resolve().parents[4] / '.runtime/model-runtime-20261027/tmp/probe'
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ['NUMBA_CACHE_DIR'] = str(scratch / 'numba')
    os.environ['HF_HOME'] = str(scratch / 'hf')
    import numpy
    import torch
    import transformers
    result = {'schema_version': 1, 'kind': args.kind, 'python': platform.python_version(),
              'torch': torch.__version__, 'numpy': numpy.__version__,
              'transformers': transformers.__version__, 'cuda_build': torch.version.cuda,
              'weights_loaded': False, 'inference_executed': False}
    if args.kind == 'asr-cpu':
        import hydra
        import sentencepiece
        import torchaudio
        import av
        assert platform.python_version() == '3.11.14'
        assert torch.__version__ == '2.8.0+cpu'
        assert torchaudio.__version__ == '2.8.0+cpu'
        assert transformers.__version__ == '4.57.1'
        assert not torch.cuda.is_available()
        source = Path(__file__).resolve().parents[4] / '.runtime/source-handoff-20261026-150225/payload/model-metadata/rnnt'
        sys.path.insert(0, str(source))
        from modeling_gigaam import GigaAMModel
        result.update(torchaudio=torchaudio.__version__, av=av.__version__, gigaam_import=True,
                      cuda_available=False)
    else:
        import librosa
        import numba
        import scipy
        import sklearn
        import soundfile
        import soxr
        from transformers import AutoModelForAudioFrameClassification, AutoProcessor
        from transformers.models.nemotron3_diarization.modeling_nemotron3_diarization import Nemotron3DiarizationForAudioFrameClassification
        from transformers.models.nemotron3_diarization.processing_nemotron3_diarization import Nemotron3DiarizationProcessor
        from transformers.models.nemotron_asr_streaming.feature_extraction_nemotron_asr_streaming import NemotronAsrStreamingFeatureExtractor
        assert platform.python_version() == '3.12.10'
        assert torch.__version__ == '2.8.0+cu128'
        assert transformers.__version__ == '5.18.0.dev0'
        assert torch.cuda.is_available()
        props = torch.cuda.get_device_properties(0)
        free, total = torch.cuda.mem_get_info(0)
        result.update(librosa=librosa.__version__, numba=numba.__version__,
                      scipy=scipy.__version__, sklearn=sklearn.__version__,
                      soundfile=soundfile.__version__, soxr=soxr.__version__,
                      nemotron_import=True, cuda_available=True,
                      cuda_device={'name': props.name, 'uuid': str(getattr(props, 'uuid', '')),
                                   'total_memory': props.total_memory,
                                   'free_bytes': free, 'total_bytes': total})
    result['status'] = 'PASS_IMPORTS_ONLY'
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))

if __name__ == '__main__':
    main()
