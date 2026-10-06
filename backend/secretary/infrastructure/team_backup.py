"""Four-store maintenance snapshots and fresh, outbound-blocked restoration.

No settings, credentials, default database paths or native processes are opened
on import. The lifecycle port must be the deployment's owned supervisor.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import time
from urllib.parse import quote
from uuid import uuid4

from .team_maintenance import MaintenanceError

ROLES=('secretary','team','billing','vikunja')
MAX_MANIFEST=32*1024*1024
RESTORE_GUARD_DDL='''CREATE TABLE IF NOT EXISTS maintenance_restore_guard(
 id INTEGER PRIMARY KEY CHECK(id=1),restore_id TEXT NOT NULL,
 reconciliation_required INTEGER NOT NULL CHECK(reconciliation_required=1),manifest_sha256 TEXT NOT NULL)'''


class BackupError(ValueError):
    def __init__(self,code):
        self.code=code if isinstance(code,str) and re.fullmatch('[a-z][a-z0-9_]{0,80}',code) else 'backup_failed'
        super().__init__(self.code)


@dataclass(frozen=True)
class BackupResult:
    state: str
    destination: Path
    code: str | None = None
    full_recovery: bool = False
    native_containment_verified: bool = False


@dataclass(frozen=True)
class RestoreResult:
    state: str
    destination: Path
    databases: dict
    asset_roots: dict
    restore_id: str
    outbound_enabled: bool = False


def _json(value):
    return json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False)


def _safe(value,*,file=False):
    raw=Path(value)
    if not raw.is_absolute() or '..' in raw.parts:
        raise BackupError('backup_path_invalid')
    path=Path(os.path.abspath(raw))
    for part in (path,*path.parents):
        try:
            info=part.lstat()
        except FileNotFoundError:continue
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise BackupError('backup_reparse_forbidden')
    if file:
        info=path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
            raise BackupError('backup_file_invalid')
    return path


def _fresh(value,root):
    root=_safe(root)
    path=_safe(value)
    if path==root or not path.is_relative_to(root) or path.exists():
        raise BackupError('backup_destination_invalid')
    return path


def _read(path):
    path=_safe(path,file=True)
    if path.stat().st_size>MAX_MANIFEST:
        raise BackupError('backup_manifest_invalid')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError,UnicodeError):
        raise BackupError('backup_manifest_invalid') from None


def _write(path,value):
    payload=_json(value).encode('utf-8')
    if len(payload)>MAX_MANIFEST:
        raise BackupError('backup_manifest_too_large')
    with path.open('xb') as stream:
        stream.write(payload);stream.flush();os.fsync(stream.fileno())


def _hash(path):
    digest=hashlib.sha256()
    with _safe(path,file=True).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


def _inactive_source(path):
    # immutable SQLite ignores journals. It is safe for inspection only after
    # refusing them, and callers must recheck after collecting their snapshot.
    for suffix in ('-wal','-shm','-journal'):
        try: Path(str(path)+suffix).lstat()
        except FileNotFoundError: continue
        raise BackupError('restore_preview_active_source')


def _ro(path,*,immutable=False):
    if immutable:
        _safe(path,file=True);_inactive_source(path)
    return sqlite3.connect('file:'+quote(Path(path).as_posix(),safe='/:')+'?mode=ro'+('&immutable=1' if immutable else ''),uri=True,timeout=.2)


def _check(conn):
    if conn.execute('PRAGMA integrity_check').fetchall()!=[('ok',)] or conn.execute('PRAGMA foreign_key_check').fetchone():
        raise BackupError('backup_integrity_failed')


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _schema(conn):
    # No rows containing provider payloads, messages or tokens enter the manifest.
    ddl=conn.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name").fetchall()
    return {'user_version':conn.execute('PRAGMA user_version').fetchone()[0],
            'schema_sha256':hashlib.sha256(_json(ddl).encode()).hexdigest(),
            'row_counts':{name:conn.execute('SELECT count(*) FROM "'+name.replace('"','""')+'"').fetchone()[0]
                          for name in sorted(_tables(conn)) if not name.startswith('sqlite_')}}


def _copy(source,target,deadline):
    if time.monotonic()>deadline:raise BackupError('backup_deadline')
    before=_safe(source,file=True).stat()
    target.parent.mkdir(parents=True,exist_ok=True)
    digest=hashlib.sha256();size=0
    with source.open('rb') as incoming,target.open('xb') as outgoing:
        opened=os.fstat(incoming.fileno())
        if (opened.st_dev,opened.st_ino)!=(before.st_dev,before.st_ino) or opened.st_nlink!=1:
            raise BackupError('backup_asset_changed')
        for chunk in iter(lambda:incoming.read(1024*1024),b''):
            if time.monotonic()>deadline:
                raise BackupError('backup_deadline')
            outgoing.write(chunk);digest.update(chunk);size+=len(chunk)
        outgoing.flush();os.fsync(outgoing.fileno())
    after=_safe(source,file=True).stat()
    if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
        raise BackupError('backup_asset_changed')
    return {'sha256':digest.hexdigest(),'bytes':size}


def _asset_references(conn,roots):
    """Known physical references, including immutable identification archives."""
    tables=_tables(conn);refs=[];issues=[]
    if 'meetings' in tables:
        for (value,) in conn.execute('SELECT media_path FROM meetings WHERE media_path IS NOT NULL'):
            if value.startswith('uploading:'):
                issues.append('asset_upload_incomplete')
            else: refs.append(value)
        if conn.execute('SELECT 1 FROM meetings WHERE recording=1').fetchone():
            issues.append('asset_capture_unmanaged')
    if 'chunks' in tables:
        refs.extend(r[0] for r in conn.execute('SELECT path FROM chunks'))
    if 'identification_intents' in tables:
        for (raw,) in conn.execute('SELECT snapshot FROM identification_intents'):
            try:
                refs.extend(item['path'] for item in json.loads(raw).get('chunks',[]) if item.get('path'))
            except (ValueError,KeyError,TypeError,AttributeError):
                issues.append('asset_reference_invalid')
    data=roots.get('secretary_data')
    if 'voice_enrollments' in tables and data:
        columns={row[1] for row in conn.execute('PRAGMA table_info(voice_enrollments)')}
        if {'person_profile_id','storage_key'}<=columns:
            for profile,generation in conn.execute("SELECT person_profile_id,storage_key FROM voice_enrollments WHERE storage_key IS NOT NULL AND storage_key!=''"):
                refs.append(str(data/'voice'/'profiles'/profile/(generation+'.dpapi')))
        if 'private_material' in columns and conn.execute("SELECT 1 FROM voice_enrollments WHERE private_material IS NOT NULL AND private_material!=''").fetchone():
            issues.append('asset_legacy_material_review')
    return refs,issues


def _capture_paths(conn,data):
    tables=_tables(conn);paths=[]
    if 'meetings' in tables:
        paths.extend(data/'audio'/row[0]/'capture.json' for row in conn.execute('SELECT id FROM meetings'))
    if 'enrollment_recordings' in tables:
        paths.extend(data/'voice'/'staging'/row[0]/'capture.json' for row in conn.execute('SELECT id FROM enrollment_recordings'))
    return paths


def validate_source_roles(sources,*,allow_missing=False):
    """Read-only role anchors after a tentative/committed deployment binding.

    Optional missing stores are allowed for first startup but never created here.
    Existing foreign or empty databases fail closed before a factory migration.
    """
    if not isinstance(sources,dict) or set(sources)!=set(ROLES):raise BackupError('backup_four_sources_required')
    anchors={'secretary':{'meetings','chunks','jobs'},'team':{'team_schema','team_commands'},
             'billing':{'billing_schema','billing_charges'},'vikunja':{'files'}}
    for role,source in sources.items():
        path=_safe(source)
        if allow_missing and not path.exists():continue
        _safe(path,file=True)
        with closing(_ro(path)) as conn:
            if not anchors[role]<=_tables(conn):raise BackupError('backup_source_role_invalid')


class BackupService:
    def __init__(self,maintenance,sources,*,backup_root,asset_roots,lifecycle,manifest_path,
                 required_participants=None,participant_descriptors=None,require_native_containment=None,
                 config_versions=None,native_storage=None,clock=None):
        if set(sources)!=set(ROLES):raise BackupError('backup_four_sources_required')
        self.sources={role:_safe(sources[role],file=True) for role in ROLES}
        if len(set(self.sources.values()))!=4:raise BackupError('backup_sources_overlap')
        maintenance.bind_sources(self.sources,validate_new=validate_source_roles)
        self.maintenance=maintenance;self.backup_root=_safe(backup_root)
        self.roots={key:_safe(path) for key,path in asset_roots.items()}
        if any(not re.fullmatch('[a-z][a-z0-9_]{0,63}',key) or not path.is_dir() for key,path in self.roots.items()):
            raise BackupError('backup_asset_root_invalid')
        if any(a!=b and (a.is_relative_to(b) or b.is_relative_to(a)) for a in self.roots.values() for b in self.roots.values()):
            raise BackupError('backup_asset_roots_overlap')
        if len(set(self.roots.values()))!=len(self.roots):raise BackupError('backup_asset_roots_overlap')
        if 'secretary_data' in self.roots and self.roots['secretary_data']!=self.sources['secretary'].parent:
            raise BackupError('backup_secretary_root_invalid')
        self.lifecycle=lifecycle;self.manifest_path=_safe(manifest_path,file=True)
        if (required_participants is None)==(participant_descriptors is None):raise BackupError('backup_participants_required')
        self.descriptors=None
        if require_native_containment is not None and type(require_native_containment) is not bool:
            raise BackupError('backup_containment_policy_invalid')
        self.require_native_containment=participant_descriptors is not None if require_native_containment is None else require_native_containment
        if participant_descriptors is not None:
            if not isinstance(participant_descriptors,dict) or set(participant_descriptors)!={'secretary','gateway'}:
                raise BackupError('backup_participants_required')
            self.descriptors={role:_safe(path) for role,path in participant_descriptors.items()}
            self.required=()
        else:
            if not isinstance(required_participants,(tuple,list)):raise BackupError('backup_participants_required')
            self.required=tuple(required_participants)
            if not self.required or len(set(self.required))!=len(self.required):raise BackupError('backup_participants_required')
        self.versions=config_versions or {};self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.native_storage=native_storage
        if not isinstance(self.versions,dict) or any(not isinstance(k,str) or not re.fullmatch('[a-zA-Z0-9_.-]{1,80}',k) or not isinstance(v,str) or not re.fullmatch('[a-zA-Z0-9_.+-]{1,128}',v) for k,v in self.versions.items()):
            raise BackupError('backup_versions_invalid')

    def backup(self,destination,*,owner_id='backup',timeout_seconds=120):
        target=_fresh(destination,self.backup_root)
        if type(timeout_seconds) not in (int,float) or not 1<=timeout_seconds<=600:raise BackupError('backup_deadline_invalid')
        if any(target.is_relative_to(path) or path.is_relative_to(target) for path in self.roots.values()):
            raise BackupError('backup_destination_overlaps_assets')
        state=self.maintenance.status()
        participants={p['participant_id']:p for p in state['participants'] if p['guard_version']==1}
        required=self.required
        if self.descriptors is not None:
            identities=[]
            for role,path in self.descriptors.items():
                try:
                    descriptor=_read(path)
                    participant=descriptor['participant_id']
                    expected=self.maintenance.participant_descriptor(participant,role=role)
                    if _json(descriptor)!=_json(expected):raise BackupError('backup_descriptor_unverified')
                    identities.append(participant)
                except (OSError,ValueError,KeyError,TypeError):
                    return BackupResult('deferred',target,'backup_descriptor_unverified')
            required=tuple(identities)
            if len(set(required))!=2:return BackupResult('deferred',target,'backup_descriptor_unverified')
        if not set(required)<=set(participants):
            return BackupResult('deferred',target,'backup_participant_unmanaged')
        from .team_process_identity import process_identity
        for identity in (participants[key]['identity'] for key in required):
            try:current=process_identity(identity['pid'])
            except (ValueError,OSError):return BackupResult('deferred',target,'backup_participant_unverified')
            if current!=identity:return BackupResult('deferred',target,'backup_participant_unverified')
        contained=all(self.maintenance.has_native_containment(key,participants[key]['run_id']) for key in required)
        if self.require_native_containment and not contained:
            return BackupResult('deferred',target,'backup_native_containment_unverified')
        try: claim=self.maintenance.begin_barrier(owner_id=owner_id)
        except MaintenanceError as error:return BackupResult('deferred',target,error.code)
        paused=False;deadline=time.monotonic()+timeout_seconds;result=None
        try:
            # Registration was still allowed during preflight. Once this barrier
            # closes admission, bind its evidence to the exact checked runs.
            held=self.maintenance.assert_held(claim)
            current={p['participant_id']:p for p in held['participants']}
            if any(current.get(key)!=participants[key] for key in required):
                raise BackupError('backup_participant_changed')
            if all(self.maintenance.has_native_containment(key,current[key]['run_id']) for key in required)!=contained:
                raise BackupError('backup_participant_changed')
            while self.maintenance.active_tickets(claim):
                if time.monotonic()>deadline:raise BackupError('backup_drain_timeout')
                time.sleep(.025)
            paused=True # Even a timed-out control request may already have stopped native writes.
            proof=self.lifecycle.quiesce_component(self.manifest_path,maintenance=self.maintenance,claim=claim,timeout=max(.1,min(35,deadline-time.monotonic())))
            with ExitStack() as stack:
                for source in self.sources.values():
                    connection=stack.enter_context(closing(sqlite3.connect(source,timeout=.2,isolation_level=None)))
                    connection.execute('PRAGMA busy_timeout=200');connection.execute('BEGIN IMMEDIATE')
                    stack.callback(connection.rollback)
                self.lifecycle.verify_quiescence(self.manifest_path,proof,maintenance=self.maintenance,claim=claim)
                self.maintenance.freeze(claim,proof)
                target.mkdir(parents=True,exist_ok=False)
                _write(target/'manifest.partial.json',{'state':'copying','barrier_id':claim.barrier_id})
                (target/'databases').mkdir()
                databases={}
                source_issues=[]
                for role,source in self.sources.items():
                    output=target/'databases'/(role+'.sqlite3')
                    def progress(*_):
                        if time.monotonic()>deadline:raise BackupError('backup_deadline')
                    with closing(_ro(source)) as incoming,closing(sqlite3.connect(output)) as outgoing:
                        incoming.backup(outgoing,pages=256,progress=progress,sleep=.01)
                        _check(outgoing);metadata=_schema(outgoing)
                        if 'maintenance_restore_guard' in _tables(outgoing) and outgoing.execute('SELECT 1 FROM maintenance_restore_guard WHERE reconciliation_required=1').fetchone():
                            source_issues.append('backup_source_reconciliation_required')
                    databases[role]={'path':'databases/'+role+'.sqlite3','original_basename':source.name,
                                     'bytes':output.stat().st_size,'sha256':_hash(output),**metadata}
                omitted={path for source in (*self.sources.values(),self.maintenance.path) for path in (source,Path(str(source)+'-wal'),Path(str(source)+'-shm'),Path(str(source)+'-journal'))}
                files=[];source_files={}
                def walk_error(_):raise BackupError('backup_asset_unreadable')
                for name,root in sorted(self.roots.items()):
                    for parent,dirs,names in os.walk(root,followlinks=False,onerror=walk_error):
                        if time.monotonic()>deadline:raise BackupError('backup_deadline')
                        for dirname in dirs:_safe(Path(parent)/dirname)
                        for filename in sorted(names):
                            source=_safe(Path(parent)/filename,file=True)
                            if source in omitted:continue
                            relative=source.relative_to(root).as_posix()
                            copied=_copy(source,target/'assets'/name/relative,deadline)
                            files.append({'root':name,'path':relative,**copied});source_files[str(source)]=copied
                issues=list(source_issues)
                if not {'secretary_data','vikunja_files'}<=set(self.roots):issues.append('asset_roots_incomplete')
                if self.native_storage!='local-v2.7.0' or self.versions.get('vikunja')!='2.7.0':
                    issues.append('asset_native_storage_unverified')
                else:
                    with closing(_ro(target/'databases'/'vikunja.sqlite3',immutable=True)) as native:
                        if 'files' not in _tables(native) or not {'id','size'}<={row[1] for row in native.execute('PRAGMA table_info(files)')}:
                            issues.append('asset_native_schema_unverified')
                        else:
                            copied_native={item['path']:item for item in files if item['root']=='vikunja_files'}
                            for identity,size in native.execute('SELECT id,size FROM files'):
                                if type(identity) is not int or not 0<identity<=2**63-1 or type(size) is not int or size<0:
                                    issues.append('asset_native_reference_invalid');continue
                                entry=copied_native.get(str(identity))
                                if not entry or entry['bytes']!=size:issues.append('asset_native_reference_missing')
                with closing(_ro(target/'databases'/'secretary.sqlite3',immutable=True)) as secretary:
                    refs,ref_issues=_asset_references(secretary,self.roots);issues.extend(ref_issues)
                    if 'secretary_data' in self.roots:
                        for original in _capture_paths(secretary,self.roots['secretary_data']):
                            if str(original) not in source_files:continue
                            archived=target/'assets'/'secretary_data'/original.relative_to(self.roots['secretary_data'])
                            doc=_read(archived)
                            try:
                                refs.extend(chunk['path'] for chunk in doc.get('chunks',[]) if chunk.get('path'))
                                refs.extend(chunk['path'] for chunk in doc.get('in_progress',{}).values() if chunk.get('path'))
                            except (AttributeError,TypeError,KeyError):issues.append('asset_capture_manifest_invalid')
                    for value in refs:
                        if not Path(value).is_absolute() or str(Path(value)) not in source_files:issues.append('asset_reference_missing')
                    for path,digest in secretary.execute('SELECT path,sha256 FROM chunks') if 'chunks' in _tables(secretary) else ():
                        copied=source_files.get(str(Path(path)))
                        if copied and digest and copied['sha256']!=digest:issues.append('asset_chunk_hash_mismatch')
                self.lifecycle.verify_quiescence(self.manifest_path,proof,maintenance=self.maintenance,claim=claim)
                now=self.clock()
                if now.tzinfo is None or now.utcoffset() is None:raise BackupError('backup_clock_invalid')
                manifest={'schema_version':1,'state':'complete','full_recovery':not issues,
                          'backup_id':str(uuid4()),'deployment_id':self.maintenance.deployment_id,
                          'barrier_id':claim.barrier_id,'barrier_fence':claim.fence,'created_at':now.astimezone(timezone.utc).isoformat(),
                          'databases':databases,'asset_roots':{key:str(value) for key,value in self.roots.items()},
                          'assets':files,'issues':sorted(set(issues)),'config_versions':self.versions,
                          'native_quiescence':proof,'required_participants':list(required),
                          'native_containment_verified':contained,
                          'native_storage':self.native_storage}
                _write(target/'manifest.ready.json',manifest)
                os.replace(target/'manifest.ready.json',target/'manifest.json')
                result=BackupResult('complete',target,None,not issues,contained)
        except (MaintenanceError,BackupError) as error:result=BackupResult('deferred' if not target.exists() else 'failed',target,error.code)
        except Exception:result=BackupResult('failed',target,'backup_failed')
        finally:
            try:
                if paused:self.lifecycle.resume_component(self.manifest_path,maintenance=self.maintenance,claim=claim,timeout=35)
                self.maintenance.release(claim)
            except Exception:
                result=BackupResult('failed',target,'backup_resume_required',bool(result and result.full_recovery),bool(result and result.native_containment_verified))
        return result


def _member_path(root,relative):
    if not isinstance(relative,str) or '\\' in relative or ':' in relative or not relative or relative.startswith('/'):
        raise BackupError('backup_manifest_invalid')
    value=PurePosixPath(relative)
    if any(part in ('..','.') for part in relative.split('/')) or str(value)!=relative:
        raise BackupError('backup_manifest_invalid')
    return _safe(root.joinpath(*value.parts),file=True)


def _validate_archive(source):
    if not (source/'manifest.json').is_file():raise BackupError('backup_incomplete')
    manifest=_read(source/'manifest.json')
    required={'schema_version','state','full_recovery','backup_id','deployment_id','barrier_id','barrier_fence','created_at','databases','asset_roots','assets','issues','config_versions','native_quiescence','required_participants','native_storage','native_containment_verified'}
    if (not isinstance(manifest,dict) or set(manifest)!=required or manifest['schema_version']!=1 or manifest['state']!='complete'
            or manifest['full_recovery'] is not True or type(manifest['native_containment_verified']) is not bool or manifest['issues']!=[] or set(manifest['databases'])!=set(ROLES)
            or not {'secretary_data','vikunja_files'}<=set(manifest['asset_roots'])):raise BackupError('backup_incomplete')
    if any(not re.fullmatch('[a-z][a-z0-9_]{0,63}',key) or not isinstance(value,str) or not Path(value).is_absolute() for key,value in manifest['asset_roots'].items()):
        raise BackupError('backup_manifest_invalid')
    entries=[]
    for role,value in manifest['databases'].items():
        if value.get('path')!='databases/'+role+'.sqlite3':raise BackupError('backup_manifest_invalid')
        if not isinstance(value.get('original_basename'),str) or Path(value['original_basename']).name!=value['original_basename'] or ':' in value['original_basename']:
            raise BackupError('backup_manifest_invalid')
        entries.append((value['path'],value))
    for value in manifest['assets']:
        if value.get('root') not in manifest['asset_roots']:raise BackupError('backup_manifest_invalid')
        relative='assets/'+value['root']+'/'+value['path']
        entries.append((relative,value))
    if len({path for path,_ in entries})!=len(entries):raise BackupError('backup_manifest_invalid')
    for relative,value in entries:
        path=_member_path(source,relative)
        if type(value.get('bytes')) is not int or value['bytes']<0 or path.stat().st_size!=value['bytes'] or _hash(path)!=value.get('sha256'):
            raise BackupError('backup_hash_mismatch')
    for role in ROLES:
        # Archives contain hashed main snapshots only. A WAL/journal would be
        # unhashed content; refuse it and avoid creating read-only sidecars.
        with closing(_ro(source/'databases'/(role+'.sqlite3'),immutable=True)) as conn:_check(conn)
    return manifest


def _restore_guard(conn,restore_id,manifest_hash):
    conn.execute(RESTORE_GUARD_DDL)
    if conn.execute('SELECT 1 FROM maintenance_restore_guard').fetchone():
        raise BackupError('backup_already_restore_blocked')
    conn.execute('INSERT INTO maintenance_restore_guard VALUES(1,?,?,?)',(restore_id,1,manifest_hash))
    for verb in ('UPDATE','DELETE'):
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS backup_restore_no_{verb.lower()} BEFORE {verb} ON maintenance_restore_guard
            BEGIN SELECT RAISE(ABORT,'maintenance_restore_blocked'); END''')
    conn.execute('''CREATE TRIGGER IF NOT EXISTS backup_restore_no_replace BEFORE INSERT ON maintenance_restore_guard
        WHEN EXISTS(SELECT 1 FROM maintenance_restore_guard WHERE id=NEW.id)
        BEGIN SELECT RAISE(ABORT,'maintenance_restore_blocked'); END''')


def _relocate_secretary(path,old_roots,new_roots):
    def remap(value):
        original=Path(value)
        for key,root in old_roots.items():
            if original.is_relative_to(Path(root)):
                target=new_roots[key]/original.relative_to(Path(root))
                _safe(target,file=True)
                return str(target)
        raise BackupError('backup_asset_reference_unmapped')
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA foreign_keys=ON');tables=_tables(conn)
        captures=_capture_paths(conn,new_roots['secretary_data'])
        for table,column in (('meetings','media_path'),('chunks','path')):
            if table in tables:
                for identity,value in conn.execute(f'SELECT id,{column} FROM {table} WHERE {column} IS NOT NULL').fetchall():
                    conn.execute(f'UPDATE {table} SET {column}=? WHERE id=?',(remap(value),identity))
        if 'identification_intents' in tables:
            conn.execute("UPDATE jobs SET status='cancelled',cancel_requested=1,error='restore_review_required' WHERE id IN (SELECT job_id FROM identification_intents WHERE outcome!='completed')")
            conn.execute("UPDATE attribution_runs SET status='stale' WHERE status!='published' AND id IN (SELECT run_id FROM identification_intents WHERE outcome!='completed')")
            if 'meeting_pipeline_handoffs' in tables:
                conn.execute("""UPDATE meeting_pipeline_handoffs SET summary_authorized=0,authorization_reason='restore_review_required'
                    WHERE summary_job_id IS NULL AND EXISTS(SELECT 1 FROM identification_intents i
                    WHERE i.meeting_id=meeting_pipeline_handoffs.meeting_id AND i.transcript_version=meeting_pipeline_handoffs.transcript_version AND i.outcome!='completed')""")
            conn.execute("UPDATE identification_intents SET outcome='stale',reasons='[\"restore_review_required\"]' WHERE outcome!='completed'")
        if 'enrollment_recordings' in tables:
            conn.execute("UPDATE enrollment_recordings SET status='failed',disposition='review',reason='restore_review_required' WHERE status!='completed'")
        conn.commit();_check(conn)
    for path in captures:
        if path.exists():
            document=_read(path)
            try:
                for chunk in document.get('chunks',[]):
                    if chunk.get('path'):chunk['path']=remap(chunk['path'])
                for chunk in document.get('in_progress',{}).values():
                    if chunk.get('path'):chunk['path']=remap(chunk['path'])
            except (AttributeError,TypeError,KeyError):raise BackupError('backup_capture_manifest_invalid') from None
            with path.open('w',encoding='utf-8') as stream:stream.write(_json(document));stream.flush();os.fsync(stream.fileno())


def restore_backup(backup_dir,destination,*,restore_root):
    source=_safe(backup_dir);target=_fresh(destination,restore_root)
    if target.is_relative_to(source) or source.is_relative_to(target):raise BackupError('backup_destination_invalid')
    try:manifest=_validate_archive(source)
    except BackupError:raise
    except Exception:raise BackupError('backup_manifest_invalid') from None
    target.mkdir(parents=True,exist_ok=False)
    restore_id=str(uuid4());manifest_hash=_hash(source/'manifest.json');deadline=time.monotonic()+600
    _write(target/'restore.partial.json',{'restore_id':restore_id,'state':'copying','outbound_enabled':False})
    roots={key:target/'assets'/key for key in manifest['asset_roots']}
    for root in roots.values():root.mkdir(parents=True,exist_ok=True)
    for entry in manifest['assets']:
        copied=_copy(_member_path(source,'assets/'+entry['root']+'/'+entry['path']),roots[entry['root']]/entry['path'],deadline)
        if copied!={'sha256':entry['sha256'],'bytes':entry['bytes']}:raise BackupError('backup_hash_mismatch')
    paths={role:target/'databases'/(role+'.sqlite3') for role in ROLES}
    paths['secretary']=roots['secretary_data']/manifest['databases']['secretary']['original_basename']
    for role,path in paths.items():
        copied=_copy(source/'databases'/(role+'.sqlite3'),path,deadline)
        entry=manifest['databases'][role]
        if copied!={'sha256':entry['sha256'],'bytes':entry['bytes']}:raise BackupError('backup_hash_mismatch')
        # Each authority is blocked before copying the next database. Interrupted
        # restores cannot leave an earlier complete Team/Billing copy enabled.
        if role in ('secretary','team','billing'):
            with closing(sqlite3.connect(path)) as conn:
                _restore_guard(conn,restore_id,manifest_hash);conn.commit()
    _relocate_secretary(paths['secretary'],manifest['asset_roots'],roots)
    _write(target/'restore.json',{'schema_version':1,'state':'restored_for_reconciliation','restore_id':restore_id,
           'source_manifest_sha256':manifest_hash,'outbound_enabled':False,
           'databases':{role:str(path) for role,path in paths.items()},
           'asset_roots':{key:str(path) for key,path in roots.items()},
           'archived_original_roots':manifest['asset_roots'],
           'immutable_identification':'preserved; unfinished jobs quarantined; owner creates new intent'})
    return RestoreResult('restored_for_reconciliation',target,paths,roots,restore_id)


def retention_preview(backup_root,deployment_id):
    """Read-only 7 daily + 4 weekly selection; never authorizes deletion.

    Calendar grouping uses UTC timestamps recorded in immutable manifests.
    Partial, foreign and corrupt directories are never deletion candidates.
    """
    from uuid import UUID
    if not isinstance(deployment_id,str) or str(UUID(deployment_id))!=deployment_id:
        raise BackupError('backup_deployment_invalid')
    root=_safe(backup_root);entries=list(root.iterdir())
    if len(entries)>3650:raise BackupError('backup_retention_scan_bounded')
    complete=[];ignored=0
    for directory in entries:
        try:
            _safe(directory)
            if not directory.is_dir():continue
            manifest=_validate_archive(directory)
            if manifest['deployment_id']!=deployment_id:ignored+=1;continue
            timestamp=datetime.fromisoformat(manifest['created_at'])
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:raise BackupError('backup_manifest_invalid')
            complete.append((timestamp.astimezone(timezone.utc),directory,_hash(directory/'manifest.json')))
        except (BackupError,OSError,ValueError,TypeError,KeyError):ignored+=1
    keep=set();days=set();weeks=set()
    for stamp,path,_ in sorted(complete,key=lambda item:(item[0],str(item[1])),reverse=True):
        day=stamp.date();week=stamp.isocalendar()[:2]
        if day not in days and len(days)<7:keep.add(path);days.add(day)
        if week not in weeks and len(weeks)<4:keep.add(path);weeks.add(week)
    report=lambda selection:tuple({'path':str(path),'manifest_sha256':digest,'created_at':stamp.isoformat()}
        for stamp,path,digest in sorted(complete,key=lambda item:(item[0],str(item[1]))) if (path in keep)==selection)
    return {'state':'preview','policy':{'daily':7,'weekly':4,'calendar':'UTC'},'keep':report(True),
            'review_candidates':report(False),'ignored_directories':ignored,'deletion_authorized':False}
