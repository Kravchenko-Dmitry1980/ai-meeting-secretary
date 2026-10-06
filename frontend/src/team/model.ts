/** Team views and public command construction over the generated gateway DTOs. */
import type { Actor, Bucket, TaskChange, TaskCommand, TaskSnapshot } from './api.ts';
export type { Actor, Bucket, CommandReceipt, TaskChange, TaskCommand, TaskSnapshot } from './api.ts';

export type PublicAction = Exclude<TaskCommand['action'], 'link' | 'resolve_due'>;
export type TaskFilter = 'mine' | 'all';
export interface AssigneeBinding {
  id: string;
  revision: number;
  project_ids: readonly string[];
  display_name?: string;
  enabled?: boolean;
}
export interface TeamDraft {
  action: PublicAction;
  taskId: string | null;
  values: TaskChange;
  assignee: AssigneeBinding | null;
}
export const BUCKETS = ['inbox', 'accepted', 'doing', 'blocked', 'review', 'done', 'cancelled'] as const;
export const BUCKET_LABELS: Record<Bucket, string> = {
  inbox: 'Новая', accepted: 'Принята', doing: 'В работе', blocked: 'Заблокирована',
  review: 'На проверке', done: 'Готово', cancelled: 'Отменена',
};
export const MATRIX_KEYS = ['important-urgent', 'important-not-urgent', 'not-important-urgent', 'not-important-not-urgent', 'unclassified'] as const;
export type MatrixKey = typeof MATRIX_KEYS[number];
export const MATRIX_LABELS: Record<MatrixKey, string> = {
  'important-urgent': 'Важное и срочное', 'important-not-urgent': 'Важное, не срочное',
  'not-important-urgent': 'Не важное, срочное', 'not-important-not-urgent': 'Не важное, не срочное',
  unclassified: 'Не разобрано',
};

export class TeamStateError extends Error {
  readonly code: string;
  constructor(code: string) { super(code); this.name = 'TeamStateError'; this.code = code; }
}

export function freeze<T>(value: T): T {
  if (value && typeof value === 'object') {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}

export function immutable<T>(value: T): T { return freeze(structuredClone(value)); }
export function emptyDraft(): TeamDraft { return freeze({ action: 'create', taskId: null, values: {}, assignee: null }); }
export function compareIDs(left: string, right: string): number {
  return left.length - right.length || (left < right ? -1 : left > right ? 1 : 0);
}
export function matrixKey(task: TaskSnapshot): MatrixKey {
  if (!task.classification_confirmed || typeof task.important !== 'boolean' || typeof task.urgent !== 'boolean') return 'unclassified';
  return `${task.important ? 'important' : 'not-important'}-${task.urgent ? 'urgent' : 'not-urgent'}` as MatrixKey;
}

const active = (task: TaskSnapshot) => task.bucket !== 'done' && task.bucket !== 'cancelled';
const moscowDay = (timestamp: number) => new Date(timestamp + 3 * 60 * 60 * 1000).toISOString().slice(0, 10);

export function taskViews(tasks: readonly TaskSnapshot[], options: { actorId: string; filter: TaskFilter; now: number }) {
  if (!Number.isFinite(options.now)) throw new TeamStateError('team_clock_invalid');
  const selected = tasks.filter(task => options.filter === 'all' || task.assignee_id === options.actorId);
  const board = Object.fromEntries(BUCKETS.map(bucket => [bucket, [] as TaskSnapshot[]])) as Record<Bucket, TaskSnapshot[]>;
  const matrix = Object.fromEntries(MATRIX_KEYS.map(key => [key, [] as TaskSnapshot[]])) as Record<MatrixKey, TaskSnapshot[]>;
  const today: TaskSnapshot[] = [], overdue: TaskSnapshot[] = [], noDue: TaskSnapshot[] = [];
  const day = moscowDay(options.now);
  for (const task of selected) {
    board[task.bucket ?? 'inbox'].push(task);
    if (!active(task)) continue;
    matrix[matrixKey(task)].push(task);
    const due = task.due_confirmed && task.due_at ? Date.parse(task.due_at) : NaN;
    if (!Number.isFinite(due)) { noDue.push(task); continue; }
    if (moscowDay(due) <= day) today.push(task);
    if (due < options.now) overdue.push(task);
  }
  return { tasks: selected, board, matrix, today, overdue, noDue };
}

export function dueLabel(task: TaskSnapshot): string {
  if (!task.due_confirmed) return 'Срок не подтверждён';
  if (!task.due_at) return 'Без срока';
  const date = new Date(task.due_at);
  if (!Number.isFinite(date.getTime())) return 'Срок недоступен';
  return new Intl.DateTimeFormat('ru-RU', { timeZone: 'Europe/Moscow', day: '2-digit', month: '2-digit',
    year: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date) + ' МСК';
}

export function completionValues(task: TaskSnapshot, result: string, actor: Actor): TaskChange {
  text(result);
  const review = task.important !== false || !task.classification_confirmed;
  return { bucket: review && !(actor.role === 'owner' && task.bucket === 'review') ? 'review' : 'done', result };
}

const DECIMAL = /^[1-9][0-9]{0,127}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const DIGEST = /^[0-9a-f]{64}$/;
const DUE = ['due_at', 'due_phrase', 'due_timezone', 'due_confirmed'];
const AXES = ['important', 'urgent', 'classification_confirmed'];
const ALLOWED: Record<PublicAction, readonly string[]> = {
  create: ['title', 'description', 'assignee_id', ...DUE, ...AXES], set_state: ['bucket', 'result'],
  assign: ['assignee_id'], set_due: [...DUE, 'reason'], propose_due: [...DUE, 'reason'],
  classify: AXES, comment: ['comment'], rename: ['title'],
};

function text(value: unknown): asserts value is string {
  if (typeof value !== 'string' || !value.trim() || value.length > 4000 || value.includes('\0')) throw new TeamStateError('team_text_required');
}
function revision(value: unknown): asserts value is number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) throw new TeamStateError('team_revision_invalid');
}

export function buildTaskCommand(input: {
  actor: Actor; projectId: string; task?: TaskSnapshot | null; action: PublicAction;
  values: TaskChange; assignee?: AssigneeBinding | null; operationId: string;
}): TaskCommand {
  const { actor, projectId, task, action, assignee, operationId } = input;
  if (!DECIMAL.test(projectId) || !actor.project_ids.includes(projectId)) throw new TeamStateError('team_scope_invalid');
  if (!UUID.test(operationId) || !UUID.test(actor.id)) throw new TeamStateError('team_identity_invalid');
  revision(actor.revision);
  if (!ALLOWED[action]) throw new TeamStateError('team_action_invalid');
  const values = Object.fromEntries(Object.entries(input.values).filter(([, value]) => value !== undefined)) as TaskChange;
  if (Object.keys(values).some(key => !ALLOWED[action].includes(key))) throw new TeamStateError('team_action_fields_invalid');
  if (action !== 'create') {
    if (!task || !DECIMAL.test(task.task_id) || task.project_id !== projectId || !DIGEST.test(task.remote_fingerprint)) throw new TeamStateError('team_task_required');
    revision(task.revision);
  } else if (task) throw new TeamStateError('team_create_has_task');
  if (actor.role !== 'owner') {
    if (!['set_state', 'comment', 'propose_due'].includes(action)) throw new TeamStateError('team_owner_required');
    if (action !== 'comment' && task?.assignee_id !== actor.id) throw new TeamStateError('team_task_not_assigned');
    if (action === 'set_state' && (values.bucket === 'inbox' || values.bucket === 'cancelled' || !active(task!))) throw new TeamStateError('team_owner_required');
    if (action === 'set_state' && values.bucket === 'done' && (task!.important !== false || !task!.classification_confirmed)) throw new TeamStateError('team_owner_review_required');
  }
  let assigneeRevision: number | null = null;
  if (action === 'create' || action === 'assign') {
    if (!assignee || !UUID.test(assignee.id) || assignee.enabled === false || assignee.id !== values.assignee_id
        || !assignee.project_ids.includes(projectId)) throw new TeamStateError('team_assignee_required');
    revision(assignee.revision);
    assigneeRevision = assignee.revision;
  }
  if (action === 'create' || action === 'rename') text(values.title);
  if (action === 'comment') text(values.comment);
  if (action === 'set_state') {
    if (!BUCKETS.includes(values.bucket as Bucket)) throw new TeamStateError('team_bucket_required');
    if (values.bucket === 'done') text(values.result);
  }
  if (action === 'classify' || values.classification_confirmed === true) {
    if (typeof values.important !== 'boolean' || typeof values.urgent !== 'boolean' || values.classification_confirmed !== true) throw new TeamStateError('team_classification_required');
  }
  if (action === 'set_due' || action === 'propose_due') {
    text(values.reason);
    if (!Object.hasOwn(values, 'due_at')) throw new TeamStateError('team_due_required');
  }
  if (Object.hasOwn(values, 'due_at')) {
    if (values.due_confirmed !== true || (values.due_at !== null && (typeof values.due_at !== 'string'
        || !/(?:Z|[+-]\d{2}:\d{2})$/.test(values.due_at) || !Number.isFinite(Date.parse(values.due_at))))) throw new TeamStateError('team_due_confirmation_required');
  }
  if (values.due_timezone != null && values.due_timezone !== 'Europe/Moscow') throw new TeamStateError('team_due_timezone_invalid');
  return immutable({ operation_id: operationId, project_id: projectId, task_id: task?.task_id ?? null,
    expected_revision: task?.revision ?? null, expected_fingerprint: task?.remote_fingerprint ?? null,
    ...(assigneeRevision !== null ? { expected_assignee_revision: assigneeRevision } : {}), action, values });
}
