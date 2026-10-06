"""Keep ASGI admission through response bodies and background cleanup."""
import asyncio

from secretary.infrastructure.team_maintenance import MaintenanceBlocked
from secretary.orchestration.team_worker import TeamWorker


class MaintenanceMiddleware:
    def __init__(self, app, *, maintenance, participant_id):
        self.app, self.maintenance, self.participant_id = app, maintenance, participant_id

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or self.maintenance is None:
            return await self.app(scope, receive, send)
        started = False

        async def tracked(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
            await send(message)

        try:
            async with self.maintenance.async_admission(self.participant_id, 'intake'):
                # The disconnected caller must not release admission while an
                # endpoint's owned native/SQLite thread is still completing.
                await TeamWorker._drain(asyncio.create_task(self.app(scope, receive, tracked)), asyncio.Event())
        except MaintenanceBlocked:
            if started:
                raise
            await send({'type': 'http.response.start', 'status': 503, 'headers': [
                (b'content-type', b'application/json'), (b'cache-control', b'no-store'), (b'retry-after', b'5')]})
            await send({'type': 'http.response.body', 'body': b'{"detail":"maintenance_blocked"}'})
