"""Immutable per-version STT route; credentials and live controls never captured."""
import hashlib
import json
from secretary.infrastructure.database import now
from secretary.infrastructure.speaker_repository import canonical


class RouteConflict(ValueError):
    pass


class TranscriptRoutes:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def configured(self, mode):
        model = 'aiesa/transcribe' if mode == 'voice_identification' else self.settings.stt_model
        price = self.settings.stt_price_rub_per_minute if model == self.settings.stt_model else None
        return {'provider': 'polza', 'adapter_revision': 'polza-stt-v1',
                'settings': {'stt_model': model, 'stt_price_rub_per_minute': price}}

    def bind(self, meeting_id, *, explicit=False, new_version=False):
        with self.db.transaction() as conn:
            meeting = conn.execute('SELECT * FROM meetings WHERE id=?', (meeting_id,)).fetchone()
            if meeting is None:
                raise KeyError(meeting_id)
            version = max(1, meeting['transcript_version'])
            if new_version:
                if meeting['recording'] or conn.execute("SELECT 1 FROM jobs WHERE meeting_id=? AND status='running' AND stage IN ('transcribe','summarize')", (meeting_id,)).fetchone():
                    raise RouteConflict('route_change_requires_stopped_processing')
                version += 1
            row = conn.execute('SELECT * FROM meeting_transcript_routes WHERE meeting_id=? AND transcript_version=?', (meeting_id, version)).fetchone()
            if row:
                return version, json.loads(row['route'])
            saved = conn.execute("SELECT * FROM jobs WHERE meeting_id=? AND version=? AND stage='transcribe' ORDER BY created_at,id", (meeting_id, version)).fetchall()
            segments = conn.execute('SELECT 1 FROM segments WHERE meeting_id=? AND transcript_version=? LIMIT 1', (meeting_id, version)).fetchone()
            route = self.configured(meeting['processing_mode'])
            if saved or segments:
                if not explicit:
                    raise RouteConflict('legacy_route_needs_review')
                known, missing = [], False
                for job in saved:
                    try:
                        settings = json.loads(job['payload'] or '{}').get('settings', {})
                        if not isinstance(settings.get('stt_model'), str) or not settings['stt_model'].strip():
                            missing = True
                            continue
                        known.append({'provider': 'polza', 'adapter_revision': 'polza-stt-v1', 'settings': {
                            'stt_model': settings['stt_model'], 'stt_price_rub_per_minute': settings.get('stt_price_rub_per_minute')}})
                    except (ValueError, TypeError, AttributeError):
                        missing = True
                protected = bool(segments) or any(j['attempts'] or j['provider_job_id'] or j['status'] in ('uncertain','succeeded') for j in saved)
                if (missing and protected) or (not known and protected) or len({r['settings']['stt_model'] for r in known}) > 1:
                    raise RouteConflict('legacy_route_needs_review')
                if known:
                    route = known[0]
            body = canonical(route)
            conn.execute('INSERT INTO meeting_transcript_routes VALUES(?,?,?,?,?,?)', (
                meeting_id, version, meeting['processing_mode'], body, hashlib.sha256(body.encode()).hexdigest(), now()))
            conn.execute('UPDATE meetings SET transcript_version=?,updated_at=? WHERE id=?', (version, now(), meeting_id))
            if new_version:
                conn.execute("UPDATE jobs SET status='cancelled',cancel_requested=1,error='Superseded by explicit new transcript version' WHERE meeting_id=? AND version!=? AND status IN ('queued','waiting_config','paused_budget') AND provider_job_id IS NULL", (meeting_id, version))
                conn.execute("UPDATE jobs SET cancel_requested=1 WHERE meeting_id=? AND version!=? AND stage='identify_speakers' AND status='running'", (meeting_id,version))
            return version, route

    def get(self, meeting_id, version):
        row = self.db.one('SELECT * FROM meeting_transcript_routes WHERE meeting_id=? AND transcript_version=?', (meeting_id, version))
        if not row:
            raise RouteConflict('legacy_route_needs_review')
        return json.loads(row['route'])
