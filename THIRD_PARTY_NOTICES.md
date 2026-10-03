# Third-party notices and provenance

Secretary V1, reviewed 2026-10-01. This file records actual selective reuse and installed dependencies; it does not assign an open-source license to Secretary or qualify all future distribution formats.

## Owner-authorized source reuse

Selected React presentation files originated from **Kravchenko-Dmitry1980/ai-meeting-secretary**, commit `0268c2bd4cebfbba98e0001b5c75bd70b3e52cd9`, https://github.com/Kravchenko-Dmitry1980/ai-meeting-secretary. The owner expressly authorized reuse in this task. The repository has no final open-source LICENSE; README states that final licensing is absent. Do not relicense those files as MIT/Apache on that basis. Original selected files contained no per-file copyright headers; none were removed. New-to-original mappings and adaptations are in [frontend/REUSE.md](frontend/REUSE.md).

No source files from Meetily, meetscribe, Minutes or Amazon LMA were directly copied. Their respective MIT/GPL/file-specific notices do not become a license for this application. No Meetily Pro components, donor binary models, local credentials or environment files are included.

## Installed runtime dependencies

Python dependencies are fixed in `uv.lock`; npm versions/integrity are fixed in `frontend/package-lock.json`. The full actual Python distribution inventory and npm lock inventory, including transitive/dev/optional entries and metadata limitations, is [docs/licenses/inventory.json](docs/licenses/inventory.json). Shipped license/notice texts are preserved under `docs/licenses/python/` and `docs/licenses/npm/`; installed packages retain their original notices too.

| Dependency | Installed version | Declared license |
|---|---|---|
| FastAPI | 0.142.2 | MIT |
| Starlette | 1.7.0 | BSD-3-Clause |
| Pydantic / pydantic-core | 2.13.5 / 2.46.5 | MIT |
| pydantic-settings | 2.15.0 | MIT |
| Uvicorn | 0.54.0 | BSD-3-Clause |
| httpx / httpcore | 0.28.1 / 1.0.9 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| python-docx | 1.2.0 | MIT |
| lxml | 6.1.3 | BSD-3-Clause; bundled libraries have their own notices |
| python-dotenv | 1.2.4 | BSD-3-Clause |
| PyAudioWPatch | 0.2.12.8 | Apache-2.0, Copyright (c) 2022 S0D3S |
| React / React DOM | 19.2.5 | MIT |
| clsx | 2.1.1 | MIT |
| tailwind-merge | 3.5.0 | MIT |
| lucide-react | 1.11.0 | ISC |

Development tools include pytest (MIT), pytest-asyncio (Apache-2.0), psutil (BSD-3-Clause), Vite/Tailwind/ESLint/openapi-typescript (MIT) and TypeScript (Apache-2.0). Certifi's Mozilla certificate bundle is MPL-2.0. Packages with missing metadata are explicitly marked in the JSON inventory; no unknown license is silently converted to MIT.

Python 3.12 is a pre-existing interpreter governed by PSF terms; its standard SQLite library incorporates public-domain SQLite. This project uses the installed **external** FFmpeg/FFprobe executable, version 9.0 Gyan full build, observed with `--enable-gpl --enable-version3`; that build and included codecs/libraries have their own GPL/other terms. FFmpeg executables are not copied or distributed with this source. Redis/PostgreSQL/Celery/CUDA/faster-whisper/pyannote/OpenAI SDK are not dependencies of V1.

The Windows audio wheel includes PortAudio-based native code whose original license must accompany any binary distribution; see the preserved [upstream PortAudio notice](docs/licenses/portaudio/LICENSE.txt) and provenance in [WINDOWS_AUDIO.md](docs/WINDOWS_AUDIO.md) when packaging. Models and cloud services have separate terms: a public Polza catalog or SDK license is not a license to model weights, recordings, retention or account billing. No local model weights are bundled.

To refresh the inventory after dependency changes, run the local interpreter on `scripts/license_inventory.py`; review changed metadata and preserved notices before redistribution.

## Optional local speaker embedding runtime (2026-10-02)

The separate, project-local `.venv-voice` installs 35 distributions from the hash-pinned [voice requirements](config/voice-runtime/requirements.lock); it does not modify the main `uv.lock`. The installed inventory, declared metadata and missing-metadata markers are recorded in [voice license inventory](config/voice-runtime/license-inventory.json). Original shipped license/notice files (67 files) are retained under `config/voice-runtime/licenses/` and in the installed distributions. Metadata alone does not qualify every binary distribution format; review bundled native-library notices before packaging.

| Component | Pinned version / revision | Declared license / provenance |
|---|---|---|
| SpeechBrain | 1.1.1, stable PyPI wheel | Apache-2.0, https://pypi.org/project/speechbrain/1.1.1/ |
| PyTorch | 2.8.0+cpu, official Windows CPython 3.12 wheel | BSD-3-Clause; bundled component terms in original LICENSE, https://pytorch.org/get-started/previous-versions/ |
| torchaudio | 2.8.0+cpu, matching official CPU wheel | BSD-2-Clause; original shipped notices retained |
| Hugging Face Hub | 0.36.2 | Apache-2.0 |
| speechbrain/spkrec-ecapa-voxceleb | `0f99f2d0ebe89ac095bcc5903c4dd8f72b367286` | Apache-2.0 declared by the pinned [model card](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb/tree/0f99f2d0ebe89ac095bcc5903c4dd8f72b367286); original README retained with local artifacts |

The six selected model artifacts total 88,988,850 bytes and are SHA-256 pinned in [model manifest](config/voice-runtime/model-manifest.json). Weights remain under ignored `.runtime/voice/models`; no weights, example recordings or participant data are bundled in source. The inference smoke uses installed classes and `torch.load(weights_only=True)`, without model-repository Python, arbitrary `develop` code, training, TTS or cloning. Model-card benchmark results and licensing do not establish recognition quality for this room or consent for participant recordings.
