"""Explicit disposable native GET diagnostic; retains every activation hold."""
from __future__ import annotations
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))

from secretary.infrastructure.restore_managed_native import (
    ManagedNativeError, WORKER_MARKER, observe_restored_native,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(WORKER_MARKER, action='store_true', required=True)
    for name in ('project-root', 'backup', 'restore', 'maintenance', 'lifecycle', 'config'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if len(sys.argv) < 2 or sys.argv[1] != WORKER_MARKER:
        parser.error('worker marker must be the first argument')
    try:
        result = asyncio.run(observe_restored_native(args.project_root, args.backup, args.restore,
            maintenance_path=args.maintenance, lifecycle_path=args.lifecycle, config_path=args.config))
        print(json.dumps(asdict(result), sort_keys=True))
        return 0 if result.state == 'observed' else 1
    except ManagedNativeError as error:
        print(json.dumps(dict(state='failed', code=error.code, diagnostic_only=True,
                             activation_supported=False, outbound_enabled=False), sort_keys=True))
        return 1
    except (ValueError, OSError, RuntimeError):
        print(json.dumps(dict(state='failed', code='managed_native_invalid', diagnostic_only=True,
                             activation_supported=False, outbound_enabled=False), sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
