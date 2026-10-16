"""Explicit interactive local operator enrollment / R3 deny preparation only."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend'))

from secretary.infrastructure.restore_operator import OperatorAuthority, OperatorError
from secretary.infrastructure.restore_reconciliation_control import ControlError
from secretary.infrastructure.team_restore_reconciliation import prepare_restoration, ReconciliationError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action',required=True)
    sub.add_parser('enroll-operator',help='Explicitly enroll current Windows local operator; fresh consent required')
    prepare = sub.add_parser('prepare',help='Prepare deny dispositions; retain all activation holds')
    prepare.add_argument('--backup',required=True)
    prepare.add_argument('--restore',required=True)
    prepare.add_argument('--maintenance',required=True)
    prepare.add_argument('--lifecycle',required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'enroll-operator':
            result = {'state':'operator_enrolled','operator_digest':OperatorAuthority(ROOT).enroll()}
        else:
            result = prepare_restoration(args.backup,args.restore,
                maintenance_path=args.maintenance,lifecycle_path=args.lifecycle)
        print(json.dumps(result,sort_keys=True))
        return 0
    except (OperatorError,ControlError,ReconciliationError) as error:
        print(json.dumps({'state':'blocked','code':error.code},sort_keys=True))
        return 1
    except (ValueError,OSError):
        print(json.dumps({'state':'blocked','code':'restore_reconciliation_validation_failed'},sort_keys=True))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
