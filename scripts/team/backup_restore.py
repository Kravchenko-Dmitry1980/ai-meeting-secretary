"""Explicit local backup/restore CLI. No default owner paths or environment reads."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend'))
from secretary.infrastructure.team_backup import BackupError,BackupService,restore_backup,retention_preview,_read,_safe
from secretary.infrastructure.team_maintenance import MaintenanceRepository


def main(argv=None):
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='action',required=True)
    backup=sub.add_parser('backup');backup.add_argument('--config',required=True);backup.add_argument('--destination',required=True);backup.add_argument('--apply',action='store_true')
    restore=sub.add_parser('restore');restore.add_argument('--backup',required=True);restore.add_argument('--destination',required=True);restore.add_argument('--restore-root',required=True);restore.add_argument('--apply',action='store_true')
    retention=sub.add_parser('retention-preview');retention.add_argument('--backup-root',required=True);retention.add_argument('--deployment-id',required=True)
    reconciliation=sub.add_parser('reconciliation-preview');reconciliation.add_argument('--backup',required=True);reconciliation.add_argument('--restore',required=True)
    args=parser.parse_args(argv)
    try:
        if args.action=='reconciliation-preview':
            from secretary.infrastructure.team_restore_preview import reconciliation_preview
            print(json.dumps(reconciliation_preview(args.backup,args.restore),sort_keys=True));return 0
        if args.action=='retention-preview':
            print(json.dumps(retention_preview(args.backup_root,args.deployment_id),sort_keys=True));return 0
        # Preview never opens sources, starts processes or creates directories.
        if not args.apply:
            print(json.dumps({'state':'preview','action':args.action,'requires_apply':True,'outbound_enabled':False}))
            return 0
        if args.action=='restore':
            result=restore_backup(args.backup,args.destination,restore_root=args.restore_root)
        else:
            config=_read(_safe(args.config,file=True))
            required={'schema_version','deployment_id','maintenance_database','lifecycle_manifest','databases','backup_root','asset_roots','participant_descriptors','config_versions','native_storage'}
            if not isinstance(config,dict) or set(config)!=required or config['schema_version']!=1:
                raise BackupError('backup_config_invalid')
            specification=importlib.util.spec_from_file_location('team_backup_supervisor',ROOT/'scripts/team/supervisor.py')
            lifecycle=importlib.util.module_from_spec(specification);sys.modules[specification.name]=lifecycle;specification.loader.exec_module(lifecycle)
            control=_safe(config['maintenance_database'],file=True)
            maintenance=MaintenanceRepository(control,config['deployment_id'])
            service=BackupService(maintenance,config['databases'],backup_root=config['backup_root'],asset_roots=config['asset_roots'],
                lifecycle=lifecycle,manifest_path=config['lifecycle_manifest'],participant_descriptors=config['participant_descriptors'],
                config_versions=config['config_versions'],native_storage=config['native_storage'])
            result=service.backup(args.destination)
        # Paths are local administration output; no content, provider headers,
        # tokens, account IDs, raw errors or native command arguments are emitted.
        print(json.dumps(asdict(result),default=str,sort_keys=True))
        return 0 if result.state in {'complete','restored_for_reconciliation'} and getattr(result,'full_recovery',True) else 2
    except Exception as error:
        code=error.code if isinstance(error,BackupError) else 'backup_operation_failed'
        print(json.dumps({'state':'failed','code':code,'outbound_enabled':False}))
        return 2


if __name__=='__main__':raise SystemExit(main())
