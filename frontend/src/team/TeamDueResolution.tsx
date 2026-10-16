import { useEffect, useId, useMemo, useState, useSyncExternalStore } from 'react';
import type { Actor, TeamApi, TaskSnapshot } from './api';
import { dueScopeKey, TeamDueResolutionStore, utcFromMoscow } from './dueResolutionStore';

function date(value: string | null) {
  return value === null ? 'Без срока' : `${new Intl.DateTimeFormat('ru-RU', { timeZone: 'Europe/Moscow',
    dateStyle: 'long', timeStyle: 'short' }).format(new Date(value))} МСК`;
}
const labels: Record<string, string> = { queued: 'Принято в очередь', running: 'Выполняется', reconciling: 'Сверяется после сбоя',
  applied: 'Применено и проверено', conflict: 'Конфликт: требуется новая сверка', uncertain: 'Результат не подтверждён', rejected: 'Отклонено' };
const errors: Record<string, string> = { team_due_required: 'Укажите точную дату и время или «Без срока» и причину.',
  team_operation_pending: 'Решение по этой задаче уже отправлено. Сначала проверьте его результат.',
  team_preview_stale: 'Версия задачи или сеанс изменились. Обновите задачи и повторите сверку.',
  team_due_preview_expired: 'Предпросмотр истёк. Подготовьте новый.', team_command_conflict: 'Срок или права изменились. Обновите задачи и подготовьте новый предпросмотр.' };
export function TeamDueResolution({ api, actor, sessionEpoch, projectId, task, needsReview, now, onRefresh }: {
  api: TeamApi; actor: Actor | null; sessionEpoch: number; projectId: string | null; task: TaskSnapshot | null;
  needsReview: boolean; now: number; onRefresh: () => void;
}) {
  const id = useId();
  const store = useMemo(() => new TeamDueResolutionStore(api), [api]);
  const state = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  const scopeKey = dueScopeKey(actor, sessionEpoch, projectId, task);
  useEffect(() => store.disconnect, [store]);
  useEffect(() => { store.setScope(actor, sessionEpoch, projectId, task); }, [store, actor, sessionEpoch, projectId, task]);
  const [local, setLocal] = useState({ scopeKey, input: '', clear: false, reason: '' });
  const draft = local.scopeKey === scopeKey ? local : { scopeKey, input: '', clear: false, reason: '' };
  const [localError, setLocalError] = useState<{ scopeKey: string; message: string } | null>(null);
  const run = (action: () => Promise<void>) => { setLocalError(null); void action().catch((error: unknown) => {
    const errorCode = error && typeof error === 'object' && 'code' in error && typeof error.code === 'string' ? error.code : '';
    setLocalError({ scopeKey, message: errors[errorCode] ?? 'Действие не подтверждено. Уточните дату и причину или повторите сверку.' });
  }); };
  if (actor?.role !== 'owner' || state.scopeKey !== scopeKey) return null;
  if (!needsReview && !state.open && !state.operations.length) return null;
  const candidate = state.candidate, preview = state.preview;
  const expired = !!preview && Date.parse(preview.expires_at) <= now;
  const pending = state.operations.some(value => value.operationId === state.operationId);
  return <section className="team-panel team-due-resolution" aria-labelledby={`${id}-title`}>
    <h3 id={`${id}-title`}>Сверка внешнего изменения срока</h3>
    {task && needsReview && <><p className="team-muted">В проекте обнаружен неподтверждённый срок. Проверка выбранной задачи покажет исходное и внешнее значения. До решения владельца такое изменение требует внимания.</p>
      <button className="team-button" disabled={state.busy} onClick={() => run(store.load)}>Проверить внешний срок этой задачи</button></>}
    {state.busy && <p role="status">Проверяем или готовим предпросмотр…</p>}
    {state.open && candidate && <><dl className="team-preview-values"><div><dt>Задача</dt><dd>{candidate.title}</dd></div>
      <div><dt>Ранее подтверждённый срок</dt><dd>{date(candidate.baseline_due_at)}</dd></div>
      <div><dt>Обнаруженный внешний срок</dt><dd>{date(candidate.observed_due_at)}</dd></div></dl>
      {!preview && <form className="team-form" onSubmit={event => { event.preventDefault();
        run(() => store.prepare(draft.clear ? null : utcFromMoscow(draft.input), draft.reason)); }}>
        <fieldset className="team-form-fields" disabled={state.busy}><legend>Ваше явное решение</legend>
          <label className="team-field" htmlFor={`${id}-clear`}><span><input id={`${id}-clear`} type="checkbox" checked={draft.clear}
            onChange={event => setLocal({ ...draft, clear: event.target.checked })} /> Без срока</span></label>
          <label className="team-field" htmlFor={`${id}-date`}>Новый срок · Москва (UTC+03:00)
            <input id={`${id}-date`} type="datetime-local" required={!draft.clear} disabled={draft.clear} value={draft.input}
              onChange={event => setLocal({ ...draft, input: event.target.value })} /></label>
          <label className="team-field" htmlFor={`${id}-reason`}>Причина решения<textarea id={`${id}-reason`} required maxLength={4000}
            value={draft.reason} onChange={event => setLocal({ ...draft, reason: event.target.value })} /></label>
          <button className="team-button" type="submit">Показать решение для подтверждения</button></fieldset>
      </form>}
      {preview && <section className="team-preview" aria-label="Неизменяемый предпросмотр решения"><h4>Подтвердите именно это решение</h4>
        <dl className="team-preview-values"><div><dt>Новый срок</dt><dd>{date(preview.due_at)}</dd></div><div><dt>Причина</dt><dd className="team-literal">{preview.reason}</dd></div>
          <div><dt>Предпросмотр действует до</dt><dd>{date(preview.expires_at)}</dd></div></dl>
        <p className="team-hint">Принятие в очередь ещё не означает исполнения. Изменение подтверждается последующей проверкой Vikunja.</p>
        <details className="team-technical"><summary>Версия и идентификаторы</summary><dl>
          <div><dt>Наблюдение</dt><dd>{preview.candidate.observation_id}</dd></div><div><dt>Ревизия</dt><dd>{preview.candidate.baseline_revision}</dd></div>
          <div><dt>Внешняя версия</dt><dd>{preview.candidate.observed_fingerprint}</dd></div><div><dt>Операция</dt><dd>{state.operationId}</dd></div></dl></details>
        {expired && <p className="team-notice" role="status">Предпросмотр истёк. Повторите проверку внешнего срока.</p>}
        <button className="team-button team-button-primary" disabled={pending || state.busy || expired} onClick={() => run(store.confirm)}>Подтвердить решение</button>
      </section>}
      <button className="team-button" onClick={store.close}>Закрыть сверку</button></>}
    {(state.errorCode || localError?.scopeKey === scopeKey) && <p className="team-notice error" role="alert">{state.errorCode ? errors[state.errorCode] ?? 'Сверка недоступна или требует обновления задачи.' : localError?.message}
      {state.errorCode && <small> Код: {state.errorCode}</small>}</p>}
    {state.operations.map(operation => <div className="team-notice" key={operation.operationId} role="status"><strong>{operation.title}</strong>
      <p>{operation.receipt ? labels[operation.receipt.execution_state.state] : operation.transport === 'sending' ? 'Отправляем решение…' : 'Результат отправки не подтверждён'}</p>
      <code>{operation.operationId}</code>{operation.errorCode && <p>Код: {operation.errorCode}</p>}
      {operation.transport !== 'sending' && <button className="team-button" onClick={() => run(() => store.poll(operation.operationId))}>Проверить результат</button>}
      <button className="team-button" onClick={onRefresh}>Обновить задачи</button></div>)}
  </section>;
}
