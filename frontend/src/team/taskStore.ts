/** Pure state controller. A successful HTTP exchange never implies task execution. */
import type { Actor, CommandReceipt, SessionView, TaskHistoryEntry, TeamApi, TeamStatusView } from './api.ts';
import { buildTaskCommand, compareIDs, emptyDraft, immutable, TeamStateError } from './model.ts';
import type { AssigneeBinding, TaskCommand, TaskSnapshot, TeamDraft } from './model.ts';

export interface TrackedOperation {
  command: TaskCommand;
  receipt: CommandReceipt | null;
  transport: 'sending' | 'known' | 'unknown' | 'rejected';
  errorCode: string | null;
}
export interface TeamTaskState {
  actor: Actor | null;
  projectId: string | null;
  tasks: readonly TaskSnapshot[];
  members: readonly AssigneeBinding[];
  status: TeamStatusView | null;
  metadataErrorCode: string | null;
  selectedTaskId: string | null;
  selectedTask: TaskSnapshot | null;
  history: readonly TaskHistoryEntry[];
  detailLoading: boolean;
  draft: TeamDraft;
  draftVersion: number;
  preview: TaskCommand | null;
  operations: readonly TrackedOperation[];
  loading: boolean;
  errorCode: string | null;
  sessionExpired: boolean;
  lastFetchedAt: string | null;
}
interface Options { clock?: () => number; uuid?: () => string; autoSubscribe?: boolean }
interface Capture { session: number; apiSession: number; scope: number; projectId: string | null; actorId: string | null }

const emptyState = (): TeamTaskState => ({ actor: null, projectId: null, tasks: [], members: [], status: null,
  metadataErrorCode: null, selectedTaskId: null, selectedTask: null, history: [], detailLoading: false,
  draft: emptyDraft(), draftVersion: 0, preview: null, operations: [], loading: false,
  errorCode: null, sessionExpired: false, lastFetchedAt: null });
function code(error: unknown): string {
  if (error && typeof error === 'object' && 'code' in error && typeof error.code === 'string'
      && /^[a-z][a-z0-9_]{0,100}$/.test(error.code)) return error.code;
  return 'team_request_failed';
}
function status(error: unknown): number | undefined {
  return error && typeof error === 'object' && 'status' in error && typeof error.status === 'number' ? error.status : undefined;
}
function uncertain(error: unknown): boolean {
  return !status(error) || status(error)! >= 500 || (error !== null && typeof error === 'object' && 'uncertain' in error && error.uncertain === true);
}
function canonical(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.entries(value).sort(([a],[b]) => a < b ? -1 : a > b ? 1 : 0)
    .map(([key, child]) => JSON.stringify(key) + ':' + canonical(child)).join(',') + '}';
  return JSON.stringify(value) ?? 'null';
}
function payload(command: TaskCommand): string {
  const { operation_id: ignored, ...body } = command;
  void ignored;
  return canonical(body);
}
function snapshotValid(task: TaskSnapshot, projectId: string): boolean {
  return !!task && typeof task.task_id === 'string' && /^[1-9][0-9]{0,127}$/.test(task.task_id)
    && task.project_id === projectId && Number.isSafeInteger(task.revision) && task.revision >= 0
    && /^[0-9a-f]{64}$/.test(task.remote_fingerprint);
}

export function operationState(operation: TrackedOperation): string {
  if (operation.receipt) return operation.receipt.execution_state.state;
  return operation.transport === 'sending' ? 'submitting' : operation.transport === 'unknown' ? 'uncertain' : 'rejected';
}

export class TeamTaskStore {
  private state: TeamTaskState = immutable(emptyState());
  private listeners = new Set<() => void>();
  private session = 0;
  private apiEpoch = -1;
  private scope = 0;
  private load = 0;
  private selection = 0;
  private historyLoad = 0;
  private disposed = false;
  private disconnect: (() => void) | null = null;
  private operations = new Map<string, TrackedOperation>();
  private previews = new Map<string, { command: TaskCommand; capture: Capture; draftVersion:number }>();
  private posts = new Map<string, Promise<CommandReceipt | undefined>>();
  private reads = new Map<string, Promise<CommandReceipt | undefined>>();
  private clock: () => number;
  private uuid: () => string;
  readonly api: TeamApi;

  constructor(api: TeamApi, options: Options = {}) {
    this.api = api;
    this.clock = options.clock ?? Date.now;
    this.uuid = options.uuid ?? (() => crypto.randomUUID());
    this.setSession(api.actor);
    if (options.autoSubscribe !== false) this.connect();
  }

  getSnapshot = (): TeamTaskState => this.state;
  subscribe = (listener: () => void): (() => void) => { this.listeners.add(listener); return () => this.listeners.delete(listener); };
  connect = (): (() => void) => {
    if (this.disposed) throw new TeamStateError('team_store_disposed');
    this.disconnect?.();
    const stop = this.api.subscribeSession(() => {
      const expired = !!this.state.actor && !this.api.actor;
      this.setSession(this.api.actor);
      if (expired) this.update({ sessionExpired: true });
    });
    this.disconnect = stop;
    return () => { if (this.disconnect === stop) this.disconnect = null; stop(); };
  };
  dispose = (): void => {
    this.disconnect?.(); this.disconnect = null; this.disposed = true;
    this.session++; this.scope++; this.selection++; this.load++;
    this.listeners.clear(); this.previews.clear(); this.operations.clear();
    this.state = immutable(emptyState());
  };
  private update(patch: Partial<TeamTaskState>) {
    if (this.disposed) return;
    this.state = Object.freeze({ ...this.state, ...patch });
    for (const listener of this.listeners) listener();
  }
  private capture(): Capture {
    return { session: this.session, apiSession: this.api.sessionEpoch, scope: this.scope,
      projectId: this.state.projectId, actorId: this.state.actor?.id ?? null };
  }
  private live(capture: Capture): boolean {
    return !this.disposed && capture.session === this.session && capture.apiSession === this.api.sessionEpoch
      && capture.actorId === this.state.actor?.id;
  }
  private scoped(capture: Capture): boolean {
    return this.live(capture) && capture.scope === this.scope && capture.projectId === this.state.projectId;
  }
  private showOperations() {
    this.update({ operations: Object.freeze([...this.operations.values()].filter(op => op.command.project_id === this.state.projectId)) });
  }
  setSession = (session: Actor | SessionView | null): void => {
    if (this.disposed) return;
    const actor = session && ('actor' in session ? session.actor : session);
    if (canonical(actor) === canonical(this.state.actor) && this.api.sessionEpoch === this.apiEpoch) return;
    this.apiEpoch = this.api.sessionEpoch;
    this.session++; this.scope++; this.selection++; this.load++;
    this.previews.clear(); this.operations.clear(); this.posts.clear(); this.reads.clear();
    this.state = immutable({ ...emptyState(), actor });
    for (const listener of this.listeners) listener();
  };
  selectProject = async (projectId: string | null): Promise<void> => {
    if (projectId !== null && (!this.state.actor || !this.state.actor.project_ids.includes(projectId))) throw new TeamStateError('team_scope_invalid');
    this.scope++; this.selection++; this.load++; this.previews.clear();
    this.update({ projectId, tasks: [], members: [], status: null, metadataErrorCode: null,
      selectedTaskId: null, selectedTask: null, history: [], detailLoading: false,
      draft: emptyDraft(), draftVersion: this.state.draftVersion + 1, preview: null,
      loading: false, errorCode: null, lastFetchedAt: null });
    this.showOperations();
    if (projectId !== null) await this.refresh();
  };
  private merge(task: TaskSnapshot) {
    const previous = this.state.tasks.find(item => item.task_id === task.task_id);
    if (previous && previous.revision >= task.revision) return;
    const copy = immutable(task);
    const tasks = [...this.state.tasks.filter(item => item.task_id !== copy.task_id), copy].sort((a,b) => compareIDs(a.task_id,b.task_id));
    this.update({ tasks: Object.freeze(tasks), ...(this.state.selectedTaskId === copy.task_id ? { selectedTask: copy } : {}) });
  }
  refresh = async (): Promise<void> => {
    const capture = this.capture(), generation = ++this.load;
    if (!capture.projectId || !this.state.actor) return;
    const initialTasks = new Map(this.state.tasks.map(task => [task.task_id,task]));
    this.update({ loading: true, errorCode: null });
    const metadata = this.refreshMetadata(capture,generation);
    try {
      const rows = new Map<string, TaskSnapshot>();
      const cursors = new Set<string>();
      let after: string | undefined;
      for (let page = 0; page < 100; page++) {
        const result = await this.api.listTasks(capture.projectId, { limit: 100, after });
        if (!this.scoped(capture) || generation !== this.load) return;
        for (const task of result.items) {
          if (!snapshotValid(task, capture.projectId)) throw new TeamStateError('team_task_response_invalid');
          if (!rows.has(task.task_id) || rows.get(task.task_id)!.revision < task.revision) rows.set(task.task_id, immutable(task));
        }
        if (!result.next_cursor) {
          // Preserve snapshots received after this list started, including a new create receipt.
          // Unchanged rows absent from the fresh list can still disappear after remote deletion.
          for (const task of this.state.tasks) if ((rows.has(task.task_id) && task.revision > rows.get(task.task_id)!.revision)
              || (!rows.has(task.task_id) && initialTasks.get(task.task_id)!==task)) rows.set(task.task_id,task);
          const tasks = Object.freeze([...rows.values()].sort((a,b) => compareIDs(a.task_id,b.task_id)));
          this.update({ tasks, selectedTask: tasks.find(t => t.task_id === this.state.selectedTaskId) ?? null,
            lastFetchedAt: new Date(this.clock()).toISOString() });
          return;
        }
        if (cursors.has(result.next_cursor)) throw new TeamStateError('team_pagination_invalid');
        cursors.add(result.next_cursor); after = result.next_cursor;
      }
      throw new TeamStateError('team_pagination_incomplete');
    } catch (error) {
      if (this.scoped(capture) && generation === this.load) this.failure(error);
    } finally {
      await metadata;
      if (this.scoped(capture) && generation === this.load) this.update({ loading: false });
    }
  };
  private async refreshMetadata(capture: Capture,generation:number) {
    if (!capture.projectId) return;
    const current = () => this.scoped(capture) && generation===this.load;
    const work: Promise<void>[] = [];
    if (this.state.actor?.role === 'owner' && typeof this.api.listMembers === 'function') work.push((async () => {
      const members: AssigneeBinding[] = [], cursors = new Set<string>(); let after: string | undefined;
      for (let page = 0; page < 100; page++) {
        const result = await this.api.listMembers(capture.projectId!,{limit:100,after});
        if (!current()) return;
        members.push(...result.items.map(member => ({...member, project_ids:[capture.projectId!]})));
        if (!result.next_cursor) { this.update({members:immutable(members)}); return; }
        if (cursors.has(result.next_cursor)) throw new TeamStateError('team_pagination_invalid');
        cursors.add(result.next_cursor); after=result.next_cursor;
      }
      throw new TeamStateError('team_pagination_incomplete');
    })());
    if (typeof this.api.getStatus === 'function') work.push((async () => {
      try {
        const result = await this.api.getStatus(capture.projectId!);
        if (current()) {
          if (result.project_id !== capture.projectId) throw new TeamStateError('team_scope_invalid');
          this.update({status:immutable(result)});
        }
      } catch(error) {
        if(current())this.update({status:null});
        throw error;
      }
    })());
    const results = await Promise.allSettled(work);
    if(current()&&results.every(result=>result.status==='fulfilled'))this.update({metadataErrorCode:null});
    for (const result of results) if (result.status==='rejected' && current()) {
      if (status(result.reason)===401) this.failure(result.reason);
      else this.update({metadataErrorCode:code(result.reason)});
    }
  }
  selectTask = async (taskId: string | null): Promise<void> => {
    const capture = this.capture(), generation = ++this.selection, historyGeneration = ++this.historyLoad;
    this.update({ selectedTaskId: taskId, selectedTask: this.state.tasks.find(task => task.task_id===taskId) ?? null,
      history: [], detailLoading: !!taskId, errorCode: null });
    if (!taskId || !capture.projectId) return;
    try {
      const task = await this.api.getTask(taskId);
      if (!this.scoped(capture) || generation!==this.selection) return;
      if (!snapshotValid(task,capture.projectId) || task.task_id!==taskId) throw new TeamStateError('team_task_response_invalid');
      this.merge(task);
      const canonicalTask = this.state.tasks.find(item=>item.task_id===taskId) ?? immutable(task);
      this.update({selectedTask:canonicalTask});
      if(historyGeneration===this.historyLoad)await this.refreshHistory(taskId,capture,generation);
    } catch(error) { if(this.scoped(capture)&&generation===this.selection&&historyGeneration===this.historyLoad)this.failure(error); }
    finally { if(this.scoped(capture)&&generation===this.selection&&historyGeneration===this.historyLoad)this.update({detailLoading:false}); }
  };
  private async refreshHistory(taskId:string,capture:Capture,selection:number):Promise<void> {
    const generation=++this.historyLoad;
    const current=()=>this.scoped(capture)&&selection===this.selection
      &&this.state.selectedTaskId===taskId&&generation===this.historyLoad;
    if(!current())return;
    this.update({detailLoading:true,errorCode:null});
    try {
      if(typeof this.api.getHistory!=='function')return;
      const entries=new Map<string,TaskHistoryEntry>(),cursors=new Set<string>();let after:string|undefined;
      for(let page=0;page<100;page++) {
        const history=await this.api.getHistory(taskId,{limit:100,after});
        if(!current())return;
        for(const entry of history.items) {
          const existing=entries.get(entry.id);
          if(existing&&canonical(existing)!==canonical(entry))throw new TeamStateError('team_history_invalid');
          if(!existing)entries.set(entry.id,entry);
        }
        if(!history.next_cursor){this.update({history:immutable([...entries.values()])});return;}
        if(cursors.has(history.next_cursor))throw new TeamStateError('team_pagination_invalid');
        cursors.add(history.next_cursor);after=history.next_cursor;
      }
      throw new TeamStateError('team_pagination_incomplete');
    } catch(error) { if(current())this.failure(error); }
    finally { if(current())this.update({detailLoading:false}); }
  }
  setDraft = (patch: Partial<TeamDraft>): void => {
    const changedAction = patch.action !== undefined && patch.action !== this.state.draft.action;
    const changedTarget = Object.hasOwn(patch,'taskId') && patch.taskId !== this.state.draft.taskId;
    const clearValues = patch.values !== undefined && Object.keys(patch.values).length===0;
    const draft = immutable({ ...this.state.draft, ...patch,
      values: changedAction || changedTarget || clearValues ? patch.values ?? {} : {...this.state.draft.values,...patch.values},
      assignee: Object.hasOwn(patch,'assignee') ? patch.assignee ?? null : changedAction || changedTarget ? null : this.state.draft.assignee });
    this.update({ draft, draftVersion: this.state.draftVersion+1, preview:null, errorCode:null });
  };
  dismissPreview = (): void => { this.update({preview:null}); };
  prepare = (): TaskCommand => {
    if (!this.state.actor || !this.state.projectId) throw new TeamStateError('team_session_required');
    const draft = this.state.draft;
    const command = buildTaskCommand({actor:this.state.actor, projectId:this.state.projectId,
      task:draft.taskId ? this.state.tasks.find(task=>task.task_id===draft.taskId) : null,
      action:draft.action,values:draft.values,assignee:draft.assignee,operationId:this.uuid()});
    if(this.previews.has(command.operation_id)||this.operations.has(command.operation_id))throw new TeamStateError('team_operation_duplicate');
    if ([...this.operations.values()].some(op=>payload(op.command)===payload(command)
        && (op.transport==='unknown'||op.transport==='sending'||!op.receipt||['queued','running','reconciling','uncertain'].includes(op.receipt.execution_state.state)))) throw new TeamStateError('team_operation_pending');
    this.previews.set(command.operation_id,{command,capture:this.capture(),draftVersion:this.state.draftVersion});
    this.update({preview:command,errorCode:null});
    return command;
  };
  private failure(error: unknown) {
    if(status(error)===401) { this.setSession(null);this.update({sessionExpired:true}); }
    else this.update({errorCode:code(error)});
  }
  private receive(operationId:string, receipt:CommandReceipt, capture:Capture):CommandReceipt {
    const previous=this.operations.get(operationId);
    if(!previous||receipt.acceptance_receipt.operation_id!==operationId||!Number.isSafeInteger(receipt.execution_state.revision)
        ||receipt.execution_state.revision<0||!['queued','running','reconciling','applied','conflict','uncertain','rejected'].includes(receipt.execution_state.state)
        ||(receipt.current&&(!snapshotValid(receipt.current,previous.command.project_id)
          ||(previous.command.task_id!==null&&previous.command.task_id!==undefined&&receipt.current.task_id!==previous.command.task_id)))) throw new TeamStateError('team_receipt_invalid');
    if(previous.receipt && (canonical(previous.receipt.acceptance_receipt)!==canonical(receipt.acceptance_receipt))) throw new TeamStateError('team_receipt_invalid');
    if(previous.receipt && previous.receipt.execution_state.revision>receipt.execution_state.revision)return previous.receipt;
    const selectedRevision=this.state.selectedTaskId===previous.command.task_id?this.state.selectedTask?.revision??-1:-1;
    const saved=immutable(receipt);
    this.operations.set(operationId,Object.freeze({...previous,receipt:saved,transport:'known',errorCode:null}));
    if(this.scoped(capture)&&previous.command.project_id===this.state.projectId&&saved.current)this.merge(saved.current);
    const preview=this.previews.get(operationId);
    if(this.scoped(capture)&&previous.command.project_id===this.state.projectId&&saved.acceptance_receipt.decision==='accepted'
        && !['conflict','uncertain','rejected'].includes(saved.execution_state.state)) {
      this.update({...(this.state.preview?.operation_id===operationId?{preview:null}:{}),
        ...(preview?.draftVersion===this.state.draftVersion?{draft:emptyDraft(),draftVersion:this.state.draftVersion+1}:{})});
    }
    this.showOperations();
    if((!previous.receipt||saved.execution_state.revision>previous.receipt.execution_state.revision
          ||(this.state.selectedTask?.revision??-1)>selectedRevision)
        &&this.scoped(capture)&&previous.command.project_id===this.state.projectId
        &&previous.command.task_id&&previous.command.task_id===this.state.selectedTaskId)
      void this.refreshHistory(previous.command.task_id,capture,this.selection);
    return saved;
  }
  confirm = (operationId?: string): Promise<CommandReceipt|undefined> => {
    const id=operationId??this.state.preview?.operation_id;
    if(!id)return Promise.reject(new TeamStateError('team_preview_required'));
    if(this.posts.has(id))return this.posts.get(id)!;
    if(this.operations.has(id))return this.poll(id);
    const preview=this.previews.get(id);
    if(!preview||!this.scoped(preview.capture))return Promise.reject(new TeamStateError('team_preview_stale'));
    this.operations.set(id,Object.freeze({command:preview.command,receipt:null,transport:'sending',errorCode:null}));
    this.showOperations();
    const work=this.submit(id,preview.command,preview.capture);
    this.posts.set(id,work);
    return work;
  };
  private async submit(id:string,command:TaskCommand,capture:Capture):Promise<CommandReceipt|undefined> {
    try {
      const receipt=await this.api.postCommand(command);
      if(!this.live(capture))return;
      return this.receive(id,receipt,capture);
    } catch(error) {
      if(!this.live(capture))return;
      const op=this.operations.get(id)!;
      this.operations.set(id,Object.freeze({...op,transport:uncertain(error)?'unknown':'rejected',errorCode:code(error)}));
      this.showOperations();
      if(status(error)===401){this.failure(error);return;}
      if(uncertain(error))return this.poll(id);
    } finally {this.posts.delete(id);}
  }
  poll = (operationId:string):Promise<CommandReceipt|undefined> => {
    if(this.reads.has(operationId))return this.reads.get(operationId)!;
    if(!this.operations.has(operationId))return Promise.reject(new TeamStateError('team_operation_unavailable'));
    const work=this.readReceipt(operationId,this.capture());
    this.reads.set(operationId,work);return work;
  };
  private async readReceipt(id:string,capture:Capture):Promise<CommandReceipt|undefined> {
    try {
      const receipt=await this.api.getReceipt(id);
      if(!this.live(capture))return;
      return this.receive(id,receipt,capture);
    } catch(error) {
      if(!this.live(capture))return;
      const op=this.operations.get(id);
      if(op)this.operations.set(id,Object.freeze({...op,errorCode:op.errorCode==='team_receipt_invalid'?op.errorCode:code(error)}));
      this.showOperations();
      if(status(error)===401)this.failure(error);
    } finally {this.reads.delete(id);}
  }
}

export function createTeamTaskStore(api: TeamApi, options: Options = {}): TeamTaskStore { return new TeamTaskStore(api,options); }
