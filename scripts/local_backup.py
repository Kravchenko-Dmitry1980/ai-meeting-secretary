"""Explicit local data backup, verification, and isolated restore commands."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from secretary.infrastructure.local_backup import (  # noqa: E402
    LocalBackupError,
    create_backup,
    restore_backup,
    verify_backup,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and verify a cold Secretary data backup, or restore it into a new isolated folder."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    backup = commands.add_parser("backup", help="Create a new full backup (the app must be stopped).")
    backup.add_argument("--data-dir", type=Path, required=True, help="Absolute Secretary data directory.")
    backup.add_argument("--destination", type=Path, required=True, help="New absolute backup directory.")

    verify = commands.add_parser("verify", help="Verify manifest, files, hashes, SQLite integrity, and references.")
    verify.add_argument("--backup-dir", type=Path, required=True, help="Absolute backup directory.")

    restore = commands.add_parser("restore", help="Restore into a new child directory; outbound processing is disabled.")
    restore.add_argument("--backup-dir", type=Path, required=True, help="Absolute backup directory.")
    restore.add_argument("--destination", type=Path, required=True, help="New absolute restore directory.")
    restore.add_argument("--restore-root", type=Path, required=True, help="Existing parent directory for the isolated restore.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "backup":
            result = create_backup(args.data_dir, args.destination)
        elif args.command == "verify":
            result = verify_backup(args.backup_dir)
        else:
            result = restore_backup(args.backup_dir, args.destination, restore_root=args.restore_root)
        print(json.dumps({"ok": True, **result}, sort_keys=True, ensure_ascii=True))
        return 0
    except LocalBackupError as exc:
        print(json.dumps({"ok": False, "code": exc.code}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
