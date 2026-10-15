"""Local-only uvicorn launcher with a Secretary-owned graceful stop marker."""
from __future__ import annotations

import argparse
import asyncio
import logging.handlers
import socket
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import uvicorn

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3


def resolve_log_directory(root: Path, requested: Path | None = None) -> Path:
    """Keep runtime logs inside the project's private runtime directory."""
    runtime_root = (Path(root).resolve() / '.runtime').resolve()
    candidate = Path(requested).resolve() if requested is not None else runtime_root / 'logs'
    if candidate == runtime_root or not candidate.is_relative_to(runtime_root):
        raise ValueError('log_directory_outside_runtime')
    return candidate


def build_log_config(log_directory: Path, *, max_bytes: int = LOG_MAX_BYTES,
                     backup_count: int = LOG_BACKUP_COUNT) -> dict:
    """Bound Uvicorn error/access logs by size and retained generations."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError('invalid_log_max_bytes')
    if type(backup_count) is not int or backup_count < 1:
        raise ValueError('invalid_log_backup_count')
    directory = Path(log_directory)
    formatter = {'format': '%(asctime)s %(levelname)s %(name)s %(message)s'}

    def rotating_handler(filename: str) -> dict:
        return {
            'class': 'logging.handlers.RotatingFileHandler',
            'formatter': 'default',
            'filename': str(directory / filename),
            'maxBytes': max_bytes,
            'backupCount': backup_count,
            'encoding': 'utf-8',
        }

    return {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {'default': formatter},
        'handlers': {
            'error': rotating_handler('secretary.error.log'),
            'access': rotating_handler('secretary.access.log'),
        },
        'loggers': {
            'uvicorn': {'handlers': ['error'], 'level': 'INFO', 'propagate': False},
            'uvicorn.error': {'level': 'INFO'},
            'uvicorn.access': {'handlers': ['access'], 'level': 'INFO', 'propagate': False},
        },
        'root': {'handlers': ['error'], 'level': 'INFO'},
    }


def stop_marker(root: Path, run_id: str | None = None, runtime_root: Path | None = None) -> Path:
    runtime = root / '.runtime'
    if runtime_root is not None:
        from local_supervisor import runtime_path
        runtime = runtime_path(runtime_root)
    if run_id is None:
        return runtime / 'stop.request'
    if str(UUID(run_id)) != run_id:
        raise ValueError('invalid_secretary_launch_id')
    return runtime / ('stop-' + run_id + '.request')


async def run(port: int, team_runtime_config: Path | None = None, run_id: str | None = None,
              offline: bool = False, isolated_offline: bool = False,
              log_directory: Path | None = None, runtime_root: Path | None = None,
              supervised_local: bool = False) -> None:
    if supervised_local:
        if not offline or not isolated_offline or team_runtime_config is not None:
            raise ValueError('supervised_local_requires_isolated_offline')
        from secretary.infrastructure.team_process_job import install_process_job
        install_process_job().verify_owned_identity()
    # Uvicorn otherwise binds after lifespan startup, which can claim jobs.
    # Own the literal loopback listener before loading settings or any DB.
    with owned_listener(port) as listener:
        await run_owned(port, listener, team_runtime_config, run_id, offline=offline,
                        isolated_offline=isolated_offline, log_directory=log_directory,
                        runtime_root=runtime_root)


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
                    isolated_offline=False, log_directory=None, runtime_root=None):
    root = Path(__file__).resolve().parents[1]
    marker = stop_marker(root, run_id, runtime_root)
    application, factory = 'secretary.api:create_app', True
    if isolated_offline and not offline:
        raise ValueError('isolated_mode_requires_offline')
    if offline and team_runtime_config is not None:
        raise ValueError('offline_mode_not_supported_with_team_runtime')
    log_directory = resolve_log_directory(root, log_directory)
    log_directory.mkdir(parents=True, exist_ok=True)
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
    server = uvicorn.Server(uvicorn.Config(application, host='127.0.0.1', port=port,
        factory=factory, proxy_headers=False, log_level='info',
        log_config=build_log_config(log_directory), timeout_graceful_shutdown=5))

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
    parser.add_argument('--log-directory', type=Path)
    parser.add_argument('--runtime-root', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--supervised-local', action='store_true', help=argparse.SUPPRESS)
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
    if args.log_directory is not None and not args.log_directory.is_absolute():
        parser.error('log directory must be an absolute path')
    if args.run_id is not None:
        try:
            stop_marker(Path(__file__).resolve().parents[1], args.run_id)
        except ValueError:
            parser.error('run id must be a canonical UUID')
    asyncio.run(run(args.port, args.team_runtime_config, args.run_id, offline=args.offline,
                    isolated_offline=args.isolated_offline, log_directory=args.log_directory,
                    runtime_root=args.runtime_root, supervised_local=args.supervised_local))
