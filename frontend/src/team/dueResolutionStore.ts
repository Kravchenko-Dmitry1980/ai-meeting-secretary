import { TeamApiError } from './api.ts';
import type { Actor, CommandReceipt, DueResolutionCandidate, DueResolutionPreview, TeamApi, TaskSnapshot } from './api.ts';
import { immutable } from './model.ts';

export type DueOperation = Readonly<{ operationId: string; taskId: string; title: string;
  transport: 'sending' | 'known' | 'unknown' | 'rejected'; receipt: CommandReceipt | null; errorCode: string | null }>;
export type DueResolutionState = Readonly<{ scopeKey: string; open: boolean; busy: boolean;
  candidate: DueResolutionCandidate | null; preview: DueResolutionPreview | null; operationId: string | null;
  errorCode: string | null; operations: readonly DueOperation[] }>;
export function dueScopeKey(actor: Actor | null, epoch: number, project: string | null, task: TaskSnapshot | null) {
  return JSON.stringify([actor?.id, actor?.revision, actor?.role, epoch, project, task?.task_id, task?.revision, task?.remote_fingerprint]);
}
export function utcFromMoscow(value: string): string | undefined {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value) || value.startsWith('0000-')) return undefined;
  const date = new Date(`${value}:00+03:00`);
  return Number.isFinite(date.getTime()) && new Date(date.getTime() + 10800000).toISOString().slice(0, 16) === value ? date.toISOString() : undefined;
}
const blank = (scopeKey = ''): DueResolutionState => immutable({ scopeKey, open: false, busy: false,
  candidate: null, preview: null, operationId: null, errorCode: null, operations: [] });
const code = (error: unknown) => error instanceof TeamApiError ? error.code : 'team_due_resolution_unavailable';
export class TeamDueResolutionStore {
  private state = blank();
  private listeners = new Set<() => void>();
  private actor: Actor | null = null;
  private epoch = -1;
  private project: string | null = null;
  private task: TaskSnapshot | null = null;
  private generation = 0;
  private active = false;
  private read: AbortController | null = null;
  private posts = new Map<string, Promise<void>>();
  private polls = new Map<string, Promise<void>>();
  private api: TeamApi;
  private options: { uuid?: () => string; now?: () => number };
  constructor(api: TeamApi, options: { uuid?: () => string; now?: () => number } = {}) { this.api = api; this.options = options; }
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private update(patch: Partial<DueResolutionState>) {
    this.state = immutable({ ...this.state, ...patch });
    for (const listener of this.listeners) listener();
  }
  setScope(actor: Actor | null, epoch: number, project: string | null, task: TaskSnapshot | null) {
    const key = dueScopeKey(actor, epoch, project, task);
    if (this.active && key === this.state.scopeKey) return;
    const sameBase = this.active && actor?.id === this.actor?.id && actor?.revision === this.actor?.revision
      && epoch === this.epoch && project === this.project;
    const operations = sameBase ? this.state.operations : [];
    this.generation += 1; this.read?.abort(); this.read = null;
    this.actor = actor ? immutable(actor) : null; this.epoch = epoch; this.project = project;
    this.task = task ? immutable(task) : null; this.active = true;
    this.state = immutable({ ...blank(key), operations });
    for (const listener of this.listeners) listener();
  }
  disconnect = () => { this.active = false; this.generation += 1; this.read?.abort(); this.read = null;
    this.state = blank(); for (const listener of this.listeners) listener(); };
  private base() { return JSON.stringify([this.actor?.id, this.actor?.revision, this.epoch, this.project]); }
  private live(base: string) { return this.active && base === this.base() && this.actor?.role === 'owner'
    && this.api.actor?.id === this.actor.id && this.api.actor.revision === this.actor.revision
    && this.api.sessionEpoch === this.epoch && !!this.project && this.api.actor.project_ids.includes(this.project); }
  private target() {
    if (!this.live(this.base()) || !this.task || this.task.project_id !== this.project) throw new TeamApiError('team_preview_stale', 0);
    return this.task;
  }
  close = () => { this.generation += 1; this.read?.abort(); this.read = null;
    this.update({ open: false, busy: false, candidate: null, preview: null, operationId: null, errorCode: null }); };
  load = async () => {
    if (this.state.busy) return;
    const task = this.target(), base = this.base(), generation = ++this.generation;
    if (this.state.operations.some(operation => operation.taskId === task.task_id
      && (operation.transport === 'sending' || operation.transport === 'unknown'
        || operation.receipt && ['queued', 'running', 'reconciling', 'uncertain'].includes(operation.receipt.execution_state.state))))
      throw new TeamApiError('team_operation_pending', 0);
    const abort = new AbortController(); this.read?.abort(); this.read = abort;
    this.update({ open: true, busy: true, candidate: null, preview: null, operationId: null, errorCode: null });
    try {
      const candidate = await this.api.getDueResolution(task.task_id, { signal: abort.signal });
      if (!this.live(base) || generation !== this.generation) return;
      if (candidate.project_id !== task.project_id || candidate.task_id !== task.task_id
        || candidate.baseline_revision !== task.revision || candidate.baseline_fingerprint !== task.remote_fingerprint)
        throw new TeamApiError('team_preview_stale', 409);
      this.update({ candidate });
    } catch (error) { if (this.live(base) && generation === this.generation) this.update({ errorCode: code(error) }); }
    finally { if (this.live(base) && generation === this.generation) this.update({ busy: false }); }
  };
  prepare = async (due: string | null | undefined, reason: string) => {
    if (this.state.busy) return;
    this.target();
    if (!this.state.candidate || due === undefined || !reason.trim()) throw new TeamApiError('team_due_required', 0);
    const base = this.base(), generation = this.generation, candidate = this.state.candidate;
    this.update({ busy: true, preview: null, operationId: null, errorCode: null });
    try {
      const preview = await this.api.createDueResolutionPreview(candidate, { due_at: due, reason });
      if (this.live(base) && generation === this.generation) this.update({ preview,
        operationId: (this.options.uuid ?? (() => crypto.randomUUID()))() });
    } catch (error) { if (this.live(base) && generation === this.generation) this.update({ errorCode: code(error),
      ...(error instanceof TeamApiError && [401, 403, 409].includes(error.status) ? { candidate: null } : {}) }); }
    finally { if (this.live(base) && generation === this.generation) this.update({ busy: false }); }
  };
  confirm = (): Promise<void> => {
    const preview = this.state.preview, id = this.state.operationId;
    if (!preview || !id) return Promise.reject(new TeamApiError('team_preview_stale', 0));
    if (this.posts.has(id)) return this.posts.get(id)!;
    if (this.state.operations.some(operation => operation.operationId === id)) return Promise.resolve();
    this.target();
    if (Date.parse(preview.expires_at) <= (this.options.now ?? Date.now)())
      return Promise.reject(new TeamApiError('team_due_preview_expired', 409));
    const base = this.base();
    this.update({ operations: [...this.state.operations, { operationId: id, taskId: preview.candidate.task_id,
      title: preview.candidate.title, transport: 'sending', receipt: null, errorCode: null }] });
    const work = this.submit(preview, id, base); this.posts.set(id, work); return work;
  };
  private operation(id: string, patch: Partial<DueOperation>) {
    this.update({ operations: this.state.operations.map(operation => operation.operationId === id ? { ...operation, ...patch } : operation) });
  }
  private async submit(preview: DueResolutionPreview, id: string, base: string) {
    try {
      const receipt = await this.api.confirmDueResolution(preview, id);
      if (!this.live(base)) return;
      this.operation(id, { transport: 'known', receipt, errorCode: null });
      if (this.state.operationId === id) this.update({ preview: null, candidate: null, operationId: null, open: false });
    } catch (error) { if (this.live(base)) {
      this.operation(id, { transport: error instanceof TeamApiError && error.uncertain ? 'unknown' : 'rejected', errorCode: code(error) });
      if (error instanceof TeamApiError && [401, 403, 409].includes(error.status) && this.state.operationId === id)
        this.update({ preview: null, candidate: null, operationId: null, errorCode: code(error) });
    } } finally { this.posts.delete(id); }
  }
  poll = (id: string): Promise<void> => {
    if (this.polls.has(id)) return this.polls.get(id)!;
    const operation = this.state.operations.find(value => value.operationId === id);
    if (!operation || !this.live(this.base())) return Promise.reject(new TeamApiError('team_preview_stale', 0));
    const work = this.readReceipt(operation, this.base()); this.polls.set(id, work); return work;
  };
  private async readReceipt(operation: DueOperation, base: string) {
    try {
      const receipt = await this.api.getReceipt(operation.operationId);
      if (!this.live(base)) return;
      if (receipt.acceptance_receipt.operation_id !== operation.operationId
        || receipt.execution_state.task_id != null && receipt.execution_state.task_id !== operation.taskId
        || receipt.current && (receipt.current.task_id !== operation.taskId || receipt.current.project_id !== this.project))
        throw new TeamApiError('team_response_invalid', 0);
      this.operation(operation.operationId, { transport: 'known', receipt, errorCode: null });
    } catch (error) { if (this.live(base)) this.operation(operation.operationId, { errorCode: code(error) }); }
    finally { this.polls.delete(operation.operationId); }
  }
}
