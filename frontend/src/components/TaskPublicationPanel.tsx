import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../services/api';
import { taskPublicationsApi } from '../services/taskPublications';
import type { DeliveryReceipt, PublicationContext, PublicationPreviewResult, PublishPreview, TaskPublicationsClient } from '../services/taskPublications';
import { capturePublicationCommand, capturePublicationSelections, formatPublicationDue, initialPublicationDraft, mergePublicationReceipt, publicationApplied, publicationEligible, publicationError, publicationItemLabel, publicationPending, publicationReason, publicationScopeEqual } from '../utils/taskPublications';
import type { PublicationDraft } from '../utils/taskPublications';
import { Button } from './ui/Button';

type PendingStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
type Schedule = (callback: () => void | Promise<void>, delay: number) => () => void;
const defaultSchedule: Schedule = (callback, delay) => { const timer = window.setTimeout(() => { void callback(); }, delay); return () => window.clearTimeout(timer); };
const preAcceptanceConflicts = new Set(['publication_preview_expired', 'publication_source_changed', 'publication_payload_changed', 'publication_action_unavailable', 'member_mapping_changed', 'preview_payload_mismatch']);
function browserStorage(): PendingStorage | undefined { try { return window.sessionStorage; } catch { return undefined; } }
function storedOperation(storage: PendingStorage | undefined, key: string): string | null {
  try { const value = storage?.getItem(key); return value && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value) ? value : null; } catch { return null; }
}
export interface TaskPublicationPanelProps {
  meetingId: string; transcriptVersion: number; summaryVersion: number; refreshKey?: string | number;
  onSource: (segmentId: string, transcriptVersion: number) => void;
  api?: TaskPublicationsClient; clock?: () => number; schedule?: Schedule; storage?: PendingStorage;
}

export function TaskPublicationPanel({ meetingId, transcriptVersion, summaryVersion, refreshKey, onSource, api = taskPublicationsApi, clock = Date.now, schedule = defaultSchedule, storage }: TaskPublicationPanelProps) {
  const scopeKey = JSON.stringify([meetingId, transcriptVersion, summaryVersion]);
  const store = storage ?? browserStorage();
  const storageKey = `secretary-publication:${scopeKey}`;
  const [context, setContext] = useState<PublicationContext | null>(null);
  const [drafts, setDrafts] = useState<Record<string, PublicationDraft>>({});
  const [previewResult, setPreviewResult] = useState<PublicationPreviewResult | null>(null);
  const [receipts, setReceipts] = useState<DeliveryReceipt[]>([]);
  const [unknownOperation, setUnknownOperation] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [setup, setSetup] = useState(false);
  const [error, setError] = useState('');
  const life = useRef(0); const reads = useRef(0); const edits = useRef(0); const locked = useRef(false);
  const contextRef = useRef<PublicationContext | null>(null);
  const receiptsRef = useRef<DeliveryReceipt[]>([]);
  const receiptReads = useRef(new Map<string, number>());
  const unknownRef = useRef<string | null>(null);
  const previewRef = useRef<PublishPreview | null>(null);
  const pendingOperationKey = JSON.stringify([...new Set([...receipts.filter(publicationPending).map((item) => item.operation_id), ...(unknownOperation ? [unknownOperation] : [])])].sort());

  const invalidate = useCallback(() => { ++edits.current; previewRef.current = null; setPreviewResult(null); }, []);
  const invalidateReads = useCallback(() => { ++reads.current; }, []);
  const leaveScope = useCallback(() => { ++life.current; invalidateReads(); previewRef.current = null; }, [invalidateReads]);
  const remember = useCallback((operationId: string | null) => {
    try { if (operationId) store?.setItem(storageKey, operationId); else store?.removeItem(storageKey); } catch { /* Receipt history remains server-owned when browser storage is unavailable. */ }
  }, [store, storageKey]);
  const applyReceipt = useCallback((incoming: DeliveryReceipt, expectedOperation?: string) => {
    const current = contextRef.current;
    if (!current || (expectedOperation && incoming.operation_id !== expectedOperation)) throw new Error('receipt_scope');
    const previous = receiptsRef.current.find((item) => item.operation_id === incoming.operation_id);
    const merged = mergePublicationReceipt(previous, incoming, current.scope);
    receiptReads.current.set(merged.operation_id, (receiptReads.current.get(merged.operation_id) ?? 0) + 1);
    const next = [...receiptsRef.current.filter((item) => item.operation_id !== merged.operation_id), merged];
    receiptsRef.current = next; setReceipts(next);
    if (unknownRef.current === merged.operation_id) { unknownRef.current = null; setUnknownOperation(null); }
    if (!publicationPending(merged) && storedOperation(store, storageKey) === merged.operation_id) remember(null);
  }, [store, storageKey, remember]);

  const recover = useCallback(async (operationId: string, owner: number) => {
    const generation = reads.current;
    const token = (receiptReads.current.get(operationId) ?? 0) + 1;
    receiptReads.current.set(operationId, token);
    const ownsRead = () => owner === life.current && generation === reads.current && token === receiptReads.current.get(operationId);
    try {
      const incoming = await api.read(meetingId, operationId);
      if (!ownsRead()) return 'superseded';
      applyReceipt(incoming, operationId); setError('');
      return 'found';
    } catch (failure) {
      if (!ownsRead()) return 'superseded';
      setError(failure instanceof ApiError && failure.status === 403 ? 'Восстановите доступ владельца к проекту, чтобы проверить прежнюю операцию. Новая публикация пока недоступна.' : 'Результат неизвестен. Проверка использует прежнюю операцию; новая публикация пока недоступна.');
      return failure instanceof ApiError && failure.status === 404 ? 'missing' : 'failed';
    }
  }, [api, meetingId, applyReceipt]);

  const refresh = useCallback(async () => {
    const read = ++reads.current; const owner = life.current;
    invalidate(); setLoading(true); setError('');
    try {
      const [fresh, history] = await Promise.all([api.context(meetingId), api.list(meetingId)]);
      if (read !== reads.current || owner !== life.current) return;
      if (fresh.scope.meeting_id !== meetingId || fresh.scope.transcript_version !== transcriptVersion || fresh.scope.summary_version !== summaryVersion) throw new Error('source_scope');
      const changed = JSON.stringify(fresh) !== JSON.stringify(contextRef.current);
      contextRef.current = fresh; setContext(fresh); setSetup(false);
      setDrafts((previous) => Object.fromEntries(fresh.candidates.map((row) => [row.action_id, previous[row.action_id] ? { ...previous[row.action_id], assigneeId: row.assignee_id ?? previous[row.action_id].assigneeId, selected: changed ? false : previous[row.action_id].selected } : initialPublicationDraft(row)])));
      const currentHistory = history.filter((item) => publicationScopeEqual(item.scope, fresh.scope));
      const next = currentHistory.map((item) => mergePublicationReceipt(receiptsRef.current.find((old) => old.operation_id === item.operation_id), item, fresh.scope));
      // A temporarily lagging list cannot erase an operation already acknowledged here.
      for (const old of receiptsRef.current) if (publicationScopeEqual(old.scope, fresh.scope) && !next.some((item) => item.operation_id === old.operation_id)) next.push(old);
      receiptsRef.current = next; setReceipts(next);
      const saved = unknownRef.current ?? storedOperation(store, storageKey);
      if (saved && !next.some((item) => item.operation_id === saved)) {
        unknownRef.current = saved; setUnknownOperation(saved); await recover(saved, owner);
      } else {
        unknownRef.current = null; setUnknownOperation(null);
        if (saved && next.some((item) => item.operation_id === saved && !publicationPending(item))) remember(null);
      }
    } catch (failure) {
      if (read !== reads.current || owner !== life.current) return;
      if (failure instanceof ApiError && failure.status === 503 && failure.message === 'team_publication_not_configured') { setSetup(true); setContext(null); contextRef.current = null; }
      else setError(failure instanceof Error && failure.message === 'source_scope' ? 'Показана другая версия источника. Обновите итоги встречи.' : 'Не удалось обновить публикации. Проверьте локальный сервер и повторите чтение.');
    } finally { if (read === reads.current && owner === life.current) setLoading(false); }
  }, [api, meetingId, transcriptVersion, summaryVersion, invalidate, store, storageKey, recover, remember]);

  useEffect(() => {
    const owner = ++life.current;
    void Promise.resolve().then(() => {
      if (owner !== life.current) return;
      contextRef.current = null; receiptsRef.current = []; unknownRef.current = null; previewRef.current = null; locked.current = false;
      setContext(null); setDrafts({}); setReceipts([]); setUnknownOperation(null); setPreviewResult(null); setBusy(false); setSetup(false); setError('');
    });
    return leaveScope;
  }, [scopeKey, leaveScope]);
  useEffect(() => {
    let cancelled = false;
    void Promise.resolve().then(() => { if (!cancelled) void refresh(); });
    return () => { cancelled = true; invalidateReads(); };
  }, [refresh, refreshKey, invalidateReads]);

  useEffect(() => {
    const owner = life.current; let cancelled = false; let stop = () => {};
    const pendingIds = () => [...new Set([...receiptsRef.current.filter(publicationPending).map((item) => item.operation_id), ...(unknownRef.current ? [unknownRef.current] : [])])];
    const tick = async () => {
      if (cancelled || owner !== life.current) return;
      const ids = pendingIds();
      await Promise.all(ids.map((id) => recover(id, owner)));
      if (!cancelled && owner === life.current && pendingIds().length) stop = schedule(tick, 10000);
    };
    if (pendingIds().length && !loading) stop = schedule(tick, 2000);
    return () => { cancelled = true; stop(); };
  }, [pendingOperationKey, loading, scopeKey, recover, schedule]);

  const current = context && context.scope.meeting_id === meetingId && context.scope.transcript_version === transcriptVersion && context.scope.summary_version === summaryVersion ? context : null;
  const visibleReceipts = current ? receipts.filter((item) => publicationScopeEqual(item.scope, current.scope)) : [];
  const pending = !!unknownOperation || visibleReceipts.some(publicationPending);
  let choicesReady = false;
  if (current) { try { capturePublicationSelections(current, drafts); choicesReady = true; } catch { /* Inline choices and server reasons explain what is unresolved. */ } }
  const update = (actionId: string, patch: Partial<PublicationDraft>) => {
    invalidate(); setDrafts((previous) => ({ ...previous, [actionId]: { ...previous[actionId], ...patch } }));
  };
  const prepare = async () => {
    if (!current || locked.current || loading || pending) return;
    let body;
    try { body = capturePublicationSelections(current, drafts); } catch (failure) { setError(failure instanceof Error ? failure.message : 'Проверьте выбранные значения.'); return; }
    const owner = life.current; const version = edits.current;
    locked.current = true; setBusy(true); setError('');
    try {
      const result = await api.preview(meetingId, body);
      if (owner !== life.current || version !== edits.current) return;
      if (result.preview) {
        const selectedIds = body.selections.map((item) => item.action_id).sort();
        if (JSON.stringify(result.preview.items.map((item) => item.action_id).sort()) !== JSON.stringify(selectedIds)) throw new Error('preview_selection');
        capturePublicationCommand(result.preview, current, result.preview.preview_id, clock());
      }
      previewRef.current = result.preview ?? null; setPreviewResult(result);
    } catch (failure) { if (owner === life.current && version === edits.current) setError(publicationError(failure)); }
    finally { if (owner === life.current) { locked.current = false; setBusy(false); } }
  };
  const confirm = async () => {
    if (!current || locked.current || loading || pending || !previewRef.current) return;
    let command;
    try { command = capturePublicationCommand(previewRef.current, current, crypto.randomUUID(), clock()); }
    catch (failure) { invalidate(); setError(failure instanceof Error ? failure.message : 'Обновите предпросмотр.'); return; }
    const owner = life.current;
    locked.current = true; setBusy(true); setError('');
    remember(command.operation_id); unknownRef.current = command.operation_id; setUnknownOperation(command.operation_id);
    invalidate();
    try {
      const result = await api.confirm(meetingId, command);
      if (owner !== life.current) return;
      if (JSON.stringify(result.items.map((item) => item.action_id).sort()) !== JSON.stringify(command.items.map((item) => item.action_id).sort())) throw new Error('receipt_selection');
      applyReceipt(result, command.operation_id);
    } catch (failure) {
      if (owner !== life.current) return;
      if (failure instanceof ApiError && [400, 413, 422].includes(failure.status)) { unknownRef.current = null; setUnknownOperation(null); remember(null); setError(publicationError(failure)); }
      else {
        setError('Результат неизвестен. Проверяется прежняя операция.');
        const result = await recover(command.operation_id, owner);
        if (owner === life.current && result === 'missing' && failure instanceof ApiError && failure.status === 409 && preAcceptanceConflicts.has(failure.message)) {
          unknownRef.current = null; setUnknownOperation(null); remember(null); setError(publicationError(failure));
        }
      }
    } finally { if (owner === life.current) { locked.current = false; setBusy(false); } }
  };

  return <section aria-label="Публикация задач в команду" className="space-y-4 rounded-xl border border-violet-400/20 p-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h3 className="font-semibold text-white">Передать задачи команде</h3><Button variant="ghost" disabled={loading || busy} onClick={() => { void refresh(); }}>Обновить публикации</Button></div>
    <p className="text-sm text-slate-400">Назначение ответственного сохраняется отдельно. Команда получит только выбранные задачи после вашего подтверждения.</p>
    {loading && <p role="status">Загрузка публикаций…</p>}
    {setup && <p role="status" className="text-sm text-slate-400">Публикация в команду пока не настроена. Задачи и проверенные назначения сохранены в Secretary.</p>}
    {error && <p role="alert" className="text-sm text-amber-200">{error}</p>}
    {current && <>
      <p className="text-sm text-slate-300">Проект: <strong>{current.project_name}</strong></p>
      {current.candidates.map((candidate) => {
        const draft = drafts[candidate.action_id] ?? initialPublicationDraft(candidate);
        const eligible = publicationEligible(candidate);
        return <article key={candidate.action_id} data-action-id={candidate.action_id} className="space-y-3 rounded-xl border border-white/10 p-4">
          <label className="checkbox-label"><input aria-label="Выбрать для публикации" type="checkbox" disabled={loading || !eligible || (!draft.selected && Object.values(drafts).filter((item) => item.selected).length >= 100)} checked={draft.selected && eligible} onChange={(event) => update(candidate.action_id, { selected: event.target.checked && eligible })} /><span>{candidate.title}</span></label>
          {candidate.evidence_quote ? <blockquote className="whitespace-pre-wrap break-words border-l-2 border-violet-400/40 pl-3 text-sm text-slate-200">{candidate.evidence_quote}</blockquote> : <p className="text-sm text-amber-200">Дословный источник требует проверки.</p>}
          <div className="flex flex-wrap gap-2">{candidate.source_segment_ids.map((id, index) => <Button key={id} variant="ghost" onClick={() => onSource(id, current.scope.transcript_version)}>Источник {index + 1}</Button>)}</div>
          {candidate.reason_codes.map((code) => <p key={code} className="text-sm text-amber-200">{publicationReason(code)}</p>)}
          {candidate.existing_publication_id && <p className="text-sm text-slate-400">Есть сохранённая публикация этого пункта. Её состояние показано ниже.</p>}
          <label className="field-label">Ответственный команды<select aria-label="Ответственный команды" disabled={loading || !eligible || !!candidate.assignee_id} value={draft.assigneeId} onChange={(event) => update(candidate.action_id, { assigneeId: event.target.value })}><option value="">Выберите участника команды</option>{draft.assigneeId && !current.members.some((member) => member.id === draft.assigneeId) && <option value={draft.assigneeId} disabled>Участник недоступен</option>}{current.members.filter((member) => !candidate.assignee_id || member.id === candidate.assignee_id).map((member) => <option key={member.id} value={member.id}>{member.display_name}</option>)}</select></label>
          {candidate.assignee_id && <p className="text-xs text-slate-400">Ответственный связан с проверенным профилем. Его изменение требует настройки связи участника с командой.</p>}
          <p className="text-sm text-slate-400">В источнике: {candidate.due_phrase ?? 'срок не назван'}</p>
          <label className="field-label">Срок<select aria-label="Решение по сроку" disabled={loading || !eligible} value={draft.dueResolution} onChange={(event) => update(candidate.action_id, { dueResolution: event.target.value as PublicationDraft['dueResolution'] })}><option value="unresolved">Требует моего выбора</option><option value="date">Указать дату и время</option><option value="none">Без срока</option></select></label>
          {draft.dueResolution === 'date' && <label className="field-label">Дата и время · Москва<input aria-label="Дата и время по Москве" type="datetime-local" value={draft.dueLocal} disabled={loading || !eligible} onChange={(event) => update(candidate.action_id, { dueLocal: event.target.value })} /></label>}
          {!!candidate.possible_supersedes.length && <div className="space-y-3"><p className="text-sm text-amber-200">Есть похожая задача из прежних итогов. Выберите действие явно.</p><label className="field-label">Прежняя задача<select aria-label="Решение по прежней задаче" value={draft.intent} disabled={loading || !candidate.eligible} onChange={(event) => { const intent = event.target.value as PublicationDraft['intent']; update(candidate.action_id, { intent, separateId: intent === 'create_separate' ? draft.separateId ?? crypto.randomUUID() : null }); }}><option value="">Выберите действие</option><option value="link">Связать с существующей</option><option value="propose_update">Предложить изменение</option><option value="create_separate">Создать отдельную</option></select></label>{(draft.intent === 'link' || draft.intent === 'propose_update') && <label className="field-label">Карточка<select aria-label="Прежняя карточка" value={draft.targetPublicationId} onChange={(event) => update(candidate.action_id, { targetPublicationId: event.target.value })}><option value="">Выберите проверенную карточку</option>{candidate.possible_supersedes.map((item) => <option key={item.publication_id} value={item.publication_id} disabled={!item.verified || !item.task_id}>{item.title}{item.task_id ? ` · №${item.task_id}` : ' · не подтверждена'}</option>)}</select></label>}</div>}
        </article>;
      })}
      {!current.candidates.length && <p className="text-sm text-slate-400">В этой версии нет задач для публикации.</p>}
      <Button disabled={loading || busy || pending || !choicesReady} onClick={() => { void prepare(); }}>Подготовить публикацию</Button>
      {previewResult && !previewResult.preview && <div role="status" className="space-y-2 text-sm text-amber-200"><p>Предпросмотр требует уточнения. Проверьте выбранные значения.</p>{previewResult.candidates.filter((item) => drafts[item.action_id]?.selected).map((item) => <div key={item.action_id}><strong>{item.title}</strong>{item.reason_codes.map((code) => <p key={code}>{publicationReason(code)}</p>)}</div>)}</div>}
      {previewResult?.preview && <div aria-label="Предпросмотр публикации" className="space-y-3 rounded-xl border border-mint/25 p-4"><h4 className="font-medium text-white">Что получит команда</h4><p className="text-sm text-slate-400">Проект: {current.project_name}</p>{previewResult.preview.items.map((item) => <article key={item.action_id} className="space-y-2"><p className="font-medium text-white">{item.title}</p><p className="text-sm text-slate-300">Кому: {current.members.find((member) => member.id === item.assignee_id)?.display_name ?? 'Участник требует проверки'}</p><p className="text-sm text-slate-300">Срок: {formatPublicationDue(item.due_at)}</p><p className="text-sm text-slate-400">{item.intent === 'link' ? 'Связать с существующей карточкой' : item.intent === 'propose_update' ? 'Передать предложение об изменении' : item.intent === 'create_separate' ? 'Создать отдельную задачу' : 'Создать задачу'}</p><blockquote className="whitespace-pre-wrap break-words text-sm text-slate-300">{item.evidence_quote}</blockquote></article>)}<Button disabled={busy || loading || pending} onClick={() => { void confirm(); }}>Опубликовать выбранные задачи</Button></div>}
      {pending && <p role="status" className="text-sm text-amber-200">Есть незавершённая операция. Результат проверяется по её прежнему ID.</p>}
      {(pending || visibleReceipts.length > 0) && <Button variant="ghost" disabled={busy || loading} onClick={() => { const owner = life.current; const ids = [...new Set([...visibleReceipts.map((item) => item.operation_id), ...(unknownOperation ? [unknownOperation] : [])])]; void Promise.all(ids.map((id) => recover(id, owner))); }}>Проверить состояние</Button>}
      {visibleReceipts.map((receipt) => <div key={receipt.operation_id} className="space-y-2 border-t border-white/10 pt-3">{receipt.items.map((item) => <article key={item.publication_id} className="space-y-1 text-sm"><p className={publicationApplied(receipt, item) ? 'text-mint' : 'text-slate-300'}>{current.candidates.find((candidate) => candidate.action_id === item.action_id)?.title ?? 'Задача'} · {publicationItemLabel(receipt, item)}</p>{publicationApplied(receipt, item) && <p className="text-slate-400">Карточка №{item.execution_state.task_id}</p>}{item.source_stale && <p className="text-amber-200">Источник изменился. Опубликованная карточка сохраняется.</p>}{item.correction_required && <p className="text-amber-200">Требуется отдельное исправление; проверьте текущую задачу.</p>}</article>)}</div>)}
    </>}
  </section>;
}
