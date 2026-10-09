"""Local-only uvicorn launcher with a Secretary-owned graceful stop marker."""
from __future__ import annotations

import argparse
import asyncio
import socket
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import uvicorn


def stop_marker(root: Path, run_id: str | None = None) -> Path:
    if run_id is None:
        return root / '.runtime' / 'stop.request'
    if str(UUID(run_id)) != run_id:
        raise ValueError('invalid_secretary_launch_id')
    return root / '.runtime' / ('stop-' + run_id + '.request')


async def run(port: int, team_runtime_config: Path | None = None, run_id: str | None = None,
              offline: bool = False, isolated_offline: bool = False) -> None:
    # Uvicorn otherwise binds after lifespan startup, which can claim jobs.
    # Own the literal loopback listener before loading settings or any DB.
    with owned_listener(port) as listener:
        await run_owned(port, listener, team_runtime_config, run_id, offline=offline,
                        isolated_offline=isolated_offline)


@contextmanager
def owned_listener(port):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('invalid_secretary_port')
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(('127.0.0.1', port))
        listener.listen(128)
        listener.setblocking(False)
        yield listener
    finally:
        listener.close()


async def run_owned(port, listener, team_runtime_config=None, run_id=None, offline=False,
                    isolated_offline=False):
    root = Path(__file__).resolve().parents[1]
    marker = stop_marker(root, run_id)
    application, factory = 'secretary.api:create_app', True
    if isolated_offline and not offline:
        raise ValueError('isolated_mode_requires_offline')
    if offline and team_runtime_config is not None:
        raise ValueError('offline_mode_not_supported_with_team_runtime')
    if offline:
        from secretary.api import create_app
        if isolated_offline:
            from secretary.settings import Settings
            application = create_app(settings=Settings(_env_file=None), outbound_enabled=False)
        else:
            application = create_app(outbound_enabled=False)
        factory = False
    elif team_runtime_config is not None:
        from secretary.infrastructure.team_process_job import install_process_job
        # The module retains the non-inheritable HANDLE until process exit.
        # Install before settings/factory/native work; failure starts no app.
        native_job = install_process_job()
        from secretary.team_settings import load_team_settings
        from secretary.orchestration.secretary_team_runtime import create_secretary_team_app
        config = load_team_settings(team_runtime_config)
        if port != config.local_secretary_port:
            raise ValueError('secretary_runtime_port_mismatch')
        application, factory = create_secretary_team_app(config, run_id=run_id, native_job=native_job), False
    server = uvicorn.Server(uvicorn.Config(application, host='127.0.0.1', port=port, factory=factory, proxy_headers=False, log_level='info', timeout_graceful_shutdown=5))

    async def watch() -> None:
        while not server.should_exit:
            if marker.exists():
                server.should_exit = True
                return
            await asyncio.sleep(0.3)

    watcher = asyncio.create_task(watch())
    try:
        await server.serve(sockets=[listener])
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--team-runtime-config', type=Path)
    parser.add_argument('--run-id')
    parser.add_argument('--offline', action='store_true', help='Disable outbound integrations for this Secretary process')
    parser.add_argument('--isolated-offline', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be between 1024 and 65535')
    if args.team_runtime_config is not None and not args.team_runtime_config.is_absolute():
        parser.error('team runtime config must be an absolute path')
    if args.offline and args.team_runtime_config is not None:
        parser.error('--offline cannot be combined with --team-runtime-config')
    if args.isolated_offline and not args.offline:
        parser.error('--isolated-offline requires --offline')
    if args.run_id is not None:
        try:
            stop_marker(Path(__file__).resolve().parents[1], args.run_id)
        except ValueError:
            parser.error('run id must be a canonical UUID')
    asyncio.run(run(args.port, args.team_runtime_config, args.run_id, offline=args.offline,
                    isolated_offline=args.isolated_offline))
