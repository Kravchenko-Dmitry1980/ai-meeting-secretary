"""Process-stop checks for the blocked R5 candidate binding on synthetic stores."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path

import pytest

from test_restore_admission_binding import generation_with_fence_lineage, module as admission_module
from test_restore_source_preparation_control import api, binding, h


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows custody contract')


def private(tmp_path):
    directory = tmp_path / 'private'
    import secretary.infrastructure.restore_operator as operator
    operator.create_private_directory(directory)
    return directory


def bound_candidate(directory):
    contract = admission_module()
    candidate = contract.SuccessorFenceCandidateV1.from_source_generation(
        generation_with_fence_lineage())
    bound = binding(directory)
    record = candidate.to_prepared_blocked_record()
    context = bound['context']
    context['expected_successor_fence'] = candidate.expected_successor_fence
    context['successor_fence_candidate_sha256'] = candidate.candidate_sha256
    context['source_context_sha256'] = sha256(json.dumps(
        record, sort_keys=True, separators=(',', ':')).encode('ascii')).hexdigest()
    return bound, candidate


def _run_child(path, bound, mode):
    repository = Path(__file__).resolve().parents[1]
    script = r"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from secretary.infrastructure.restore_source_preparation_control import SourcePreparationControl
control = SourcePreparationControl.open_existing(Path(sys.argv[2]), json.loads(sys.argv[3]))
if sys.argv[4] == 'after_commit':
    from secretary.infrastructure.restore_operator import VerifiedOperatorApproval
    values = json.loads(sys.argv[5])
    control.consume_approval(VerifiedOperatorApproval(*values))
else:
    with control._transaction() as (conn, snapshot):
        values = json.loads(sys.argv[5])
        conn.execute('INSERT INTO source_operator_approval VALUES(1,?,?,?,?)', values)
        os._exit(73)
os._exit(73)
"""
    receipt = (h('synthetic child operator'), h('synthetic child approval'),
        h('synthetic child nonce'), int(time.time()) + 120)
    return subprocess.run(
        [sys.executable, '-B', '-c', script, str(repository / 'backend'), str(path),
         json.dumps(bound, sort_keys=True), mode, json.dumps(receipt)],
        cwd=repository, capture_output=True, text=True, timeout=20, check=False)


def test_candidate_binding_survives_process_exit_after_blocked_commit(tmp_path):
    directory = private(tmp_path)
    port = api()
    bound, candidate = bound_candidate(directory)
    path = directory / 'source-preparation.sqlite3'
    port.SourcePreparationControl.create(path, bound)

    child = _run_child(path, bound, 'after_commit')

    assert child.returncode == 73, child.stderr[-1200:]
    reopened = port.SourcePreparationControl.open_existing(path, bound)
    snapshot = reopened.snapshot()
    assert (snapshot['binding']['context']['expected_successor_fence']
        == candidate.expected_successor_fence)
    assert (snapshot['binding']['context']['successor_fence_candidate_sha256']
        == candidate.candidate_sha256)
    assert snapshot['approval']['approval_digest'] == h('synthetic child approval')
    assert snapshot['state'] == 'partial_prepared_blocked'
    assert snapshot['activation_supported'] is False and snapshot['outbound_enabled'] is False
    assert not hasattr(reopened, 'activate') and not hasattr(reopened, 'transfer_fence')


def test_uncommitted_candidate_control_write_is_not_adopted_after_process_exit(tmp_path):
    directory = private(tmp_path)
    port = api()
    bound, _ = bound_candidate(directory)
    path = directory / 'source-preparation.sqlite3'
    port.SourcePreparationControl.create(path, bound)

    child = _run_child(path, bound, 'before_commit')

    assert child.returncode == 73, child.stderr[-1200:]
    journal = Path(str(path) + '-journal')
    assert journal.is_file(), 'the child must stop with the SQLite transaction still uncommitted'
    with pytest.raises(port.SourcePreparationControlError) as error:
        port.SourcePreparationControl.open_existing(path, bound)
    assert error.value.code == 'source_preparation_store_invalid'
    assert journal.is_file(), 'failed closed without repairing or deleting crash evidence'
