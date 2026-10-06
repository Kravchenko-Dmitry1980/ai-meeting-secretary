"""Cross-process admission and fail-closed maintenance coordination.

Tickets describe actual owned lifetimes, not expiring leases. Callers must drain
their threads/HTTP attempts and persist receipts before leaving. New outbound or
capture admissions never inherit a generic pre-maintenance job permission.
"""
from __future__ import annotations

from contextlib import asynccontextmanager, closing, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import re
import sqlite3
from uuid import UUID, uuid4
from urllib.parse import quote


class MaintenanceError(ValueError):
    def __init__(self,code):
        self.code=code if isinstance(code,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,80}',code) else 'maintenance_invalid'
        super().__init__(self.code)


class MaintenanceBlocked(MaintenanceError):
    """New work was refused; no external dispatch has been authorized."""


@dataclass(frozen=True)
class MaintenanceTicket:
    id: str
    deployment_id: str
    participant_id: str
    run_id: str
    kind: str
    operation_id: str | None
    fence: int


@dataclass(frozen=True)
class BarrierClaim:
    deployment_id: str
    barrier_id: str
    owner_id: str
    fence: int


@dataclass(frozen=True)
class RecoveryCheckpoint:
    participant_id: str
    run_id: str
    ticket_ids: tuple[str,...]
    billing_operation_ids: tuple[str,...]
    source_watermarks: tuple[tuple[str,str],...]
    checkpoint_digest: str


_AMBIENT=ContextVar('team_maintenance_admissions',default=())
_HASH=re.compile(r'[0-9a-f]{64}\Z')


def _uuid(value):
    try:
        if not isinstance(value,str) or str(UUID(value))!=value:
            raise ValueError
        return value
    except (ValueError,TypeError,AttributeError):
        raise MaintenanceError('maintenance_identity_invalid') from None


def _name(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',value):
        raise MaintenanceError('maintenance_identity_invalid')
    return value


def _canonical(value):
    return json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False)


def _identity(value):
    if (not isinstance(value,dict) or set(value)!={'pid','creation_time','executable_sha256','argv_sha256'}
            or type(value['pid']) is not int or value['pid']<1
            or not isinstance(value['creation_time'],str) or not 1<=len(value['creation_time'])<=128
            or any(ord(c)<32 for c in value['creation_time'])
            or any(not isinstance(value[key],str) or not _HASH.fullmatch(value[key])
                   for key in ('executable_sha256','argv_sha256'))):
        raise MaintenanceError('maintenance_identity_invalid')
    return _canonical(value)


class MaintenanceRepository:
    def __init__(self,path,deployment_id,clock=None):
        self.path=Path(path).resolve()
        self.deployment_id=_uuid(deployment_id)
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        # Identify an existing file before WAL pragmas or write transactions.
        if self.path.exists():
            with closing(sqlite3.connect('file:'+quote(self.path.as_posix(),safe='/:')+'?mode=ro',uri=True,timeout=.2)) as probe:
                tables={row[0] for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables and 'maintenance_state' not in tables:
                    raise MaintenanceError('maintenance_database_invalid')
                if 'maintenance_state' in tables:
                    row=probe.execute('SELECT deployment_id FROM maintenance_state WHERE id=1').fetchone()
                    if row is None or row[0]!=self.deployment_id:
                        raise MaintenanceError('maintenance_deployment_mismatch')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self._transaction() as conn:
            tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and 'maintenance_state' not in tables:
                raise MaintenanceError('maintenance_database_invalid')
            if not tables:
                statements=(
                    '''CREATE TABLE maintenance_state(id INTEGER PRIMARY KEY CHECK(id=1),
                        deployment_id TEXT NOT NULL,mode TEXT NOT NULL CHECK(mode IN('open','draining','frozen','restore_blocked')),
                        fence INTEGER NOT NULL,barrier_id TEXT,owner_id TEXT,native_evidence TEXT)''',
                    '''CREATE TABLE maintenance_participants(participant_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,
                        identity TEXT NOT NULL,guard_version INTEGER NOT NULL CHECK(guard_version=1))''',
                    '''CREATE TABLE maintenance_tickets(id TEXT PRIMARY KEY,participant_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,kind TEXT NOT NULL,operation_id TEXT,fence INTEGER NOT NULL,
                        state TEXT NOT NULL CHECK(state IN('active','finished')),created_at TEXT NOT NULL)''',
                    '''CREATE TABLE maintenance_events(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,
                        barrier_id TEXT,fence INTEGER NOT NULL,created_at TEXT NOT NULL)''',
                    '''CREATE TRIGGER maintenance_ticket_immutable BEFORE UPDATE ON maintenance_tickets
                        WHEN NEW.id!=OLD.id OR NEW.participant_id!=OLD.participant_id OR NEW.run_id!=OLD.run_id
                        OR NEW.kind!=OLD.kind OR NEW.operation_id IS NOT OLD.operation_id OR NEW.fence!=OLD.fence
                        OR NEW.created_at!=OLD.created_at OR OLD.state='finished'
                        BEGIN SELECT RAISE(ABORT,'maintenance_ticket_immutable'); END''',
                    '''CREATE TRIGGER maintenance_ticket_no_delete BEFORE DELETE ON maintenance_tickets
                        BEGIN SELECT RAISE(ABORT,'maintenance_ticket_immutable'); END''',
                    '''CREATE TRIGGER maintenance_ticket_no_replace BEFORE INSERT ON maintenance_tickets
                        WHEN EXISTS(SELECT 1 FROM maintenance_tickets WHERE id=NEW.id)
                        BEGIN SELECT RAISE(ABORT,'maintenance_ticket_immutable'); END''',
                    '''CREATE TRIGGER maintenance_event_no_update BEFORE UPDATE ON maintenance_events
                        BEGIN SELECT RAISE(ABORT,'maintenance_event_immutable'); END''',
                    '''CREATE TRIGGER maintenance_event_no_delete BEFORE DELETE ON maintenance_events
                        BEGIN SELECT RAISE(ABORT,'maintenance_event_immutable'); END''',
                    '''CREATE TRIGGER maintenance_event_no_replace BEFORE INSERT ON maintenance_events
                        WHEN EXISTS(SELECT 1 FROM maintenance_events WHERE id=NEW.id)
                        BEGIN SELECT RAISE(ABORT,'maintenance_event_immutable'); END''',
                )
                for statement in statements:
                    conn.execute(statement)
                conn.execute("INSERT INTO maintenance_state VALUES(1,?,'open',0,NULL,NULL,NULL)",(self.deployment_id,))
            if conn.execute('SELECT deployment_id FROM maintenance_state WHERE id=1').fetchone()[0]!=self.deployment_id:
                raise MaintenanceError('maintenance_deployment_mismatch')
            conn.execute('''CREATE TABLE IF NOT EXISTS maintenance_recovery_checkpoints(
                id TEXT PRIMARY KEY,payload TEXT NOT NULL,recovery_ticket_id TEXT NOT NULL,
                dead_proof TEXT NOT NULL,created_at TEXT NOT NULL)''')
            for verb in ('UPDATE','DELETE'):
                conn.execute(f'''CREATE TRIGGER IF NOT EXISTS maintenance_recovery_no_{verb.lower()}
                    BEFORE {verb} ON maintenance_recovery_checkpoints
                    BEGIN SELECT RAISE(ABORT,'maintenance_recovery_immutable'); END''')
            conn.execute('''CREATE TRIGGER IF NOT EXISTS maintenance_recovery_no_replace
                BEFORE INSERT ON maintenance_recovery_checkpoints
                WHEN EXISTS(SELECT 1 FROM maintenance_recovery_checkpoints WHERE id=NEW.id)
                BEGIN SELECT RAISE(ABORT,'maintenance_recovery_immutable'); END''')
            conn.execute('CREATE TABLE IF NOT EXISTS maintenance_sources(role TEXT PRIMARY KEY,path TEXT NOT NULL UNIQUE)')
            for verb in ('UPDATE','DELETE'):
                conn.execute(f'''CREATE TRIGGER IF NOT EXISTS maintenance_source_no_{verb.lower()}
                    BEFORE {verb} ON maintenance_sources BEGIN SELECT RAISE(ABORT,'maintenance_source_immutable'); END''')
            conn.execute('''CREATE TRIGGER IF NOT EXISTS maintenance_source_no_replace BEFORE INSERT ON maintenance_sources
                WHEN EXISTS(SELECT 1 FROM maintenance_sources WHERE role=NEW.role OR path=NEW.path)
                BEGIN SELECT RAISE(ABORT,'maintenance_source_immutable'); END''')
            conn.execute('''CREATE TABLE IF NOT EXISTS maintenance_native_containment(
                participant_id TEXT NOT NULL,run_id TEXT NOT NULL,identity TEXT NOT NULL,
                containment_version INTEGER NOT NULL CHECK(containment_version=1),recorded_at TEXT NOT NULL,
                PRIMARY KEY(participant_id,run_id))''')
            for verb in ('UPDATE','DELETE'):
                conn.execute(f'''CREATE TRIGGER IF NOT EXISTS maintenance_containment_no_{verb.lower()}
                    BEFORE {verb} ON maintenance_native_containment
                    BEGIN SELECT RAISE(ABORT,'maintenance_containment_immutable'); END''')
            conn.execute('''CREATE TRIGGER IF NOT EXISTS maintenance_containment_no_replace BEFORE INSERT ON maintenance_native_containment
                WHEN EXISTS(SELECT 1 FROM maintenance_native_containment WHERE participant_id=NEW.participant_id AND run_id=NEW.run_id)
                BEGIN SELECT RAISE(ABORT,'maintenance_containment_immutable'); END''')

    def _now(self):
        value=self.clock()
        if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise MaintenanceError('maintenance_clock_invalid')
        return value.astimezone(timezone.utc).isoformat()

    @contextmanager
    def _transaction(self):
        conn=sqlite3.connect(self.path,timeout=.2,isolation_level=None)
        conn.row_factory=sqlite3.Row
        try:
            conn.execute('PRAGMA busy_timeout=200')
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def register_participant(self,participant_id,*,run_id,identity,guard_version=1):
        _name(participant_id);_uuid(run_id)
        encoded=_identity(identity)
        if type(guard_version) is not int or guard_version!=1:
            raise MaintenanceError('maintenance_guard_version_invalid')
        with self._transaction() as conn:
            row=conn.execute('SELECT * FROM maintenance_participants WHERE participant_id=?',(participant_id,)).fetchone()
            if row and (row['run_id'],row['identity'],row['guard_version'])==(run_id,encoded,guard_version):
                return
            if self._status(conn)['mode']!='open':
                raise MaintenanceBlocked('maintenance_blocked')
            if conn.execute("SELECT 1 FROM maintenance_tickets WHERE participant_id=? AND state='active'",(participant_id,)).fetchone():
                raise MaintenanceError('maintenance_active_operations')
            conn.execute('''INSERT INTO maintenance_participants VALUES(?,?,?,?) ON CONFLICT(participant_id)
                DO UPDATE SET run_id=excluded.run_id,identity=excluded.identity,guard_version=excluded.guard_version''',
                (participant_id,run_id,encoded,guard_version))

    def bind_sources(self,sources,*,validate_new=None):
        """Bind the deployment to four stores without opening any source file."""
        roles={'secretary','team','billing','vikunja'}
        if not isinstance(sources,dict) or set(sources)!=roles:
            raise MaintenanceError('maintenance_sources_invalid')
        normalized={}
        for role,value in sources.items():
            if not isinstance(value,(str,Path)) or not Path(value).is_absolute() or '..' in Path(value).parts:
                raise MaintenanceError('maintenance_sources_invalid')
            normalized[role]=os.path.normcase(os.path.abspath(value))
        if len(set(normalized.values()))!=4:raise MaintenanceError('maintenance_sources_invalid')
        with self._transaction() as conn:
            existing=dict(conn.execute('SELECT role,path FROM maintenance_sources'))
            if existing:
                if existing!=normalized:raise MaintenanceError('maintenance_sources_mismatch')
                if validate_new is not None:validate_new(dict(normalized))
                return
            state=self._status(conn)
            if state['mode']!='open':raise MaintenanceBlocked('maintenance_blocked')
            if conn.execute('SELECT 1 FROM maintenance_tickets LIMIT 1').fetchone():
                raise MaintenanceError('maintenance_sources_unverified')
            for role,path in sorted(normalized.items()):conn.execute('INSERT INTO maintenance_sources VALUES(?,?)',(role,path))
            if validate_new is not None:validate_new(dict(normalized))

    def bound_sources(self):
        with self._transaction() as conn:return dict(conn.execute('SELECT role,path FROM maintenance_sources'))

    def register_native_containment(self,participant_id,native_job):
        """Register kernel-verified containment before this run starts any work."""
        from .team_process_job import WindowsJob,OwnedProcessJobProof,ProcessJobError
        _name(participant_id)
        if type(native_job) is not WindowsJob:raise MaintenanceError('maintenance_containment_unverified')
        with self._transaction() as conn:
            if self._status(conn)['mode']!='open':raise MaintenanceBlocked('maintenance_blocked')
            row=conn.execute('SELECT * FROM maintenance_participants WHERE participant_id=?',(participant_id,)).fetchone()
            if not row or conn.execute('SELECT count(*) FROM maintenance_sources').fetchone()[0]!=4:
                raise MaintenanceError('maintenance_containment_unverified')
            try:proof=WindowsJob.verify_owned_identity(native_job)
            except (ProcessJobError,ValueError,AttributeError,OSError):
                raise MaintenanceError('maintenance_containment_unverified') from None
            if type(proof) is not OwnedProcessJobProof or proof.pid!=os.getpid() or _identity(asdict(proof))!=row['identity']:
                raise MaintenanceError('maintenance_containment_identity_mismatch')
            if self._contained(conn,participant_id,row['run_id'],row['identity']):return
            if conn.execute('SELECT 1 FROM maintenance_tickets WHERE participant_id=? AND run_id=? LIMIT 1',(participant_id,row['run_id'])).fetchone():
                raise MaintenanceError('maintenance_containment_late')
            conn.execute('INSERT INTO maintenance_native_containment VALUES(?,?,?,1,?)',
                         (participant_id,row['run_id'],row['identity'],self._now()))

    def _contained(self,conn,participant_id,run_id,identity):
        row=conn.execute('''SELECT identity,containment_version FROM maintenance_native_containment
            WHERE participant_id=? AND run_id=?''',(participant_id,run_id)).fetchone()
        return bool(row and row['identity']==identity and row['containment_version']==1)

    def has_native_containment(self,participant_id,run_id):
        _name(participant_id);_uuid(run_id)
        with self._transaction() as conn:
            row=conn.execute('SELECT run_id,identity FROM maintenance_participants WHERE participant_id=?',(participant_id,)).fetchone()
            return bool(row and row['run_id']==run_id and self._contained(conn,participant_id,run_id,row['identity']))

    def participant_descriptor(self,participant_id,*,role):
        _name(participant_id)
        if role not in ('secretary','gateway'):raise MaintenanceError('maintenance_descriptor_invalid')
        with self._transaction() as conn:
            sources=dict(conn.execute('SELECT role,path FROM maintenance_sources'))
            row=conn.execute('SELECT * FROM maintenance_participants WHERE participant_id=?',(participant_id,)).fetchone()
            if len(sources)!=4 or not row:raise MaintenanceError('maintenance_descriptor_unverified')
            return {'schema_version':1,'deployment_id':self.deployment_id,'role':role,
                    'participant_id':participant_id,'run_id':row['run_id'],'identity':json.loads(row['identity']),
                    'containment_version':1 if self._contained(conn,participant_id,row['run_id'],row['identity']) else 0,
                    'sources_sha256':hashlib.sha256(_canonical(sources).encode()).hexdigest()}

    def enter(self,participant_id,kind,operation_id=None):
        _name(participant_id);_name(kind)
        if operation_id is not None:
            _uuid(operation_id)
        with self._transaction() as conn:
            state=self._status(conn)
            if state['mode']!='open':
                raise MaintenanceBlocked('maintenance_blocked')
            member=conn.execute('SELECT run_id FROM maintenance_participants WHERE participant_id=?',(participant_id,)).fetchone()
            if not member:
                raise MaintenanceError('maintenance_participant_unknown')
            ticket=MaintenanceTicket(str(uuid4()),self.deployment_id,participant_id,member[0],kind,operation_id,state['fence'])
            conn.execute("INSERT INTO maintenance_tickets VALUES(?,?,?,?,?,?,'active',?)",
                         (ticket.id,ticket.participant_id,ticket.run_id,ticket.kind,ticket.operation_id,ticket.fence,self._now()))
            return ticket

    def _ticket(self,conn,ticket):
        if not isinstance(ticket,MaintenanceTicket) or ticket.deployment_id!=self.deployment_id:
            raise MaintenanceError('maintenance_ticket_invalid')
        row=conn.execute('SELECT * FROM maintenance_tickets WHERE id=?',(ticket.id,)).fetchone()
        if not row or self._ticket_value(row)!=ticket:
            raise MaintenanceError('maintenance_ticket_invalid')
        return row

    def _ticket_value(self,row):
        return MaintenanceTicket(row['id'],self.deployment_id,row['participant_id'],row['run_id'],row['kind'],row['operation_id'],row['fence'])

    def leave(self,ticket):
        with self._transaction() as conn:
            row=self._ticket(conn,ticket)
            if row['state']=='active':
                conn.execute("UPDATE maintenance_tickets SET state='finished' WHERE id=?",(ticket.id,))

    @contextmanager
    def admission(self,participant_id,kind,operation_id=None):
        key=(str(self.path),self.deployment_id,participant_id)
        ambient=next((ticket for stored,ticket in reversed(_AMBIENT.get()) if stored==key),None)
        if ambient is not None and not (kind.startswith('outbound') or kind.startswith('capture')):
            with self._transaction() as conn:
                if self._ticket(conn,ambient)['state']!='active':
                    raise MaintenanceError('maintenance_ticket_finished')
            yield ambient
            return
        ticket=self.enter(participant_id,kind,operation_id)
        token=_AMBIENT.set((*_AMBIENT.get(),(key,ticket)))
        try:
            yield ticket
        finally:
            _AMBIENT.reset(token)
            self.leave(ticket)

    @asynccontextmanager
    async def async_admission(self,participant_id,kind,operation_id=None):
        # Control transactions are bounded to 200ms; no awaited DB/native thread
        # can become detached between admission and context ownership.
        with self.admission(participant_id,kind,operation_id) as ticket:
            yield ticket

    @contextmanager
    def ticket_context(self,ticket):
        """Transfer an existing admission into its owned executor thread.

        This does not admit new work or finish the ticket; the submitter owns
        leave() only after its native/thread lifetime has actually ended.
        """
        with self._transaction() as conn:
            row=self._ticket(conn,ticket)
            participant=conn.execute('SELECT run_id FROM maintenance_participants WHERE participant_id=?',(ticket.participant_id,)).fetchone()
            if row['state']!='active' or not participant or participant[0]!=ticket.run_id:
                raise MaintenanceError('maintenance_ticket_finished')
        key=(str(self.path),self.deployment_id,ticket.participant_id)
        token=_AMBIENT.set((*_AMBIENT.get(),(key,ticket)))
        try:yield ticket
        finally:_AMBIENT.reset(token)

    def begin_barrier(self,*,owner_id):
        _name(owner_id)
        with self._transaction() as conn:
            state=self._status(conn)
            if state['mode']!='open':
                raise MaintenanceBlocked('maintenance_blocked')
            if state['active_capture']:
                raise MaintenanceError('maintenance_capture_active')
            claim=BarrierClaim(self.deployment_id,str(uuid4()),owner_id,state['fence']+1)
            conn.execute("UPDATE maintenance_state SET mode='draining',fence=?,barrier_id=?,owner_id=?,native_evidence=NULL WHERE id=1",
                         (claim.fence,claim.barrier_id,claim.owner_id))
            self._event(conn,'begin',claim)
            return claim

    def _held(self,conn,claim):
        state=self._status(conn)
        if (not isinstance(claim,BarrierClaim) or claim.deployment_id!=self.deployment_id
                or state['mode'] not in {'draining','frozen'}
                or (state['barrier_id'],state['owner_id'],state['fence'])!=(claim.barrier_id,claim.owner_id,claim.fence)):
            raise MaintenanceError('maintenance_barrier_lost')
        return state

    def active_tickets(self,claim):
        with self._transaction() as conn:
            self._held(conn,claim)
            return tuple(self._ticket_value(row) for row in conn.execute("SELECT * FROM maintenance_tickets WHERE state='active' ORDER BY rowid"))

    def active_operations(self,participant_id=None):
        """Read-only recovery inventory; this never expires or retires a ticket.

        The supervisor must independently verify an exited predecessor and save
        uncertain receipts before explicitly leaving those exact tickets.
        """
        if participant_id is not None:_name(participant_id)
        with self._transaction() as conn:
            sql="SELECT * FROM maintenance_tickets WHERE state='active'"
            args=()
            if participant_id is not None:sql+=' AND participant_id=?';args=(participant_id,)
            return tuple(self._ticket_value(row) for row in conn.execute(sql+' ORDER BY rowid',args))

    def retire_dead_participant(self,checkpoint,*,proof):
        """Trusted recovery orchestration only, after its durable source commits.

        The source checkpoint digest is evidence of the caller's scoped recovery,
        never authority to change another live process's charges or tickets.
        """
        from .team_process_identity import DeadProcessProof,verify_dead_identity
        if not isinstance(checkpoint,RecoveryCheckpoint) or not isinstance(proof,DeadProcessProof):
            raise MaintenanceError('maintenance_recovery_invalid')
        _name(checkpoint.participant_id);_uuid(checkpoint.run_id)
        if (type(checkpoint.ticket_ids) is not tuple or not checkpoint.ticket_ids or len(checkpoint.ticket_ids)>10000
                or len(set(checkpoint.ticket_ids))!=len(checkpoint.ticket_ids)
                or type(checkpoint.billing_operation_ids) is not tuple or len(set(checkpoint.billing_operation_ids))!=len(checkpoint.billing_operation_ids)
                or type(checkpoint.source_watermarks) is not tuple or not checkpoint.source_watermarks or len(checkpoint.source_watermarks)>16
                or not isinstance(checkpoint.checkpoint_digest,str) or not _HASH.fullmatch(checkpoint.checkpoint_digest)):
            raise MaintenanceError('maintenance_recovery_invalid')
        for value in (*checkpoint.ticket_ids,*checkpoint.billing_operation_ids):_uuid(value)
        for item in checkpoint.source_watermarks:
            if not isinstance(item,tuple) or len(item)!=2 or not isinstance(item[1],str) or not 1<=len(item[1])<=128 or any(ord(c)<32 for c in item[1]):
                raise MaintenanceError('maintenance_recovery_invalid')
            _name(item[0])
        payload=_canonical(asdict(checkpoint));identity=hashlib.sha256(payload.encode()).hexdigest()
        ambient=[ticket for key,ticket in _AMBIENT.get() if key[:2]==(str(self.path),self.deployment_id)
                 and ticket.kind=='crash_recovery' and ticket.participant_id!=checkpoint.participant_id]
        if not ambient:raise MaintenanceError('maintenance_recovery_admission_required')
        with self._transaction() as conn:
            if self._status(conn)['mode']!='open':raise MaintenanceBlocked('maintenance_blocked')
            if conn.execute('SELECT count(*) FROM maintenance_sources').fetchone()[0]!=4:
                raise MaintenanceError('maintenance_sources_unverified')
            recovery=ambient[-1]
            if self._ticket(conn,recovery)['state']!='active':raise MaintenanceError('maintenance_recovery_admission_required')
            old=conn.execute('SELECT * FROM maintenance_participants WHERE participant_id=?',(checkpoint.participant_id,)).fetchone()
            if not old or old['run_id']!=checkpoint.run_id:raise MaintenanceError('maintenance_recovery_identity_mismatch')
            expected=json.loads(old['identity'])
            if {key:getattr(proof,key) for key in expected}!=expected or proof.state not in ('exited','pid_reused'):
                raise MaintenanceError('maintenance_recovery_identity_mismatch')
            try:fresh=verify_dead_identity(expected)
            except ValueError:raise MaintenanceError('maintenance_recovery_process_unverified') from None
            prior=conn.execute('SELECT payload FROM maintenance_recovery_checkpoints WHERE id=?',(identity,)).fetchone()
            if prior:
                if prior[0]!=payload:raise MaintenanceError('maintenance_recovery_conflict')
                return
            rows=conn.execute("SELECT * FROM maintenance_tickets WHERE participant_id=? AND run_id=? AND state='active'",(checkpoint.participant_id,checkpoint.run_id)).fetchall()
            if {r['id'] for r in rows}!=set(checkpoint.ticket_ids):raise MaintenanceError('maintenance_recovery_ticket_mismatch')
            billing={r['operation_id'] for r in rows if r['kind'].startswith('outbound_polza')}
            if None in billing or billing!=set(checkpoint.billing_operation_ids):raise MaintenanceError('maintenance_recovery_billing_mismatch')
            conn.execute('INSERT INTO maintenance_recovery_checkpoints VALUES(?,?,?,?,?)',(identity,payload,recovery.id,_canonical(asdict(fresh)),self._now()))
            for ticket_id in checkpoint.ticket_ids:
                conn.execute("UPDATE maintenance_tickets SET state='finished' WHERE id=? AND state='active'",(ticket_id,))

    def freeze(self,claim,native_evidence):
        with self._transaction() as conn:
            state=self._held(conn,claim)
            if state['active_tickets']:
                raise MaintenanceError('maintenance_active_operations')
            proof=asdict(native_evidence) if is_dataclass(native_evidence) else native_evidence
            required={'deployment_id','run_id','component','barrier_id','barrier_fence','pid','creation_time',
                      'executable_sha256','argv_hash','observed_stopped_at'}
            if (not isinstance(proof,dict) or set(proof)!=required
                    or (proof['deployment_id'],proof['component'],proof['barrier_id'],proof['barrier_fence'])
                       !=(self.deployment_id,'vikunja',claim.barrier_id,claim.fence)):
                raise MaintenanceError('maintenance_native_unverified')
            _uuid(proof['run_id'])
            _identity({'pid':proof['pid'],'creation_time':proof['creation_time'],
                       'executable_sha256':proof['executable_sha256'],'argv_sha256':proof['argv_hash']})
            try:
                stamp=datetime.fromisoformat(proof['observed_stopped_at'].replace('Z','+00:00'))
                if stamp.tzinfo is None or stamp.utcoffset() is None:
                    raise ValueError
            except (ValueError,TypeError,AttributeError):
                raise MaintenanceError('maintenance_native_unverified') from None
            encoded=_canonical(proof)
            if state['mode']=='frozen':
                if _canonical(state['native_evidence'])!=encoded:
                    raise MaintenanceError('maintenance_native_unverified')
                return state
            conn.execute("UPDATE maintenance_state SET mode='frozen',native_evidence=? WHERE id=1",(encoded,))
            self._event(conn,'freeze',claim)
            return self._status(conn)

    def assert_held(self,claim):
        with self._transaction() as conn:
            return self._held(conn,claim)

    def release(self,claim):
        with self._transaction() as conn:
            self._held(conn,claim)
            conn.execute("UPDATE maintenance_state SET mode='open',barrier_id=NULL,owner_id=NULL,native_evidence=NULL WHERE id=1")
            self._event(conn,'release',claim)

    def _event(self,conn,kind,claim):
        conn.execute('INSERT INTO maintenance_events(kind,barrier_id,fence,created_at) VALUES(?,?,?,?)',
                     (kind,claim.barrier_id,claim.fence,self._now()))

    def _status(self,conn):
        result=dict(conn.execute('SELECT * FROM maintenance_state WHERE id=1').fetchone())
        result.pop('id')
        if result['deployment_id']!=self.deployment_id:
            raise MaintenanceError('maintenance_deployment_mismatch')
        result['native_evidence']=json.loads(result['native_evidence']) if result['native_evidence'] else None
        result['active_tickets']=conn.execute("SELECT count(*) FROM maintenance_tickets WHERE state='active'").fetchone()[0]
        result['active_capture']=conn.execute("SELECT count(*) FROM maintenance_tickets WHERE state='active' AND kind LIKE 'capture%' ").fetchone()[0]
        result['participants']=tuple({'participant_id':row['participant_id'],'run_id':row['run_id'],
            'identity':json.loads(row['identity']),'guard_version':row['guard_version']}
            for row in conn.execute('SELECT * FROM maintenance_participants ORDER BY participant_id'))
        return result

    def status(self):
        with self._transaction() as conn:
            return self._status(conn)
