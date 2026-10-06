"""Explicit native Gateway entry point. Imports never load config/start services."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import socket
import sys

from secretary.team_settings import RuntimeConfigurationError, load_team_settings


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Argument errors must not echo an accidentally supplied credential.
        self.exit(2, 'team_runtime_arguments_invalid\n')


async def run_gateway(settings, *, run_id, control_dir):
    from secretary.interface.team_gateway import gateway_server_config
    from secretary.orchestration.team_runtime import TeamRuntime
    from secretary.orchestration.team_worker import TeamWorker
    from secretary.infrastructure.team_process_job import install_process_job
    import uvicorn
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    runtime = stop = None
    try:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # Own the exact loopback endpoint before stores, recovery or providers.
        listener.bind(('127.0.0.1', settings.gateway_port))
        listener.listen(128)
        listener.setblocking(False)
        native_job = install_process_job()
        runtime = TeamRuntime(settings, run_id=run_id, control_dir=control_dir, native_job=native_job)
        config = gateway_server_config(runtime.app, host='127.0.0.1', port=settings.gateway_port)
        config.log_config = None
        config.log_level = 'warning'
        server = uvicorn.Server(config)
        async def requested_stop():
            await runtime.stop_requested.wait()
            server.should_exit = True
        stop = asyncio.create_task(requested_stop(), name='team-stop-request')
        await server.serve(sockets=[listener])
        return 0 if server.started else 2
    finally:
        async def finalize():
            try:
                if stop is not None:
                    stop.cancel()
                    await asyncio.gather(stop, return_exceptions=True)
                if runtime is not None:
                    await runtime.close()
            finally:
                listener.close()
        await TeamWorker._drain(asyncio.create_task(finalize(), name='team-gateway-finalize'), asyncio.Event())


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--control-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        settings = load_team_settings(args.config)
        return asyncio.run(run_gateway(settings, run_id=args.run_id, control_dir=args.control_dir))
    except KeyboardInterrupt:
        return 0
    except Exception:
        print(json.dumps({'status': 'refused', 'error_code': 'team_runtime_unavailable'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
