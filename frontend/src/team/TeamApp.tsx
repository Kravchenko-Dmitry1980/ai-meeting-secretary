import { useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, CheckCircle2, Clock3, LayoutGrid, LoaderCircle, RefreshCw, Users, X } from 'lucide-react';
import { createTeamApi } from './api';
import type { Actor, TaskChange, TaskSnapshot } from './api';
import { useTeamAuth } from './useTeamAuth';
import { useTeamTasks } from './useTeamTasks';
import { BUCKETS, BUCKET_LABELS, MATRIX_KEYS, MATRIX_LABELS, emptyDraft, taskViews } from './model';
import type { AssigneeBinding, Bucket, MatrixKey, PublicAction, TeamDraft } from './model';
import { TeamTaskForm } from './TeamTaskForm';
import { TeamCommandPreview } from './TeamCommandPreview';
import { TeamOwnerDashboard } from './TeamOwnerDashboard';
import { TeamDueResolution } from './TeamDueResolution';
import { taskDescriptionText } from './taskDescription';

type View = 'today' | 'board' | 'matrix';
const views: { id: View; label: string }[] = [{ id: 'today', label: 'Сегодня' }, { id: 'board', label: 'Канбан' }, { id: 'matrix', label: 'Матрица' }];
const stateLabels: Record<string, string> = { queued: 'Принято в очередь', running: 'Выполняется', reconciling: 'Проверяется после сбоя', applied: 'Применено и проверено', conflict: 'Конфликт: требуется сверка изменений', uncertain: 'Результат пока неизвестен', rejected: 'Отклонено' };
const actionLabels: Record<string, string> = { create: 'Создание задачи', set_state: 'Изменение статуса', assign: 'Назначение ответственного', set_due: 'Изменение срока', resolve_due: 'Подтверждение внешнего срока', classify: 'Важность и срочность', comment: 'Комментарий', propose_due: 'Предложение срока', rename: 'Изменение названия', link: 'Связь с источником' };
const fieldLabels: Record<string, string> = { title: 'название', description: 'описание', assignee_id: 'ответственный', bucket: 'статус', important: 'важность', urgent: 'срочность', classification_confirmed: 'подтверждение приоритетов', due_at: 'срок', due_confirmed: 'подтверждение срока', due_phrase: 'фраза о сроке', due_timezone: 'часовой пояс', remote_fingerprint: 'версия', comment: 'комментарий', result: 'результат', reason: 'причина' };
const errorLabels: Record<string, string> = {
  team_session_expired: 'Сеанс завершён. Войдите снова.', team_session_lost: 'Сеанс завершён. Войдите снова.',
  team_request_failed: 'Не удалось выполнить запрос.', team_read_failed: 'Не удалось обновить задачи.',
  external_change_detected: 'Задача изменена вне приложения. Обновите её и подготовьте новую команду.',
  actor_revision_conflict: 'Права участника изменились. Войдите снова.',
  revision_conflict: 'Задача изменилась. Обновите карточку перед подтверждением.',
  monthly_budget_exhausted: 'Месячный бюджет Polza исчерпан.',
  account_snapshot_stale: 'Нужна свежая проверка расходов Polza.',
  team_text_required: 'Заполните текст команды или результат выполнения.',
  team_assignee_required: 'Выберите участника из актуального справочника.',
  team_due_required: 'Укажите точные дату и время либо выберите «Без срока».',
  team_due_confirmation_required: 'Подтвердите срок или отсутствие срока.',
  team_classification_required: 'Выберите и важность, и срочность.',
  team_operation_pending: 'Эта команда уже отправлена. Сначала проверьте её результат.',
  team_preview_stale: 'Проект или сеанс изменился. Подготовьте команду заново.',
  team_owner_required: 'Это действие доступно владельцу проекта.',
  team_task_required: 'Выберите актуальную задачу в текущем проекте.',
  team_task_not_assigned: 'Изменить этот статус может ответственный или владелец.',
  team_bucket_required: 'Выберите новый статус задачи.',
  remote_due_unconfirmed: 'Срок изменён вне приложения и требует подтверждения.',
  member_mapping_changed: 'Связь участника с проектом изменилась. Обновите данные перед назначением.',
};
function message(code: string | null | undefined) { return code ? errorLabels[code] ?? 'Действие не подтверждено. Проверьте состояние или обновите карточку.' : ''; }
function date(value: string | null | undefined) {
  if (!value) return 'Не подтверждено';
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime()) ? new Intl.DateTimeFormat('ru-RU', { timeZone: 'Europe/Moscow', dateStyle: 'short', timeStyle: 'short' }).format(parsed) + ' МСК' : 'Дата недоступна';
}
function safeAction(task: TaskSnapshot, actor: Actor) {
  return actor.role === 'owner' || task.assignee_id === actor.id && task.bucket !== 'done' && task.bucket !== 'cancelled';
}

function HistoryValues({ values, members }: { values: TaskChange; members: AssigneeBinding[] }) {
  const text = (key: string, value: unknown) => {
    if (key === 'due_at') return value === null ? 'Без срока' : date(typeof value === 'string' ? value : null);
    if (key === 'bucket' && typeof value === 'string') return BUCKET_LABELS[value as Bucket];
    if (key === 'assignee_id') return members.find((member) => member.id === value)?.display_name ?? 'Участник команды';
    if (typeof value === 'boolean') return key === 'important' ? value ? 'Важно' : 'Неважно' : key === 'urgent' ? value ? 'Срочно' : 'Несрочно' : value ? 'Да' : 'Нет';
    return value === null ? 'Не указано' : String(value);
  };
  return <details className="team-history-values"><summary>Детали команды</summary><dl>{Object.entries(values).filter(([, value]) => value !== undefined).map(([key, value]) => <div key={key}><dt>{fieldLabels[key] ?? 'Данные команды'}</dt><dd>{text(key, value)}</dd></div>)}</dl></details>;
}

function TaskCard({ task, actor, members, onSelect, onMove, onDrag, onDragEnd, now, matrix = false }: {
  task: TaskSnapshot; actor: Actor; members: AssigneeBinding[]; onSelect: (task: TaskSnapshot) => void;
  onMove: (task: TaskSnapshot, value: string, matrix: boolean) => void;
  onDrag: (task: TaskSnapshot, matrix: boolean) => void; onDragEnd: () => void; now: number; matrix?: boolean;
}) {
  const overdue = task.due_at && Date.parse(task.due_at) < now && task.bucket !== 'done' && task.bucket !== 'cancelled';
  const assignee = members.find((member) => member.id === task.assignee_id)?.display_name
    ?? (task.assignee_id === actor.id ? actor.display_name : task.assignee_id ? 'Участник команды' : 'Без исполнителя');
  const enabled = matrix ? actor.role === 'owner' : safeAction(task, actor);
  const movementOptions = matrix ? MATRIX_KEYS.filter((key) => key !== 'unclassified')
    : BUCKETS.filter((bucket) => actor.role === 'owner' || bucket !== 'inbox' && bucket !== 'cancelled');
  return <article className="team-card" draggable={enabled} onDragEnd={onDragEnd}
    onDragStart={(event) => { if (!enabled) return; event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', task.task_id); onDrag(task, matrix); }}>
    <button className="team-card-title" onClick={() => onSelect(task)}>{task.title}</button>
    <div className="team-card-meta"><Users size={12} aria-hidden="true" />{assignee}<span className="team-pill">{BUCKET_LABELS[task.bucket ?? 'inbox']}</span></div>
    <div className="team-card-meta"><Clock3 size={12} aria-hidden="true" /><span className={overdue ? 'team-pill overdue' : ''}>{task.due_at ? date(task.due_at) : task.due_confirmed ? 'Без срока' : 'Срок не уточнён'}</span></div>
    {!task.classification_confirmed && <div className="team-card-meta">Приоритеты не разобраны</div>}
    {enabled && <details className="team-card-move"><summary>{matrix ? 'Изменить приоритеты' : 'Изменить статус'}</summary>
      <label><span className="team-muted">Показать изменение для подтверждения</span><select aria-label={`${matrix ? 'Квадрант' : 'Статус'} задачи ${task.title}`} value="" onChange={(event) => { if (event.target.value) onMove(task, event.target.value, matrix); }}>
        <option value="">Выбрать…</option>{movementOptions.map((key) => <option key={key} value={key}>{matrix ? MATRIX_LABELS[key as MatrixKey] : BUCKET_LABELS[key as Bucket]}</option>)}
      </select></label></details>}
  </article>;
}

export function TeamApp({ maxSource = null, lifecycleSignal, bridgeUnavailable = false }: {
  maxSource?: unknown; lifecycleSignal?: AbortSignal; bridgeUnavailable?: boolean;
} = {}) {
  const api = useMemo(() => createTeamApi(), []);
  const { state: authState, auth } = useTeamAuth(api, { max: maxSource, signal: lifecycleSignal });
  const { state, store } = useTeamTasks(api, authState.actor);
  const [view, setView] = useState<View>(() => {
    const value = new URLSearchParams(window.location.search).get('view');
    return value === 'matrix' || value === 'board' ? value : 'today';
  });
  const [filter, setFilter] = useState<'mine' | 'all'>('mine');
  const [code, setCode] = useState('');
  const [errorState, setErrorState] = useState<{ epoch: number; value: string | null }>({ epoch: -1, value: null });
  const uiError = errorState.epoch === authState.sessionEpoch ? errorState.value : null;
  const setUiError = (value: string | null) => setErrorState({ epoch: authState.sessionEpoch, value });
  const [formState, setFormState] = useState({ epoch: -1, open: false });
  const formOpen = formState.epoch === authState.sessionEpoch && formState.open;
  const setFormOpen = (open: boolean) => setFormState({ epoch: authState.sessionEpoch, open });
  const [now, setNow] = useState(Date.now);
  const detail = useRef<HTMLDialogElement>(null);
  const drag = useRef<{ taskId: string; matrix: boolean } | null>(null);
  const pollAttempts = useRef(new Map<string, number>());
  const actor = authState.actor;
  const selected = state.selectedTask;
  const visible = taskViews(state.tasks, { actorId: actor?.id ?? '', filter, now });
  const members: AssigneeBinding[] = state.members.map((member) => ({ ...member, project_ids: state.projectId ? [state.projectId] : [] }));

  useEffect(() => {
    const node = detail.current;
    if ((selected || formOpen) && node && !node.open) node.showModal();
    if (!selected && !formOpen && node?.open) node.close();
  }, [selected, formOpen, actor, authState.sessionEpoch]);

  useEffect(() => { drag.current = null; pollAttempts.current.clear(); }, [authState.sessionEpoch]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!actor) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      for (const operation of store.getSnapshot().operations) {
        if (!operation.receipt || !['queued', 'running', 'reconciling'].includes(operation.receipt.execution_state.state)) continue;
        const attempts = pollAttempts.current.get(operation.command.operation_id) ?? 0;
        if (attempts >= 30) continue;
        pollAttempts.current.set(operation.command.operation_id, attempts + 1);
        void store.poll(operation.command.operation_id);
      }
    }, 4000);
    return () => window.clearInterval(timer);
  }, [actor, store]);

  const closeDetail = () => { store.selectTask(null); setFormOpen(false); setUiError(null); };
  const selectTask = (task: TaskSnapshot) => {
    store.selectTask(task.task_id);
    store.setDraft({ action: 'comment', taskId: task.task_id, values: {}, assignee: null });
    setUiError(null);
  };
  const prepare = () => { try { store.prepare(); setUiError(null); } catch (error) {
    setUiError(error && typeof error === 'object' && 'code' in error && typeof error.code === 'string' ? message(error.code) : 'Проверьте поля команды.');
  } };
  const previewMove = (task: TaskSnapshot, value: string, matrix: boolean) => {
    setUiError(null);
    const action: PublicAction = matrix ? 'classify' : 'set_state';
    if (matrix) {
      store.setDraft({ action, taskId: task.task_id, assignee: null, values: { important: value.startsWith('important-'), urgent: value.endsWith('-urgent') && !value.endsWith('-not-urgent'), classification_confirmed: true } });
      prepare();
    } else if (value === 'done') {
      store.selectTask(task.task_id);
      store.setDraft({ action, taskId: task.task_id, assignee: null, values: { bucket: 'done', result: '' } });
      setFormOpen(true);
    } else {
      store.setDraft({ action, taskId: task.task_id, assignee: null, values: { bucket: value as Bucket } });
      prepare();
    }
  };
  const drop = (value: string, matrix: boolean) => {
    const current = drag.current;
    drag.current = null;
    if (!current || current.matrix !== matrix) return;
    const task = state.tasks.find((item) => item.task_id === current.taskId);
    if (task) previewMove(task, value, matrix);
  };
  const cards = (tasks: readonly TaskSnapshot[], matrix = false) => tasks.map((task) => actor && <TaskCard key={task.task_id} task={task} actor={actor} members={members} now={now} onSelect={selectTask} onMove={previewMove} matrix={matrix} onDragEnd={() => { drag.current = null; }} onDrag={(item, isMatrix) => { drag.current = { taskId: item.task_id, matrix: isMatrix }; }} />);
  const submitCode = async (event: React.FormEvent) => {
    event.preventDefault(); const value = code; setCode('');
    try { await auth.loginCode(value); setUiError(null); } catch { /* Sanitized error is held only in the auth store. */ }
  };

  if (!actor) return <main className="team-login"><div className="team-brand">Secretary<span> Team</span></div><h1>Задачи вашей команды</h1>
    <p>Откройте приложение из бота MAX. Для входа на компьютере запросите в боте кнопку «Вход на компьютере» и введите одноразовый код.</p>
    {bridgeUnavailable && <p className="team-notice">Не удалось подключить MAX Bridge. Вход по одноразовому коду доступен; для входа через MAX закройте и заново откройте приложение.</p>}
    {authState.status === 'checking' && <p role="status"><LoaderCircle size={18} className="team-spinner" aria-hidden="true" /> Проверяем сеанс…</p>}
    {authState.error && <div className="team-notice error" role="alert">{authState.error.message}<br /><small>Код: {authState.error.code}</small></div>}
    <form onSubmit={(event) => { void submitCode(event); }}><label className="team-field">Одноразовый код<input type="password" autoComplete="off" value={code} maxLength={80} onChange={(event) => setCode(event.target.value)} required /></label>
      <button className="team-primary" disabled={authState.status === 'checking' || !code.trim()}>Войти</button></form>
    {authState.status === 'uncertain' && <p>Ответ на вход не получен. Сначала проверьте сеанс; повторно использованный код может быть уже погашен.</p>}
    {(authState.status === 'error' || authState.status === 'uncertain') && <button onClick={() => { void auth.bootstrap({ max: maxSource }); }}>Проверить сеанс</button>}
    <p className="team-muted">При истечении сеанса MAX закройте и заново откройте приложение через бота.</p>
  </main>;

  const sync = state.status?.sync;
  const cloud = state.status?.cloud;
  const syncLabel = !sync ? state.loading ? 'Проверяем состояние синхронизации' : 'Данные синхронизации недоступны'
    : sync.state === 'not_configured' ? 'Синхронизация не настроена'
    : sync.state === 'never_synced' ? 'Ожидаем первую сверку с Vikunja'
      : sync.state === 'syncing' ? 'Сверяем задачи с Vikunja' : sync.state === 'degraded' ? 'Синхронизация требует внимания' : 'Последняя сверка завершена';
  return <div className="team-shell"><header className="team-header"><div><div className="team-brand">Secretary<span> Team</span></div><small>Общие договорённости → конкретные действия</small></div>
    <div className="team-identity"><div>{actor.display_name}<small>{actor.role === 'owner' ? 'Владелец' : 'Участник'}</small></div><button onClick={() => { void auth.logout().catch(() => undefined); }}>Выйти</button></div></header>
    <main className="team-page"><div className="team-title-row"><div><h1>{views.find((item) => item.id === view)?.label}</h1><p className="team-muted">Один набор задач, три способа выбрать следующий шаг.</p></div>
      {actor.role === 'owner' && <button className="team-primary" disabled={!state.projectId} onClick={() => { store.selectTask(null); store.setDraft(emptyDraft()); setFormOpen(true); setUiError(null); }}>+ Новая задача</button>}</div>
      <div className="team-syncbar"><section className="team-panel"><div className="team-statusline">{state.loading || sync?.state === 'syncing' ? <LoaderCircle size={17} className="team-spinner" aria-hidden="true" /> : sync?.state === 'ready' ? <CheckCircle2 size={17} aria-hidden="true" /> : <Clock3 size={17} aria-hidden="true" />}{syncLabel}</div>
        <p className="team-muted">Последняя успешная сверка: {date(sync?.last_successful_sync_at)}<br />Задачи загружены в интерфейс: {date(state.lastFetchedAt)}</p>
        {sync?.error_code && <small>{message(sync.error_code)} Код: {sync.error_code}</small>}</section>
        <section className="team-panel"><div className="team-statusline"><LayoutGrid size={17} aria-hidden="true" />{!cloud ? state.loading ? 'Polza: проверяем состояние' : 'Polza: данные состояния недоступны' : cloud.state === 'not_configured' ? 'Polza: бюджет не настроен' : cloud.state === 'paused' ? 'Polza: расходы приостановлены' : cloud.state === 'unavailable' ? 'Polza: данные недоступны' : 'Polza: последний статус — обработка доступна'}</div>
          {cloud?.paused_reason && <p className="team-muted">{message(cloud.paused_reason)} Код: {cloud.paused_reason}</p>}
          {actor.role === 'owner' && cloud && <p className="team-muted">Расход: {cloud.spend_rub ?? 'неизвестен'} ₽ · Зарезервировано: {cloud.reserved_rub ?? 'неизвестно'} ₽<br />Остаток: {cloud.remaining_rub ?? 'неизвестен'} ₽ · Лимит: {cloud.effective_budget_rub ?? 'неизвестен'} ₽/месяц</p>}
          <small>Просмотр и изменения задач не вызывают Polza.</small></section></div>
      <div className="team-toolbar"><div className="team-tabs" role="tablist" aria-label="Представление задач">{views.map((item) => <button key={item.id} role="tab" aria-selected={view === item.id} onClick={() => setView(item.id)}>{item.label}</button>)}</div>
        <label>Проект<select aria-label="Проект" value={state.projectId ?? ''} onChange={(event) => { closeDetail(); store.selectProject(event.target.value); }}><option value="" disabled>Выберите проект</option>{actor.project_ids.map((id) => <option key={id} value={id}>Проект {id}</option>)}</select></label>
        <label>Задачи<select aria-label="Мои или все задачи" value={filter} onChange={(event) => setFilter(event.target.value as 'mine' | 'all')}><option value="mine">Мои</option><option value="all">Все</option></select></label>
        <button disabled={state.loading || !state.projectId} onClick={() => { void store.refresh(); }}><RefreshCw size={14} aria-hidden="true" /> Обновить</button></div>
      <TeamOwnerDashboard api={api} actor={actor} projectId={state.projectId} sessionEpoch={authState.sessionEpoch} />
      {(state.errorCode || uiError) && <div className="team-notice error" role="alert"><AlertCircle size={16} aria-hidden="true" /> {uiError ?? message(state.errorCode)}{state.errorCode && <small> Код: {state.errorCode}</small>}</div>}
      {state.metadataErrorCode && <div className="team-notice" role="status">Справочник, история или статус синхронизации не обновлены. Повторите проверку перед назначением участника.<small> Код: {state.metadataErrorCode}</small></div>}
      {state.operations.length > 0 && <section className="team-panel team-operations" aria-label="Состояние команд"><h2>Изменения</h2>{state.operations.map((operation) => <div className="team-operation" key={operation.command.operation_id}><div className="team-statusline">{operation.transport === 'sending' && <LoaderCircle size={15} className="team-spinner" aria-hidden="true" />}{operation.receipt ? stateLabels[operation.receipt.execution_state.state] : operation.transport === 'unknown' ? 'Ответ не получен — проверяем прежнюю команду' : operation.transport === 'rejected' ? 'Отправка отклонена' : 'Отправляем команду'}</div>
        {(operation.errorCode || operation.receipt?.execution_state.error_code) && <p>{message(operation.errorCode ?? operation.receipt?.execution_state.error_code)} <small>{operation.errorCode ?? operation.receipt?.execution_state.error_code}</small></p>}<code>{operation.command.operation_id}</code>{operation.transport !== 'sending' && operation.receipt?.execution_state.state !== 'applied' && <button onClick={() => { void store.poll(operation.command.operation_id); }}>Проверить результат</button>}</div>)}</section>}
      {view === 'board' && <section className="team-board" aria-label="Канбан">{BUCKETS.map((bucket) => <section className="team-column" key={bucket} onDragOver={(event) => { if (drag.current && !drag.current.matrix) event.preventDefault(); }} onDrop={(event) => { event.preventDefault(); drop(bucket, false); }}><h2>{BUCKET_LABELS[bucket]}<span className="team-count">{visible.board[bucket].length}</span></h2>{cards(visible.board[bucket])}{!visible.board[bucket].length && <p className="team-muted">Задач нет</p>}</section>)}</section>}
      {view === 'matrix' && <><section className="team-matrix" aria-label="Матрица Эйзенхауэра">{MATRIX_KEYS.filter((key) => key !== 'unclassified').map((key) => <section className="team-quadrant" key={key} onDragOver={(event) => { if (drag.current?.matrix) event.preventDefault(); }} onDrop={(event) => { event.preventDefault(); drop(key, true); }}><h2>{MATRIX_LABELS[key]}<span className="team-count">{visible.matrix[key].length}</span></h2>{cards(visible.matrix[key], true)}{!visible.matrix[key].length && <p className="team-muted">Задач нет</p>}</section>)}</section><section className="team-panel team-unclassified"><h2>Не разобрано <span className="team-count">{visible.matrix.unclassified.length}</span></h2><div className="team-list">{cards(visible.matrix.unclassified, true)}</div>{!visible.matrix.unclassified.length && <p className="team-muted">Все задачи классифицированы.</p>}</section></>}
      {view === 'today' && <><section><h2>Сегодня и просроченные <span className="team-count">{visible.today.length}</span></h2><div className="team-list">{cards(visible.today)}</div>{!visible.today.length && <div className="team-empty">Задач с наступившим сроком нет. Проверьте задачи без срока.</div>}</section><section className="team-section"><h2>Без срока или без уточнения <span className="team-count">{visible.noDue.length}</span></h2><div className="team-list">{cards(visible.noDue)}</div></section></>}
      {!state.loading && !visible.tasks.length && <p className="team-empty">В этом проекте нет задач для выбранного фильтра.</p>}
    </main>
    <dialog ref={detail} className="team-modal team-detail" aria-labelledby="team-detail-title" onCancel={(event) => { event.preventDefault(); closeDetail(); }}><div className="team-modal-header"><div><h2 id="team-detail-title">{selected?.title ?? 'Новая задача'}</h2>{selected && <small>Статус: {BUCKET_LABELS[selected.bucket ?? 'inbox']} · Срок: {selected.due_at ? date(selected.due_at) : selected.due_confirmed ? 'Без срока' : 'Не уточнён'}</small>}</div><button aria-label="Закрыть карточку" onClick={closeDetail}><X size={18} /></button></div>
      <div className="team-detail-grid"><section>{selected?.description && <p className="team-detail-description">{taskDescriptionText(selected.description)}</p>}{selected?.origin?.source_kind === 'meeting' && <section className="team-panel"><h3>Из встречи</h3><p className="team-detail-description">{selected.origin.meeting_id ? `Встреча ${selected.origin.meeting_id}` : 'Источник — запись встречи'}</p></section>}
        {selected && <><h3>История</h3><p className="team-muted">Время ниже — момент записи или обнаружения. Изменения вне приложения не имеют подтверждённого автора.</p>{state.detailLoading && <p role="status">Загружаем историю…</p>}<ol className="team-history">{state.history.map((entry) => <li key={entry.id}><strong>{entry.kind === 'external_change' ? 'Обнаружено внешнее изменение' : entry.actor_display_name ?? 'Команда приложения'}</strong><div>{entry.action ? actionLabels[entry.action] ?? 'Изменение задачи' : 'Событие команды'}{entry.execution_state && ` · ${stateLabels[entry.execution_state]}`}</div>{entry.changed_fields.length > 0 && <div>Поля: {entry.changed_fields.map((field) => fieldLabels[field] ?? 'данные задачи').join(', ')}</div>}<time dateTime={entry.recorded_at}>{date(entry.recorded_at)}</time>{entry.values && <HistoryValues values={entry.values} members={members} />}{entry.error_code && <small>Код: {entry.error_code}</small>}</li>)}</ol>{!state.detailLoading && !state.errorCode && !state.history.length && <p className="team-muted">Записей истории пока нет.</p>}</>}</section>
        <section>{state.operations.filter((operation) => operation.command.task_id === selected?.task_id || !selected && operation.command.action === 'create').map((operation) => <p className="team-notice" key={operation.command.operation_id} role="status">{operation.receipt ? stateLabels[operation.receipt.execution_state.state] : operation.transport === 'sending' ? 'Отправляем команду…' : 'Результат не подтверждён'}{(operation.errorCode || operation.receipt?.execution_state.error_code) && <small> · {message(operation.errorCode ?? operation.receipt?.execution_state.error_code)}</small>}{operation.transport !== 'sending' && operation.receipt?.execution_state.state !== 'applied' && <button onClick={() => { void store.poll(operation.command.operation_id); }}>Проверить результат</button>}</p>)}
          {state.errorCode && <p className="team-notice error" role="alert">{message(state.errorCode)} Код: {state.errorCode}</p>}
          <TeamDueResolution api={api} actor={actor} sessionEpoch={authState.sessionEpoch} projectId={state.projectId} task={selected}
            needsReview={sync?.error_code === 'remote_due_unconfirmed'} now={now} onRefresh={() => { void store.refresh(); }} />
          <h3>{selected ? 'Подготовить изменение' : 'Создать задачу'}</h3><TeamTaskForm actor={actor} task={selected} members={members} draft={state.draft} onDraft={(patch: Partial<TeamDraft>) => store.setDraft(patch)} onPreview={prepare} error={uiError} /></section></div>
    </dialog>
    <TeamCommandPreview command={state.preview} task={state.tasks.find((task) => task.task_id === state.preview?.task_id) ?? null} members={members} busy={state.operations.some((operation) => operation.command.operation_id === state.preview?.operation_id && operation.transport === 'sending')} error={uiError} onConfirm={() => { const id = state.preview?.operation_id; if (!id) return; const request = store.confirm(id); store.dismissPreview(); void request.catch((error: unknown) => setUiError(error instanceof Error ? error.message : 'Не удалось отправить команду.')); }} onClose={() => store.dismissPreview()} />
  </div>;
}
