import type { components } from '../generated/team-api';

export type Actor = components['schemas']['Actor'];
export type SessionView = components['schemas']['SessionView'];
export type TaskSnapshot = components['schemas']['TaskSnapshot'];
export type TaskPage = components['schemas']['TaskPage'];
// The shared server DTO has a default timezone in output. Commands preserve
// intentional field absence instead of injecting that default into every write.
export type TaskChange = Omit<components['schemas']['TaskChange'], 'due_timezone'> & { due_timezone?: 'Europe/Moscow' };
export type TaskCommand = Omit<components['schemas']['TaskCommand'], 'values'> & { values: TaskChange };
export type TaskOrigin = components['schemas']['TaskOrigin'];
export type CommandReceipt = components['schemas']['CommandReceipt'];
export type MemberView = components['schemas']['MemberView'];
export type MemberPage = components['schemas']['MemberPage'];
export type TaskHistoryEntry = Omit<components['schemas']['TaskHistoryEntry'], 'values'> & { values?: TaskChange | null };
export type TaskHistoryPage = Omit<components['schemas']['TaskHistoryPage'], 'items'> & { items: TaskHistoryEntry[] };
export type TeamStatusView = components['schemas']['TeamStatusView'];
export type OwnerDashboardView = components['schemas']['OwnerDashboardView'];
export type OwnerCommandItem = components['schemas']['OwnerCommandItem'];
export type OwnerCommandPage = components['schemas']['OwnerCommandPage'];
export type DashboardDeliverySummary = components['schemas']['DashboardDeliverySummary'];
export type DashboardNotificationSummary = components['schemas']['DashboardNotificationSummary'];
export type DashboardWebhookStatus = components['schemas']['DashboardWebhookStatus'];
export type DueResolutionCandidate = components['schemas']['DueResolutionCandidate'];
export type DueResolutionPreview = components['schemas']['DueResolutionPreview'];
export type DueResolutionRequest = components['schemas']['DueResolutionRequest'];
export type Bucket = NonNullable<TaskSnapshot['bucket']>;
export type TaskListOptions = { after?: string; limit?: number; mine?: boolean; bucket?: Bucket; signal?: AbortSignal };
export type ReadOptions = { signal?: AbortSignal };
export type ReadPageOptions = ReadOptions & { after?: string; limit?: number };
export type SessionListener = (actor: Actor | null, epoch: number) => void;

export interface TeamApi {
  readonly actor: Actor | null;
  readonly sessionEpoch: number;
  subscribeSession(listener: SessionListener): () => void;
  clearSession(): void;
  me(options?: ReadOptions): Promise<SessionView>;
  loginMax(initData: string): Promise<SessionView>;
  loginCode(value: string): Promise<SessionView>;
  logout(): Promise<void>;
  listTasks(projectId: string, options?: TaskListOptions): Promise<TaskPage>;
  getTask(taskId: string, options?: ReadOptions): Promise<TaskSnapshot>;
  postCommand(command: TaskCommand): Promise<CommandReceipt>;
  getReceipt(operationId: string, options?: ReadOptions): Promise<CommandReceipt>;
  listMembers(projectId: string, options?: ReadPageOptions): Promise<MemberPage>;
  getHistory(taskId: string, options?: ReadPageOptions): Promise<TaskHistoryPage>;
  getStatus(projectId: string, options?: ReadOptions): Promise<TeamStatusView>;
  getDashboard(projectId: string, options?: ReadPageOptions): Promise<OwnerDashboardView>;
  getDueResolution(taskId: string, options?: ReadOptions): Promise<DueResolutionCandidate>;
  createDueResolutionPreview(candidate: DueResolutionCandidate, choice: Pick<DueResolutionRequest, 'due_at' | 'reason'>): Promise<DueResolutionPreview>;
  confirmDueResolution(preview: DueResolutionPreview, operationId: string): Promise<CommandReceipt>;
}

const ROOT = '/api/team/v1';
const BUCKETS = ['inbox', 'accepted', 'doing', 'blocked', 'review', 'done', 'cancelled'];
const ACTIONS = ['create', 'set_state', 'assign', 'set_due', 'classify', 'comment', 'propose_due', 'rename'];
const STATES = ['queued', 'running', 'reconciling', 'applied', 'conflict', 'uncertain', 'rejected'];
const encoder = new TextEncoder();
type RecordValue = Record<string, unknown>;

export class TeamApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly uncertain: boolean;
  constructor(code: string, status: number, uncertain = false) {
    super(uncertain ? 'Результат отправки не подтверждён. Проверьте состояние перед повторным действием.'
      : status === 401 ? 'Сеанс завершён. Войдите снова; неподтверждённые изменения нужно проверить.'
        : status === 403 ? 'Действие недоступно в текущем проекте.'
          : status === 409 ? 'Карточка изменилась. Обновите данные перед подтверждением.'
            : status === 429 ? 'Слишком много запросов. Повторите позже.'
              : status === 503 || status === 0 ? 'Сервис недоступен или запрос некорректен. Повторите проверку позже.'
                : 'Сервис вернул некорректный ответ. Изменение не подтверждено.');
    this.name = 'TeamApiError';
    this.code = /^[a-z][a-z0-9_]{0,100}$/.test(code) ? code : 'team_request_failed';
    this.status = status;
    this.uncertain = uncertain;
  }
}

function record(value: unknown): value is RecordValue {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
function shape(value: unknown, required: string[], optional: string[] = []): value is RecordValue {
  return record(value) && required.every((key) => Object.hasOwn(value, key))
    && Object.keys(value).every((key) => required.includes(key) || optional.includes(key));
}
function text(value: unknown, max: number, empty = false, bytes = max * 4): value is string {
  if (typeof value !== 'string' || (!empty && !value.length) || value.length > max || value.includes('\0')) return false;
  // TextEncoder replaces lone surrogates. Reject them rather than changing an
  // exact decimal identifier, credential or source phrase silently.
  for (let i = 0; i < value.length; i += 1) {
    const unit = value.charCodeAt(i);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = value.charCodeAt(++i);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) return false;
  }
  return encoder.encode(value).length <= bytes;
}
function decimal(value: unknown): value is string { return typeof value === 'string' && /^[1-9][0-9]{0,127}$/.test(value); }
function uuid(value: unknown): value is string { return typeof value === 'string' && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value); }
function revision(value: unknown): value is number { return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0; }
function digest(value: unknown) { return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value); }
function date(value: unknown): value is string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:[Zz]|[+-]\d{2}:\d{2})$/.test(value)) return false;
  const y = +value.slice(0, 4), m = +value.slice(5, 7), d = +value.slice(8, 10);
  const day = new Date(Date.UTC(y, m - 1, d));
  return day.getUTCFullYear() === y && day.getUTCMonth() === m - 1 && day.getUTCDate() === d
    && +value.slice(11, 13) <= 23 && +value.slice(14, 16) <= 59 && +value.slice(17, 19) <= 59
    && Number.isFinite(Date.parse(value));
}
const optional = (value: RecordValue, key: string, check: (value: unknown) => boolean) => !Object.hasOwn(value, key) || check(value[key]);
const nullable = (check: (value: unknown) => boolean) => (value: unknown) => value === null || check(value);
const boolean = (value: unknown) => typeof value === 'boolean';
const enumValue = (values: string[]) => (value: unknown) => typeof value === 'string' && values.includes(value);
const errorCode = nullable((value: unknown) => typeof value === 'string' && /^[a-z][a-z0-9_]{0,100}$/.test(value));
const readCode = (value: unknown) => typeof value === 'string' && /^[a-z][a-z0-9_]{0,127}$/.test(value);
const historyCursor = (value: unknown) => typeof value === 'string' && /^[A-Za-z0-9_-]{1,512}$/.test(value);
const rubles = (value: unknown) => typeof value === 'string' && /^[0-9]+\.[0-9]{6}$/.test(value);

function validActor(value: unknown): value is Actor {
  return shape(value, ['id', 'display_name', 'role', 'project_ids', 'revision']) && uuid(value.id)
    && text(value.display_name, 4000) && ['owner', 'member'].includes(String(value.role))
    && Array.isArray(value.project_ids)
    && value.project_ids.every(decimal) && new Set(value.project_ids).size === value.project_ids.length
    && revision(value.revision);
}
function validSession(value: unknown): value is SessionView {
  return shape(value, ['actor', 'csrf']) && validActor(value.actor)
    && typeof value.csrf === 'string' && /^[A-Za-z0-9_-]{32,256}$/.test(value.csrf);
}
function validOrigin(value: unknown) {
  if (!shape(value, [], ['source_kind', 'publication_id', 'meeting_id', 'transcript_version', 'summary_version', 'action_id'])) return false;
  return optional(value, 'source_kind', enumValue(['manual', 'meeting', 'max']))
    && optional(value, 'publication_id', nullable(uuid))
    && ['meeting_id', 'action_id'].every((key) => optional(value, key, nullable((x) => text(x, 4000))))
    && ['transcript_version', 'summary_version'].every((key) => optional(value, key, nullable(revision)));
}
function validTask(value: unknown): value is TaskSnapshot {
  return shape(value, ['task_id', 'project_id', 'revision', 'remote_fingerprint', 'title'],
    ['description', 'assignee_id', 'bucket', 'important', 'urgent', 'classification_confirmed',
      'due_at', 'due_phrase', 'due_timezone', 'due_confirmed', 'origin'])
    && decimal(value.task_id) && decimal(value.project_id) && revision(value.revision)
    && digest(value.remote_fingerprint) && text(value.title, 4000) && /\S/.test(value.title)
    && optional(value, 'description', (x) => text(x, 1024 * 1024, true, 1024 * 1024))
    && optional(value, 'assignee_id', nullable(uuid)) && optional(value, 'bucket', enumValue(BUCKETS))
    && ['important', 'urgent'].every((key) => optional(value, key, nullable(boolean)))
    && ['classification_confirmed', 'due_confirmed'].every((key) => optional(value, key, boolean))
    && optional(value, 'due_at', nullable(date)) && optional(value, 'due_phrase', nullable((x) => text(x, 4000, true)))
    && optional(value, 'due_timezone', (x) => x === 'Europe/Moscow') && optional(value, 'origin', nullable(validOrigin));
}
function validPage(value: unknown): value is TaskPage {
  return shape(value, ['items'], ['next_cursor']) && Array.isArray(value.items)
    && value.items.length <= 100 && value.items.every(validTask)
    && new Set(value.items.map((item) => item.task_id)).size === value.items.length
    && optional(value, 'next_cursor', nullable(decimal));
}
function validMemberPage(value: unknown): value is MemberPage {
  return shape(value, ['items'], ['next_cursor']) && Array.isArray(value.items) && value.items.length <= 100
    && value.items.every((item) => shape(item, ['id', 'display_name', 'revision']) && uuid(item.id)
      && text(item.display_name, 4000) && /\S/.test(item.display_name) && revision(item.revision))
    && new Set(value.items.map((item) => item.id)).size === value.items.length
    && optional(value, 'next_cursor', nullable(uuid));
}
function validHistoryEntry(value: unknown): value is TaskHistoryEntry {
  return shape(value, ['id', 'kind', 'event', 'recorded_at'], ['operation_id', 'actor_id', 'actor_display_name', 'action',
    'execution_state', 'verified_at', 'error_code', 'changed_fields', 'before_fingerprint', 'after_fingerprint', 'remote_occurred_at', 'values'])
    && text(value.id, 128) && enumValue(['command', 'external_change'])(value.kind)
    && readCode(value.event) && date(value.recorded_at)
    && ['operation_id', 'actor_id'].every((key) => optional(value, key, nullable(uuid)))
    && optional(value, 'actor_display_name', nullable((x) => text(x, 4000) && /\S/.test(x)))
    && ['action', 'error_code'].every((key) => optional(value, key, nullable(readCode)))
    && optional(value, 'execution_state', nullable(enumValue(STATES)))
    && optional(value, 'verified_at', nullable(date))
    && optional(value, 'changed_fields', (x) => Array.isArray(x) && x.every(readCode))
    && ['before_fingerprint', 'after_fingerprint'].every((key) => optional(value, key, nullable(digest)))
    && optional(value, 'remote_occurred_at', (x) => x === null)
    && optional(value, 'values', nullable(validChange))
    && (value.kind === 'command' || value.values == null);
}
function validHistoryPage(value: unknown): value is TaskHistoryPage {
  return shape(value, ['items'], ['next_cursor']) && Array.isArray(value.items) && value.items.length <= 100
    && value.items.every(validHistoryEntry) && new Set(value.items.map((item) => item.id)).size === value.items.length
    && optional(value, 'next_cursor', nullable(historyCursor));
}
function validStatus(value: unknown): value is TeamStatusView {
  if (!shape(value, ['project_id', 'sync', 'cloud']) || !decimal(value.project_id)) return false;
  const sync = value.sync, cloud = value.cloud;
  return shape(sync, ['state'], ['last_attempt_at', 'last_successful_sync_at', 'last_command_verified_at', 'error_code', 'issue_count'])
    && enumValue(['not_configured', 'never_synced', 'syncing', 'ready', 'degraded'])(sync.state)
    && ['last_attempt_at', 'last_successful_sync_at', 'last_command_verified_at'].every((key) => optional(sync, key, nullable(date)))
    && optional(sync, 'error_code', nullable(readCode)) && optional(sync, 'issue_count', revision)
    && (sync.state !== 'ready' || date(sync.last_successful_sync_at))
    && shape(cloud, ['state'], ['paused_reason', 'spend_rub', 'reserved_rub', 'remaining_rub', 'effective_budget_rub'])
    && enumValue(['not_configured', 'ready', 'paused', 'unavailable'])(cloud.state)
    && optional(cloud, 'paused_reason', nullable(readCode))
    && ['spend_rub', 'reserved_rub', 'remaining_rub', 'effective_budget_rub'].every((key) => optional(cloud, key, nullable(rubles)));
}
function validDashboard(value: unknown): value is OwnerDashboardView {
  if (!shape(value, ['project_id', 'status', 'commands', 'deliveries', 'notifications', 'webhook'])
    || !decimal(value.project_id) || !validStatus(value.status) || value.status.project_id !== value.project_id) return false;
  const commands = value.commands, deliveries = value.deliveries, notifications = value.notifications, webhook = value.webhook;
  if (!shape(commands, ['items', 'next_cursor']) || !Array.isArray(commands.items) || commands.items.length > 100
    || !nullable(historyCursor)(commands.next_cursor) || commands.next_cursor !== null && !commands.items.length
    || !commands.items.every((item) => shape(item, ['operation_id', 'task_id', 'action', 'state', 'accepted_at',
      'last_state_at', 'lease_until', 'error_code']) && uuid(item.operation_id) && nullable(decimal)(item.task_id)
      && enumValue([...ACTIONS, 'link', 'resolve_due'])(item.action) && enumValue(['queued', 'running', 'reconciling', 'uncertain'])(item.state)
      && date(item.accepted_at) && nullable(date)(item.last_state_at) && nullable(date)(item.lease_until)
      && nullable(readCode)(item.error_code))
    || new Set(commands.items.map((item) => item.operation_id)).size !== commands.items.length) return false;
  const states = ['not_configured', 'observed', 'issues', 'unavailable'];
  const deliveryCounts = ['pending_count', 'sending_count', 'retryable_count', 'uncertain_count', 'rejected_count', 'max_api_accepted_count'];
  const notificationCounts = ['pending_count', 'sending_count', 'uncertain_count', 'failed_count', 'cancelled_count'];
  if (!shape(deliveries, ['state', 'sources', ...deliveryCounts, 'last_max_api_accepted_at', 'human_read_confirmed'])
    || !enumValue(states)(deliveries.state) || !Array.isArray(deliveries.sources) || deliveries.sources.length > 2
    || !deliveries.sources.every(enumValue(['bot', 'voice'])) || new Set(deliveries.sources).size !== deliveries.sources.length
    || !deliveryCounts.every((key) => nullable(revision)(deliveries[key]))
    || !nullable(date)(deliveries.last_max_api_accepted_at) || deliveries.human_read_confirmed !== null) return false;
  const absentDelivery = ['not_configured', 'unavailable'].includes(String(deliveries.state));
  if (absentDelivery ? deliveryCounts.some((key) => deliveries[key] !== null) || deliveries.last_max_api_accepted_at !== null
    || deliveries.state === 'not_configured' && deliveries.sources.length > 0
    : !deliveries.sources.length || deliveryCounts.some((key) => deliveries[key] === null)) return false;
  if (!absentDelivery && (deliveries.state === 'issues') !== Boolean(deliveries.retryable_count || deliveries.uncertain_count || deliveries.rejected_count)
    || deliveries.last_max_api_accepted_at !== null && !deliveries.max_api_accepted_count) return false;
  if (!shape(notifications, ['state', ...notificationCounts, 'last_max_api_accepted_at', 'human_read_confirmed'])
    || !enumValue(states)(notifications.state) || !notificationCounts.every((key) => nullable(revision)(notifications[key]))
    || !nullable(date)(notifications.last_max_api_accepted_at) || notifications.human_read_confirmed !== null) return false;
  const absentNotifications = ['not_configured', 'unavailable'].includes(String(notifications.state));
  if (absentNotifications ? notificationCounts.some((key) => notifications[key] !== null)
    || notifications.last_max_api_accepted_at !== null : notificationCounts.some((key) => notifications[key] === null)) return false;
  if (!absentNotifications && (notifications.state === 'issues') !== Boolean(notifications.uncertain_count || notifications.failed_count)) return false;
  return shape(webhook, ['state', 'last_authenticated_accept_at', 'configured_subscription', 'subscription_state',
    'last_subscription_observed_at', 'last_trusted_callback_at', 'phone_delivery'])
    && enumValue(['not_configured', 'unknown', 'unavailable', 'observed', 'issues'])(webhook.state)
    && boolean(webhook.configured_subscription)
    && nullable(enumValue(['present', 'missing', 'unavailable', 'verified', 'rejected', 'uncertain']))(webhook.subscription_state)
    && ['last_authenticated_accept_at', 'last_subscription_observed_at', 'last_trusted_callback_at'].every(key => nullable(date)(webhook[key]))
    && webhook.phone_delivery === 'unknown'
    && (webhook.state !== 'not_configured' || webhook.last_authenticated_accept_at === null);
}
function validReceipt(value: unknown): value is CommandReceipt {
  if (!shape(value, ['acceptance_receipt', 'execution_state'], ['current'])) return false;
  const accepted = value.acceptance_receipt, state = value.execution_state;
  if (!shape(accepted, ['operation_id', 'payload_hash', 'decision', 'decided_at'], ['error_code'])
    || !uuid(accepted.operation_id) || !digest(accepted.payload_hash)
    || !enumValue(['accepted', 'rejected'])(accepted.decision) || !date(accepted.decided_at)
    || !optional(accepted, 'error_code', errorCode)
    || !shape(state, ['state'], ['revision', 'task_id', 'verified_at', 'error_code', 'retry_after'])
    || !enumValue(STATES)(state.state) || !optional(state, 'revision', revision)
    || !optional(state, 'task_id', nullable(decimal)) || !optional(state, 'verified_at', nullable(date))
    || !optional(state, 'error_code', errorCode) || !optional(state, 'retry_after', nullable(revision))
    || !optional(value, 'current', nullable(validTask))) return false;
  // An accepted queue item is not a verified write. Even a nominal applied
  // response needs the actual timestamp and matching task postcondition.
  return state.state !== 'applied' || accepted.decision === 'accepted' && date(state.verified_at)
    && validTask(value.current) && state.task_id === value.current.task_id;
}
const utcDate = (value: unknown): value is string => date(value) && /(?:Z|\+00:00)$/i.test(value);
const sameDate = (left: string | null, right: string | null) => left === null || right === null ? left === right : Date.parse(left) === Date.parse(right);
function validDueCandidate(value: unknown): value is DueResolutionCandidate {
  return shape(value, ['observation_id', 'project_id', 'task_id', 'baseline_revision', 'baseline_fingerprint',
    'observed_fingerprint', 'baseline_due_at', 'observed_due_at', 'title']) && uuid(value.observation_id)
    && decimal(value.project_id) && decimal(value.task_id) && revision(value.baseline_revision)
    && digest(value.baseline_fingerprint) && digest(value.observed_fingerprint)
    && nullable(utcDate)(value.baseline_due_at) && nullable(utcDate)(value.observed_due_at)
    && text(value.title, 4000) && /\S/.test(value.title);
}
function validDuePreview(value: unknown): value is DueResolutionPreview {
  return shape(value, ['preview_id', 'candidate', 'due_at', 'reason', 'created_at', 'expires_at'])
    && uuid(value.preview_id) && validDueCandidate(value.candidate) && nullable(utcDate)(value.due_at)
    && text(value.reason, 4000) && /\S/.test(value.reason) && utcDate(value.created_at) && utcDate(value.expires_at)
    && Date.parse(value.expires_at) > Date.parse(value.created_at);
}
function validCommand(value: unknown): value is TaskCommand {
  if (!shape(value, ['operation_id', 'project_id', 'action', 'values'],
    ['task_id', 'expected_revision', 'expected_fingerprint', 'expected_assignee_revision'])
    || !uuid(value.operation_id) || !decimal(value.project_id) || !enumValue(ACTIONS)(value.action)
    || !optional(value, 'task_id', nullable(decimal)) || !optional(value, 'expected_revision', nullable(revision))
    || !optional(value, 'expected_fingerprint', nullable(digest)) || !optional(value, 'expected_assignee_revision', nullable(revision))) return false;
  return validChange(value.values)
    && (!['create', 'assign'].includes(String(value.action)) || revision(value.expected_assignee_revision));
}
function validChange(v: unknown): v is TaskChange {
  if (!shape(v, [], ['title', 'description', 'assignee_id', 'bucket', 'important', 'urgent',
    'classification_confirmed', 'due_at', 'due_phrase', 'due_timezone', 'due_confirmed', 'reason', 'result', 'comment'])) return false;
  return ['title', 'reason', 'result', 'comment'].every((key) => optional(v, key, nullable((x) => text(x, 4000) && /\S/.test(x))))
    && optional(v, 'description', nullable((x) => text(x, 256 * 1024, true, 256 * 1024)))
    && optional(v, 'assignee_id', nullable(uuid)) && optional(v, 'bucket', nullable(enumValue(BUCKETS)))
    && ['important', 'urgent', 'classification_confirmed', 'due_confirmed'].every((key) => optional(v, key, nullable(boolean)))
    && optional(v, 'due_at', nullable(date)) && optional(v, 'due_phrase', nullable((x) => text(x, 4000, true)))
    && optional(v, 'due_timezone', (x) => x === 'Europe/Moscow');
}
function freeze<T>(value: T): T {
  if (value !== null && typeof value === 'object') {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}

async function discardBody(response: Response): Promise<void> {
  try { await response.body?.cancel(); } catch { /* Never expose a stream's diagnostic text. */ }
}
async function readJson(response: Response, maximum: number, uncertain: boolean): Promise<unknown> {
  const fail = () => new TeamApiError('team_response_invalid', response.status, uncertain);
  if (!/^application\/json(?:\s*;[^\r\n]*)?$/i.test(response.headers.get('Content-Type') ?? '')) {
    await discardBody(response);
    throw fail();
  }
  const declared = response.headers.get('Content-Length');
  if (declared !== null && (!/^[0-9]+$/.test(declared) || +declared > maximum)) {
    await discardBody(response);
    throw fail();
  }
  if (!response.body) throw fail();
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      size += next.value.byteLength;
      if (size > maximum) { await reader.cancel(); throw fail(); }
      chunks.push(next.value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)) as unknown;
  } catch { throw fail(); }
  finally { reader.releaseLock(); }
}

export function createTeamApi(options: { fetcher?: typeof fetch; onSessionLost?: () => void } = {}): TeamApi {
  const fetcher = options.fetcher ?? globalThis.fetch.bind(globalThis);
  let actor: Actor | null = null, csrf: string | null = null, epoch = 0, authBusy = false;
  const listeners = new Set<SessionListener>();
  const notify = () => { for (const listener of listeners) listener(actor, epoch); };
  const clearSession = () => { actor = null; csrf = null; epoch += 1; notify(); options.onSessionLost?.(); };
  const setSession = (value: SessionView) => {
    if (JSON.stringify(actor) !== JSON.stringify(value.actor)) epoch += 1;
    actor = freeze(value.actor); csrf = value.csrf; notify();
    return freeze(value);
  };
  const requireOwner = () => {
    if (!actor || !csrf) throw new TeamApiError('team_authentication_required', 401);
    if (actor.role !== 'owner') throw new TeamApiError('team_owner_required', 403);
    return { actor, csrf };
  };
  async function request<T>(path: string, method: string, valid: (value: unknown) => value is T,
    settings: { body?: string; status?: number; maximum?: number; signal?: AbortSignal; csrf?: string; ignoreEpoch?: boolean } = {}): Promise<T> {
    const startedEpoch = epoch;
    const mutation = method !== 'GET';
    const headers = new Headers({ Accept: 'application/json' });
    if (settings.body !== undefined) headers.set('Content-Type', 'application/json');
    if (settings.csrf) headers.set('X-CSRF-Token', settings.csrf);
    const abort = new AbortController();
    const externalAbort = () => abort.abort();
    if (settings.signal?.aborted) abort.abort();
    settings.signal?.addEventListener('abort', externalAbort, { once: true });
    const timer = setTimeout(() => abort.abort(), 10_000);
    try {
      let response: Response;
      try {
        response = await fetcher(ROOT + path, { method, headers, body: settings.body, credentials: 'same-origin',
          cache: 'no-store', redirect: 'error', signal: abort.signal, referrerPolicy: 'no-referrer' });
      } catch {
        throw new TeamApiError(settings.signal?.aborted ? 'team_request_cancelled' : 'team_transport_unavailable', 0, mutation);
      }
      if (response.status === 401 && startedEpoch === epoch) clearSession();
      if (!response.ok) {
        await discardBody(response);
        const code = response.status === 401 ? 'team_authentication_required' : response.status === 403 ? 'team_scope_forbidden'
          : response.status === 409 ? 'team_command_conflict' : response.status === 429 ? 'team_rate_limited' : 'team_service_unavailable';
        throw new TeamApiError(code, response.status, mutation && (response.status >= 500 || response.status === 408));
      }
      if (!settings.ignoreEpoch && startedEpoch !== epoch) {
        await discardBody(response);
        throw new TeamApiError('team_session_changed', 401);
      }
      if (response.redirected || response.status !== (settings.status ?? 200)) {
        await discardBody(response);
        throw new TeamApiError('team_response_invalid', response.status, mutation);
      }
      if (response.status === 204) return undefined as T;
      const value = await readJson(response, settings.maximum ?? 256 * 1024, mutation);
      if (!settings.ignoreEpoch && startedEpoch !== epoch) throw new TeamApiError('team_session_changed', 401);
      if (!valid(value)) throw new TeamApiError('team_response_invalid', response.status, mutation);
      return freeze(value);
    } finally {
      clearTimeout(timer);
      settings.signal?.removeEventListener('abort', externalAbort);
    }
  }
  async function login(path: string, body: Record<string, string>): Promise<SessionView> {
    if (authBusy) throw new TeamApiError('team_auth_busy', 0);
    authBusy = true;
    clearSession(); // Fence late responses from a previous account before the credential POST.
    try { return setSession(await request(path, 'POST', validSession, { body: JSON.stringify(body), maximum: 16384 })); }
    finally { authBusy = false; }
  }
  return {
    get actor() { return actor; },
    get sessionEpoch() { return epoch; },
    subscribeSession(listener) { listeners.add(listener); return () => { listeners.delete(listener); }; },
    clearSession,
    async me(settings = {}) { return setSession(await request('/me', 'GET', validSession, { ...settings, maximum: 16384 })); },
    async loginMax(initData) {
      if (!text(initData, 16384, false, 16384)) throw new TeamApiError('team_init_data_invalid', 0);
      return login('/session/max', { init_data: initData });
    },
    async loginCode(value) {
      if (!text(value, 1024)) throw new TeamApiError('team_code_invalid', 0);
      return login('/session/code', { value });
    },
    async logout() {
      if (authBusy) throw new TeamApiError('team_auth_busy', 0);
      const token = csrf;
      clearSession();
      if (!token) return;
      authBusy = true;
      try { await request('/session', 'DELETE', (value): value is undefined => value === undefined, { status: 204, csrf: token, ignoreEpoch: true }); }
      finally { authBusy = false; }
    },
    async listTasks(projectId, settings = {}) {
      const { after, limit = 50, mine = false, bucket, signal } = settings;
      if (!decimal(projectId) || after !== undefined && !decimal(after) || !revision(limit) || limit < 1 || limit > 100
        || typeof mine !== 'boolean' || bucket !== undefined && !BUCKETS.includes(bucket)) throw new TeamApiError('team_request_invalid', 0);
      const query = new URLSearchParams({ project_id: projectId, limit: String(limit) });
      if (after !== undefined) query.set('after', after);
      if (mine) query.set('mine', 'true');
      if (bucket !== undefined) query.set('bucket', bucket);
      return request('/tasks?' + query.toString(), 'GET', (value): value is TaskPage => validPage(value)
        && value.items.every((item) => item.project_id === projectId), { signal, maximum: 4 * 1024 * 1024 });
    },
    async getTask(taskId, settings = {}) {
      if (!decimal(taskId)) throw new TeamApiError('team_request_invalid', 0);
      return request('/tasks/' + taskId, 'GET', (value): value is TaskSnapshot => validTask(value) && value.task_id === taskId,
        { ...settings, maximum: 1024 * 1024 });
    },
    async postCommand(command) {
      let body: string, captured: unknown;
      try { body = JSON.stringify(command); captured = JSON.parse(body) as unknown; }
      catch { throw new TeamApiError('team_request_invalid', 0); }
      if (encoder.encode(body).length > 256 * 1024 || !validCommand(captured)) throw new TeamApiError('team_request_invalid', 0);
      if (!csrf || !actor) throw new TeamApiError('team_authentication_required', 401);
      return request('/commands', 'POST', (value): value is CommandReceipt => validReceipt(value)
        && value.acceptance_receipt.operation_id === captured.operation_id
        && (value.current == null || value.current.project_id === captured.project_id), { body, status: 202, csrf });
    },
    async getReceipt(operationId, settings = {}) {
      if (!uuid(operationId)) throw new TeamApiError('team_request_invalid', 0);
      return request('/commands/' + operationId, 'GET', (value): value is CommandReceipt => validReceipt(value)
        && value.acceptance_receipt.operation_id === operationId, settings);
    },
    async listMembers(projectId, settings = {}) {
      const { after, limit = 50, signal } = settings;
      if (!decimal(projectId) || after !== undefined && !uuid(after) || !revision(limit) || limit < 1 || limit > 100)
        throw new TeamApiError('team_request_invalid', 0);
      const query = new URLSearchParams({ project_id: projectId, limit: String(limit) });
      if (after !== undefined) query.set('after', after);
      return request('/members?' + query.toString(), 'GET', validMemberPage, { signal, maximum: 2 * 1024 * 1024 });
    },
    async getHistory(taskId, settings = {}) {
      const { after, limit = 50, signal } = settings;
      if (!decimal(taskId) || after !== undefined && !historyCursor(after) || !revision(limit) || limit < 1 || limit > 100)
        throw new TeamApiError('team_request_invalid', 0);
      const query = new URLSearchParams({ limit: String(limit) });
      if (after !== undefined) query.set('after', after);
      return request('/tasks/' + taskId + '/history?' + query.toString(), 'GET', validHistoryPage,
        { signal, maximum: 2 * 1024 * 1024 });
    },
    async getStatus(projectId, settings = {}) {
      if (!decimal(projectId)) throw new TeamApiError('team_request_invalid', 0);
      return request('/status?' + new URLSearchParams({ project_id: projectId }).toString(), 'GET',
        (value): value is TeamStatusView => validStatus(value) && value.project_id === projectId
          && (actor?.role !== 'member' || ['spend_rub', 'reserved_rub', 'remaining_rub', 'effective_budget_rub']
            .every((key) => (value.cloud as RecordValue)[key] == null)), { ...settings, maximum: 16384 });
    },
    async getDashboard(projectId, settings = {}) {
      const { after, limit = 50, signal } = settings;
      if (!decimal(projectId) || after !== undefined && !historyCursor(after) || !revision(limit) || limit < 1 || limit > 100)
        throw new TeamApiError('team_request_invalid', 0);
      const query = new URLSearchParams({ project_id: projectId, limit: String(limit) });
      if (after !== undefined) query.set('after', after);
      return request('/dashboard?' + query.toString(), 'GET',
        (value): value is OwnerDashboardView => validDashboard(value) && value.project_id === projectId && actor?.role !== 'member',
        { signal, maximum: 256 * 1024 });
    },
    async getDueResolution(taskId, settings = {}) {
      const owner = requireOwner();
      if (!decimal(taskId)) throw new TeamApiError('team_request_invalid', 0);
      return request('/tasks/' + taskId + '/due-resolution', 'GET',
        (value): value is DueResolutionCandidate => validDueCandidate(value) && value.task_id === taskId
          && owner.actor.project_ids.includes(value.project_id), { ...settings, maximum: 32768 });
    },
    async createDueResolutionPreview(candidate, choice) {
      const owner = requireOwner();
      const captured = structuredClone(candidate);
      if (!validDueCandidate(captured) || !owner.actor.project_ids.includes(captured.project_id)
        || !shape(choice, ['due_at', 'reason']) || !nullable(utcDate)(choice.due_at)
        || !text(choice.reason, 4000) || !/\S/.test(choice.reason)) throw new TeamApiError('team_request_invalid', 0);
      const due = choice.due_at, reason = choice.reason;
      const body = JSON.stringify({ observation_id: captured.observation_id, expected_revision: captured.baseline_revision,
        expected_fingerprint: captured.observed_fingerprint, due_at: due, reason });
      return request('/tasks/' + captured.task_id + '/due-resolution/previews', 'POST',
        (value): value is DueResolutionPreview => validDuePreview(value) && Object.keys(captured)
          .every(key => (value.candidate as RecordValue)[key] === (captured as RecordValue)[key])
          && sameDate(value.due_at, due) && value.reason === reason, { body, status: 201, csrf: owner.csrf, maximum: 32768 });
    },
    async confirmDueResolution(preview, operationId) {
      const owner = requireOwner();
      const captured = structuredClone(preview);
      if (!validDuePreview(captured) || !uuid(operationId) || !owner.actor.project_ids.includes(captured.candidate.project_id))
        throw new TeamApiError('team_request_invalid', 0);
      return request('/due-resolutions/' + captured.preview_id + '/confirm', 'POST',
        (value): value is CommandReceipt => validReceipt(value) && value.acceptance_receipt.operation_id === operationId
          && (value.execution_state.task_id == null || value.execution_state.task_id === captured.candidate.task_id)
          && (value.current == null || value.current.project_id === captured.candidate.project_id
            && value.current.task_id === captured.candidate.task_id),
        { body: JSON.stringify({ operation_id: operationId }), status: 202, csrf: owner.csrf });
    },
  };
}
