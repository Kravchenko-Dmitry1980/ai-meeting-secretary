# Isolated Windows model environments, 2026-10-27

Scope: local RNNT + CTC CPU runtime and separate Nemotron CUDA runtime. These
environments do not replace the application `.venv` or `.venv-voice`. Creating
them does not qualify model accuracy or SERVER deployment.

| Runtime | Python | Framework | Loader |
| --- | --- | --- | --- |
| `asr-cpu` | 3.11.14 | torch/torchaudio 2.8.0+cpu | transformers 4.57.1 |
| `nemotron` | 3.12.10 | torch 2.8.0+cu128 | verified exported transformers 5.18.0.dev0 |

Runtime root: `.runtime/model-runtime-20261027`. Managed Python, uv cache,
temporary downloads, build copies, build tools, wheels and evidence remain
under that root on D:. Python was installed with `--no-bin --no-registry`.
All runtime packages use `--link-mode copy` and `--require-hashes`.

uv initially generated Windows launchers that embedded a mutable minor-version
Python junction. Only the new environments were converted to standard CPython
venv launchers using each exact interpreter's
`python -B -m venv --without-pip --upgrade <new-env>`. This preserves installed
packages and makes `pyvenv.cfg home` and actual `sys.base_prefix` use the exact
version directory. The original CPU inventory is retained as superseded;
the final model environment receipts use the `inventory-v2.json` suffix.
The existing application launcher remains unchanged and is explicitly a
`historical_snapshot_only` receipt, with `uv.lock` as `source_reference_only`.

## Dependency decisions

Inputs are minimal direct dependencies; constraints retain the donor generation.
The CPU environment includes `av` for `dev_asr_pipeline.normalize_audio`, not
only imports needed by the model. Faster Whisper, ONNX Runtime, CUDA RNNT/CTC,
training, notebook and test dependencies are outside this environment scope.

The CPU donor freeze omitted `hf-xet`, a mandatory Windows dependency declared
by its pinned `huggingface-hub==0.36.2`. The supplemental constraint selects
`hf-xet==1.6.0`, which is present in the same donor's Nemotron snapshot. This is
an explicit dependency completion, not evidence that the CPU donor had it.
All model execution must remain offline.

The Nemotron numerical/audio stack follows the frozen snapshot. Librosa is
required by `NemotronAsrStreamingFeatureExtractor`; its dependency closure
includes NumPy, SciPy, Numba/llvmlite, soundfile, soxr and scikit-learn. TorchAudio
and TorchVision are not required by the provider's local NumPy WAV route.

## Source builds and provenance

Only two pure Python wheels are built locally:

- `antlr4-python3-runtime==4.9.3`, from the official PyPI sdist whose published
  SHA-256 is checked before extraction. Its setup script is declarative.
- `transformers==5.18.0.dev0`, from a byte-identical COPY of the immutable
  runtime export at source commit `89b6b17574892ec0770551537a3fe69d6886703e`.
  This is the verified runtime export, not a full upstream checkout; the
  original exclusion of `testing_utils.py` is preserved.

`prepare_build_sources.py` records source hashes without executing source code.
The isolated build environment is installed from `build.lock` with
`setuptools==78.1.0` and `wheel==0.45.1`. Both builds run offline using
`uv build --wheel --no-build-isolation --python <build-tools-python>` with
`SOURCE_DATE_EPOCH=1793017200`. The immutable payload is never a build target.
The resulting wheel hashes are in the runtime locks and environment evidence.

## Reproduction commands

The following commands describe installation into the already-created isolated
environments. Use the absolute uv executable; set `UV_CACHE_DIR`, `TEMP`, `TMP`,
and `UV_PYTHON_INSTALL_DIR` to the runtime root's `uv-cache`, `tmp`, and `python`
directories before any command. Do not run these against application environments.

```powershell
$uv = 'C:\Users\user\AppData\Local\hermes\bin\uv.exe'
$runtime = 'D:\AI\Projects\Active\Secretary\.runtime\model-runtime-20261027'
& $uv --no-config pip sync --python "$runtime\asr-cpu\Scripts\python.exe" --torch-backend cpu --require-hashes --only-binary :all: --link-mode copy config/local-asr/model-runtime/environments/asr-cpu.lock
& $uv --no-config pip sync --python "$runtime\nemotron\Scripts\python.exe" --torch-backend cu128 --require-hashes --only-binary :all: --link-mode copy config/local-asr/model-runtime/environments/nemotron.lock
& $uv --no-config pip check --python "$runtime\asr-cpu\Scripts\python.exe"
& $uv --no-config pip check --python "$runtime\nemotron\Scripts\python.exe"
```

The backend flags are required: PyTorch source comments in requirements locks
are provenance, not executable index configuration. Windows `--find-links`
uses a `file:///D:/...` URI to avoid backslash parsing by requirements readers.

Official wheel matrix: https://pytorch.org/get-started/previous-versions/ confirms
PyTorch 2.8.0 CPU and CUDA 12.8 wheels for Windows.

## Verification and execution boundary

`probe_environment.py --kind <asr-cpu|nemotron> --output <new-receipt>` checks
real imports and exact versions. Nemotron additionally checks CUDA device
properties and memory; it does not load model weights or execute inference.

`capture_inventory.py --root <environment> --lock <lock> --output <new-receipt>`
records the complete deterministic environment file set, all base-Python files,
interpreter hashes, distribution versions and the requirements-lock hash.
Receipts are create-only and must remain outside the scanned roots. Verification
must compare exact file sets, file sizes and hashes, including unexpected files.

Before the pilot, verify the receipt itself against its separately pinned hash.
Execute with `-B` and `PYTHONDONTWRITEBYTECODE=1`; keep `NUMBA_CACHE_DIR`,
`HF_HOME`, and other runtime caches in per-job D: scratch outside the environment.
The inventory includes existing `.pyc` files, because `-B` still permits reading
them. `PASS_IMPORTS_ONLY` is distinct from model inference or quality acceptance.

Evidence is stored in `.runtime/model-runtime-20261027/evidence/`: installation
logs, source manifests, `*-pip-check.log`, `*-probe.json`, `*.inventory.json`,
and the environment provenance receipt. No Git operation is part of this work.
