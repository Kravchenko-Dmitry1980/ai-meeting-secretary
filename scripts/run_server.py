"""Local-only uvicorn launcher with a Secretary-owned graceful stop marker."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import uvicorn


async def run(port: int) -> None:
    root = Path(__file__).resolve().parents[1]
    marker = root / '.runtime' / 'stop.request'
    server = uvicorn.Server(uvicorn.Config('secretary.api:create_app', host='127.0.0.1', port=port, factory=True, log_level='info', timeout_graceful_shutdown=5))

    async def watch() -> None:
        while not server.should_exit:
            if marker.exists():
                server.should_exit = True
                return
            await asyncio.sleep(0.3)

    watcher = asyncio.create_task(watch())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be between 1024 and 65535')
    asyncio.run(run(args.port))
