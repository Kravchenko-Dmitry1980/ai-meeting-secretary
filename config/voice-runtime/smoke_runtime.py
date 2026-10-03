"""Synthetic, offline runtime qualification. Never emit or persist voice vectors."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def child() -> None:
    # All possible framework caches remain within the ignored project runtime.
    for key, suffix in {"HF_HOME": "hf-cache", "TORCH_HOME": "torch-cache", "XDG_CACHE_HOME": "cache"}.items():
        os.environ[key] = str(ROOT / ".runtime/voice" / suffix)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    from tensor_loader import load_pinned
    torch, model, features, normalize, _ = load_pinned()
    signal = (0.08 * torch.sin(2 * torch.pi * 173 * torch.arange(32000) / 16000)).unsqueeze(0)
    with torch.inference_mode():
        vector = model(normalize(features(signal), torch.ones(1)))
    finite = bool(torch.isfinite(vector).all())
    shape = list(vector.shape)
    del vector, signal
    if not finite or shape != [1, 1, 192]:
        raise RuntimeError("Runtime did not produce a finite 192-dimensional embedding")
    print(json.dumps({"finite": finite, "shape": shape, "device": "cpu", "synthetic_seconds": 2}))


def parent(output: Path) -> None:
    import psutil
    started = time.monotonic()
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    monitored = psutil.Process(process.pid)
    tracked = {monitored.pid: monitored}
    peak = 0
    peak_process_count = 1
    timed_out = False
    memory_limit = 2 * 1024**3
    while process.poll() is None:
        try:
            descendants = monitored.children(recursive=True)
            for descendant in descendants:
                tracked[descendant.pid] = descendant
            current = 0
            for owned in [monitored, *descendants]:
                try:
                    current += owned.memory_info().rss
                except psutil.NoSuchProcess:
                    pass
            peak = max(peak, current)
            peak_process_count = max(peak_process_count, len(descendants) + 1)
        except psutil.NoSuchProcess:
            break
        if time.monotonic() - started > 120 or peak > memory_limit:
            timed_out = True
            for owned in reversed(list(tracked.values())):
                try:
                    owned.kill()
                except psutil.NoSuchProcess:
                    pass
            process.kill()
            break
        time.sleep(0.05)
    stdout, stderr = process.communicate(timeout=5)
    _, alive = psutil.wait_procs(list(tracked.values()), timeout=2)
    for owned in alive:
        try:
            owned.kill()
        except psutil.NoSuchProcess:
            pass
    if alive:
        _, alive = psutil.wait_procs(alive, timeout=2)
    stopped = process.poll() is not None and not alive
    # stderr is framework warnings only; avoid putting arbitrary tensor outputs
    # or voice data into reports even in a failed run.
    result = json.loads(stdout) if process.returncode == 0 else {}
    report = {"status": "PASS" if process.returncode == 0 and not timed_out and stopped else "FAIL",
              "exit_code": process.returncode, "child_stopped": stopped,
              "peak_owned_process_count": peak_process_count,
              "rss_scope": "launcher_and_all_observed_descendants",
              "timed_out_or_memory_limit": timed_out, "peak_rss_bytes": peak,
              "rss_limit_bytes": memory_limit, "elapsed_seconds": round(time.monotonic() - started, 3),
              "framework_warning_lines": len(stderr.splitlines()), **result}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    if report["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/speaker-implementation/task1-runtime-smoke.json")
    args = parser.parse_args()
    child() if args.child else parent(args.output)
