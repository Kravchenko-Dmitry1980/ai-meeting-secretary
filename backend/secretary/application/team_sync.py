"""Bounded full-project GET polling; no provider mutations or invented authors."""
from __future__ import annotations

from secretary.domain.team import TeamConflict
from secretary.infrastructure.team_sync_repository import SyncObservation, _error
from secretary.infrastructure.vikunja import (
    MAX_ID, MAX_ITEMS, MAX_PAGES, MappingMemberDirectory, TaskReadContext, VikunjaClient, VikunjaError,
)


class _FrozenRead:
    """Reuse the existing mapper with one captured observation and frozen IDs.

    get_task's only remote read is observe_task. Here it returns the exact
    already fetched facts, so mapping cannot cause another GET or change the
    shared client's member directory.
    """
    def __init__(self, binding, members, observation):
        self.binding, self.members, self.observation = binding, members, observation

    def observe_task(self, identifier, *, conditional=False):
        if identifier != self.observation.task_id:
            raise VikunjaError('projection_context_scope_mismatch')
        return self.observation


class TeamSyncService:
    def __init__(self, repository, client, *, worker_id='team-sync'):
        self.repository, self.client, self.worker_id = repository, client, worker_id

    def sync_once(self):
        repository, client = self.repository, self.client
        claim = repository.begin(client.binding.project_id, worker_id=self.worker_id)
        try:
            client.validate_binding()
            identifiers, seen, page_number, total, pages = [], set(), 1, None, None
            while True:
                claim = repository.renew(claim)
                page = client.list_project_tasks(page=page_number, per_page=50)
                if (type(page.page) is not int or page.page != page_number
                        or type(page.total) is not int or not 0 <= page.total <= MAX_ITEMS
                        or type(page.total_pages) is not int or not 0 <= page.total_pages <= MAX_PAGES
                        or (total is not None and (total != page.total or pages != page.total_pages))):
                    raise VikunjaError('sync_scan_incomplete')
                total, pages = page.total, page.total_pages
                for item in page.items:
                    identifier = item.get('id')
                    if type(identifier) is not int or not 0 < identifier <= MAX_ID:
                        raise VikunjaError('sync_scan_incomplete')
                    identifier = str(identifier)
                    if identifier in seen or len(seen) >= MAX_ITEMS:
                        raise VikunjaError('sync_scan_incomplete')
                    seen.add(identifier)
                    identifiers.append(identifier)
                if page.next_page is None:
                    if len(seen) != total or page_number != max(1, pages):
                        raise VikunjaError('sync_scan_incomplete')
                    break
                if page.next_page != page_number + 1 or page.next_page > MAX_PAGES:
                    raise VikunjaError('sync_scan_incomplete')
                page_number = page.next_page
            baseline = {item.task_id: item for item in claim.projections}
            members = MappingMemberDirectory(claim.members)
            evidence, snapshots = [], []
            for identifier in identifiers:
                claim = repository.renew(claim)
                observed = None
                try:
                    observed = client.observe_task(identifier)
                    if observed.task_id != identifier or observed.project_id != claim.project_id:
                        raise VikunjaError('sync_scope_invalid')
                    previous = baseline.get(identifier)
                    context = TaskReadContext(previous.revision + 1 if previous else 0, baseline=previous)
                    snapshot = VikunjaClient.get_task(_FrozenRead(client.binding, members, observed), identifier, context=context)
                    fields = tuple(name for name in ('title', 'description', 'assignee_id', 'bucket', 'important', 'urgent',
                        'classification_confirmed', 'due_at', 'due_confirmed') if previous and getattr(previous, name) != getattr(snapshot, name))
                    if previous and not fields and previous.remote_fingerprint != snapshot.remote_fingerprint:
                        fields = ('remote_fingerprint',)
                    evidence.append(SyncObservation(identifier, observed.remote_fingerprint, fields))
                    snapshots.append(snapshot)
                except VikunjaError as error:
                    evidence.append(SyncObservation(identifier, observed.remote_fingerprint if observed else None,
                        (), _error(error.code)))
            return repository.commit(claim, tuple(evidence), tuple(snapshots), tuple(identifiers))
        except TeamConflict:
            # A stale fence must never finalize a newer worker's run.
            raise
        except Exception as error:
            return repository.fail(claim, _error(error.code) if isinstance(error, VikunjaError) else 'sync_read_failed')
