# Optional local speaker embedding runtime

From the project root, run `powershell -NoProfile -File scripts/setup_voice.ps1 -DownloadModel`.
This installs only `.venv-voice` using the hash-pinned `requirements.lock`; the main
`.venv` and `uv.lock` are unchanged. A pre-existing project Python 3.12 and `uv`
are required. No Python/global tool/driver installation is attempted.

The setup places dependency/model caches under ignored `.runtime/voice`, restores
temporary process environment variables on exit, and downloads only the six
reviewed artifacts in `model-manifest.json`. `prepare_model.py` without
`--download` performs offline hash verification. The model's example audio files
are excluded. Missing files or checksum mismatches fail before inference.

Run `.venv-voice/Scripts/python.exe config/voice-runtime/smoke_runtime.py` for a
synthetic offline CPU smoke. It starts one child, loads tensor-only model weights
into the installed SpeechBrain architecture, and checks shape `[1, 1, 192]`,
finite values, a 2 GiB sampled RSS bound over the launcher and all observed
descendants, and process-tree termination within 120 seconds.
Only aggregate diagnostics are written under `.runtime`; neither the synthetic
waveform nor the vector is printed or saved. The loader does not execute
HyperPyYAML constructors, `custom.py` or remote Python.

`polza-capabilities.json` records public catalogue/documentation evidence and
one authorized synthetic Aiesa live transport/response-format smoke. That
single live check confirms segment timing and anonymous labels on synthetic
speech. Billing, real-room quality and speaker identity remain unqualified.
Recognition accuracy and thresholds require consented participant recordings;
synthetic runtime success cannot substitute for that acceptance.
