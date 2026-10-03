"""Measure real FFmpeg file preparation at 5 minutes and 3 hours, without cloud."""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import psutil

from secretary.application.preparation import prepare_audio
from secretary.settings import Settings


async def measure(root: Path, seconds: int, *, run_directory: Path | None = None) -> dict:
    run_directory = run_directory or root / '.runtime' / 'resource-check' / f'run-{uuid4().hex}'
    work = run_directory / str(seconds)
    work.mkdir(parents=True, exist_ok=False)
    source = work / 'synthetic-silence.flac'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=16000:cl=mono', '-t', str(seconds), '-c:a', 'flac', '-y', str(source)], check=True, timeout=300)
    settings = Settings(_env_file=None, project_dir=run_directory, data_dir=work,
                        polza_api_key='', cloud_enabled=False, chunk_seconds=120)
    process = psutil.Process()
    baseline = process.memory_info().rss
    peak = baseline
    child_peak = None
    started = time.monotonic()
    task = asyncio.create_task(prepare_audio(settings, source, work / 'chunks'))
    while not task.done():
        peak = max(peak, process.memory_info().rss)
        child_rss = 0
        for child in process.children(recursive=True):
            try:
                child_rss += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if child_rss:
            child_peak = max(child_peak or 0, child_rss)
        await asyncio.sleep(0.05)
    chunks = await task
    durations = sum(chunk['duration_ms'] for chunk in chunks)
    assert abs(durations - seconds * 1000) <= len(chunks), (seconds, durations)
    assert all(chunk['duration_ms'] <= 121000 for chunk in chunks)
    assert [chunk['offset_ms'] for chunk in chunks] == [sum(c['duration_ms'] for c in chunks[:i]) for i in range(len(chunks))]
    audio_bytes = sum(Path(chunk['path']).stat().st_size for chunk in chunks)
    growth = peak - baseline
    assert growth < 64 * 1024**2, f'Python RSS grew {growth} bytes'
    return {'duration_seconds': seconds, 'elapsed_seconds': time.monotonic() - started, 'work_directory': str(work), 'chunks': len(chunks), 'wav_bytes': audio_bytes, 'baseline_python_rss_bytes': baseline, 'sampled_peak_python_rss_bytes': peak, 'sampled_growth_python_rss_bytes': growth, 'sampled_peak_ffmpeg_children_rss_bytes': child_peak, 'sampling_interval_seconds': 0.05, 'offset_continuity': True, 'cloud_requests': 0}


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    run_directory = root / '.runtime' / 'resource-check' / f'run-{uuid4().hex}'
    run_directory.mkdir(parents=True, exist_ok=False)
    results = [await measure(root, 300, run_directory=run_directory),
               await measure(root, 10800, run_directory=run_directory)]
    path = run_directory / 'report.json'
    report = {'status': 'PASS', 'scope': 'real file decode/chunk preparation, not hardware capture or cloud STT',
              'report_path': str(path), 'measurements': results}
    path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
