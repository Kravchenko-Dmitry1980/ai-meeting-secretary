import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../services/api';
import { taskAssignmentsApi } from '../services/taskAssignments';
import type { AssignmentChange, AssignmentCommand, AssignmentSnapshot } from '../services/taskAssignments';
import { ASSIGNMENT_BATCH_MAX, assignmentError, assignmentReason, assignmentStamp, assignmentUncertain, canConfirmAssignment, captureAssignmentCommand } from '../utils/taskAssignments';
import type { AssignmentParticipant } from '../utils/taskAssignments';
import { Button } from './ui/Button';

export interface AssignmentReviewPanelProps {
  meetingId: string; transcriptVersion: number; summaryVersion: number | null;
  roster: readonly AssignmentParticipant[];
  /** Deadline metadata from the same selected Summary, keyed by action_id. */
  dueDates?: Readonly<Record<string, string | null | undefined>>;
  refreshKey?: string | number;
  /** Explicit immutable assignment history, always read-only. */
  assignmentRevision?: number;
  onSource: (segmentId: string, transcriptVersion: number) => void;
  onChanged?: (snapshot: AssignmentSnapshot) => void;
}
interface Draft { value: string; edit: number }
interface Pending {
  command: AssignmentCommand; draftEdits: Record<string, number>; selectedStamps: Record<string, string>;
}
interface View { scopeKey: string; snapshot: AssignmentSnapshot; latest: AssignmentSnapshot | null }
const basisText = { named_person: 'По имени в источнике', self_commitment: 'По словам автора о своём действии', manual: 'Ручной выбор', unknown: 'Основание не определено' };
const statusText = { proposed: 'Предложение · не проверено', confirmed: 'Назначение проверено человеком', needs_review: 'Нужна проверка' };

/** Prefer key={`${meetingId}:${transcriptVersion}:${summaryVersion}:${assignmentRevision ?? 'current'}`}.
 * Keep that key stable across roster/identity refreshes to retain drafts and exact uncertain replay.
 * Scope changes and unmount also invalidate async ownership internally. No form means native
 * required/select validation cannot block a replay of the already captured command.
 */
export function AssignmentReviewPanel({ meetingId, transcriptVersion, summaryVersion, roster, dueDates, refreshKey, assignmentRevision, onSource, onChanged }: AssignmentReviewPanelProps) {
  const scopeKey = JSON.stringify([meetingId, transcriptVersion, summaryVersion, assignmentRevision]);
  const [view, setView] = useState<View | null>(null);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [selected, setSelected] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [fresh, setFresh] = useState(false);
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const life = useRef(0);
  const reads = useRef(0);
  const locked = useRef(false);
  const pending = useRef<Pending | null>(null);
  const edits = useRef(0);
  const loadedSummary = useRef<number | null>(null);
  const callbacks = useRef({ onChanged });
  useEffect(() => { callbacks.current = { onChanged }; }, [onChanged]);

  const refresh = useCallback(async () => {
    const epoch = ++reads.current;
    const owner = life.current;
    setLoading(true); setFresh(false);
    try {
      const [snapshot, latest] = await Promise.all([
        taskAssignmentsApi.read(meetingId, summaryVersion ?? undefined, assignmentRevision),
        taskAssignmentsApi.read(meetingId).catch((failure: unknown) => { if (failure instanceof ApiError && failure.status === 404) return null; throw failure; }),
      ]);
      if (epoch !== reads.current || owner !== life.current) return;
      if (snapshot.meeting_id !== meetingId || snapshot.transcript_version !== transcriptVersion || (summaryVersion !== null && snapshot.summary_version !== summaryVersion) || (assignmentRevision !== undefined && snapshot.revision !== assignmentRevision) || (latest && latest.meeting_id !== meetingId)) throw new Error('scope');
      if (loadedSummary.current !== null && loadedSummary.current !== snapshot.summary_version) { setDrafts({}); setSelected({}); }
      loadedSummary.current = snapshot.summary_version;
      setView({ scopeKey, snapshot, latest }); setFresh(true);
    } catch (failure) {
      if (epoch !== reads.current || owner !== life.current) return;
      setError(failure instanceof Error && failure.message === 'scope' ? 'Получена другая версия итогов. Выберите её явно или обновите встречу.' : failure instanceof ApiError && failure.status === 404 ? 'Итоги или назначения этой версии пока недоступны. Обновите состояние после подготовки итогов.' : 'Не удалось обновить назначения. Проверьте локальный сервер и повторите чтение.');
    } finally { if (epoch === reads.current && owner === life.current) setLoading(false); }
  }, [meetingId, transcriptVersion, summaryVersion, assignmentRevision, scopeKey]);

  useEffect(() => {
    const owner = ++life.current;
    void Promise.resolve().then(() => {
      if (owner !== life.current) return;
      setView(null); setDrafts({}); setSelected({}); setBusy(false); setRetry(false); setError(''); setNotice('');
    });
    const alive = life; const fetches = reads; const lock = locked; const operation = pending; const summary = loadedSummary;
    return () => { ++alive.current; ++fetches.current; lock.current = false; operation.current = null; summary.current = null; };
  }, [scopeKey]);
  useEffect(() => {
    void Promise.resolve().then(refresh);
    const fetches = reads;
    return () => { ++fetches.current; };
  }, [refresh, refreshKey]);

  const snapshot = view?.scopeKey === scopeKey ? view.snapshot : null;
  const historical = !!snapshot && (assignmentRevision !== undefined || view?.latest?.transcript_version !== snapshot.transcript_version || view?.latest?.summary_version !== snapshot.summary_version);
  const editable = !!snapshot && fresh && !loading && !historical;
  const blocked = !editable || busy || retry;
  const eligibleSelection = snapshot?.captured_context_hash !== null ? snapshot?.items.filter((item) => selected[item.action.action_id] === assignmentStamp(item) && canConfirmAssignment(item, roster)) ?? [] : [];
  const send = async (changes?: AssignmentChange[]) => {
    if (locked.current || !snapshot) return;
    let operation = pending.current;
    if (!operation) {
      if (!editable || !changes) return;
      try {
        const command = captureAssignmentCommand(snapshot, changes, roster, crypto.randomUUID());
        operation = { command, draftEdits: {}, selectedStamps: {} };
        for (const change of changes) {
          if (drafts[change.action_id]) operation.draftEdits[change.action_id] = drafts[change.action_id].edit;
          if (selected[change.action_id]) operation.selectedStamps[change.action_id] = selected[change.action_id];
        }
      } catch (failure) { setError(failure instanceof Error ? failure.message : 'Проверьте выбранные задачи.'); return; }
      pending.current = operation;
    }
    locked.current = true;
    const owner = life.current; const readEpoch = reads.current;
    let accepted: AssignmentSnapshot | null = null;
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await taskAssignmentsApi.review(meetingId, operation.command);
      if (owner !== life.current) return;
      pending.current = null; setRetry(false);
      // Receipt is operation history; only current may become the visible assignment state.
      if (readEpoch === reads.current && result.current.meeting_id === meetingId && result.current.transcript_version === transcriptVersion && result.current.summary_version === snapshot.summary_version) {
        setView((previous) => previous?.scopeKey === scopeKey ? { ...previous, snapshot: result.current } : previous);
      } else await refresh();
      if (owner !== life.current) return;
      setDrafts((current) => Object.fromEntries(Object.entries(current).filter(([id, draft]) => operation.draftEdits[id] !== draft.edit)));
      setSelected((current) => Object.fromEntries(Object.entries(current).filter(([id, stamp]) => operation.selectedStamps[id] !== stamp)));
      setNotice(result.receipt_stale ? 'Прежняя операция найдена в истории. Показано текущее состояние; изменившиеся назначения требуют проверки.' : 'Проверка назначения сохранена.');
      accepted = result.current;
    } catch (failure) {
      if (owner !== life.current) return;
      setError(assignmentError(failure));
      const uncertain = assignmentUncertain(failure); setRetry(uncertain);
      if (!uncertain) { pending.current = null; await refresh(); }
    } finally { if (owner === life.current) { locked.current = false; setBusy(false); } }
    // A parent callback failure is not an uncertain server mutation.
    if (accepted && owner === life.current) callbacks.current.onChanged?.(accepted);
  };

  return <section aria-label="Проверка ответственных за задачи" className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h3 className="font-semibold text-white">Ответственные за задачи</h3><Button variant="ghost" disabled={busy || loading} onClick={() => { setError(''); void refresh(); }}>Обновить назначения</Button></div>
    <p className="text-sm leading-6 text-slate-400">Подтверждение означает проверку назначения человеком. Оно само по себе не доказывает согласие участника принять обязательство.</p>
    {loading && <p role="status">Загрузка назначений…</p>}
    {error && <p role="alert" className="break-words text-sm text-rose-200">{error}</p>}
    {notice && <p role="status" className="text-sm text-mint">{notice}</p>}
    {historical && <p className="text-sm text-amber-200">Историческая версия итогов · только чтение. Назначения не переносятся в новые итоги автоматически.</p>}
    {snapshot?.captured_context_hash === null && <p className="text-sm text-amber-200">В этих итогах нет сохранённой проверки назначений. Для текущей версии можно явно выбрать ответственного вручную.</p>}
    {retry && <div className="space-y-2"><p className="text-sm text-amber-200">Новый запрос недоступен, пока результат прежней операции неизвестен. Черновик можно менять; повтор отправит исходное решение.</p><Button disabled={busy} onClick={() => { void send(); }}>Повторить тот же запрос</Button></div>}
    {snapshot && !snapshot.items.length && <p className="text-sm text-slate-400">В этой версии итогов нет задач.</p>}
    {snapshot?.items.map((item, index) => {
      const id = item.action.action_id;
      const eligible = snapshot.captured_context_hash !== null && canConfirmAssignment(item, roster);
      const person = roster.find((participant) => participant.id === item.participant_id);
      const draft = drafts[id]?.value ?? item.participant_id ?? '';
      const manualAvailable = roster.filter((participant) => participant.id === draft && participant.enabled).length === 1;
      const checked = selected[id] === assignmentStamp(item) && eligible;
      return <article key={id} className="space-y-3 rounded-xl border border-white/10 p-4">
        <h4 className="break-words font-medium text-white">{item.action.text}</h4>
        {dueDates?.[id] && <p className="text-sm text-slate-400">Срок: {dueDates[id]}</p>}
        <p className="text-sm text-slate-300">Ответственный: {item.participant_id ? person ? `${person.display_name}${person.enabled ? '' : ' · отключён'}` : 'Участник недоступен' : 'Не определён'}</p>
        <p className="text-sm text-slate-400">{basisText[item.basis]} · <span className={item.status === 'confirmed' ? 'text-mint' : 'text-amber-200'}>{statusText[item.status]}</span></p>
        {item.action.evidence_quote ? <blockquote className="whitespace-pre-wrap break-words border-l-2 border-violet-400/40 pl-3 text-sm text-slate-200">{item.action.evidence_quote}</blockquote> : <p className="text-sm text-slate-400">Дословная цитата не сохранена.</p>}
        <div className="flex flex-wrap gap-2">{item.action.source_segment_ids.map((segmentId, sourceIndex) => <Button key={`${segmentId}:${sourceIndex}`} variant="ghost" onClick={() => onSource(segmentId, snapshot.transcript_version)}>Источник {sourceIndex + 1}</Button>)}</div>
        <ul className="space-y-1 text-sm text-slate-400">{item.reason_codes.map((reason, reasonIndex) => <li key={`${reason}:${reasonIndex}`}>{assignmentReason(reason)}</li>)}</ul>
        {item.previous_provenance && <p className="text-sm text-amber-200">Есть предложение из прежних итогов; проверьте его заново.</p>}
        <label className="field-label">Ответственный для задачи {index + 1}<select disabled={historical} value={draft} onChange={(event) => { const edit = ++edits.current; setDrafts((current) => ({ ...current, [id]: { value: event.target.value, edit } })); }}>
          <option value="">Выберите участника</option>
          {draft && !roster.some((participant) => participant.id === draft) && <option value={draft} disabled>Участник недоступен</option>}
          {roster.map((participant) => <option key={participant.id} value={participant.id} disabled={!participant.enabled}>{participant.display_name}{participant.enabled ? '' : ' · отключён'}</option>)}
        </select></label>
        <div className="flex flex-wrap gap-2"><Button disabled={blocked || !manualAvailable} onClick={() => { void send([{ action_id: id, decision: 'set_manual', participant_id: draft }]); }}>Назначить вручную</Button><Button variant="ghost" disabled={blocked || !item.participant_id} onClick={() => { void send([{ action_id: id, decision: 'clear', participant_id: null }]); }}>Снять ответственного</Button><Button variant="ghost" disabled={blocked || !eligible} onClick={() => { void send([{ action_id: id, decision: 'confirm_proposal', participant_id: null }]); }}>Подтвердить предложение</Button></div>
        <label className="checkbox-label"><input type="checkbox" checked={checked} disabled={blocked || !eligible || (!checked && eligibleSelection.length >= ASSIGNMENT_BATCH_MAX)} onChange={(event) => { setSelected((current) => { const next = { ...current }; if (event.target.checked && eligible && eligibleSelection.length < ASSIGNMENT_BATCH_MAX) next[id] = assignmentStamp(item); else delete next[id]; return next; }); }} /><span>Проверено мной · включить в подтверждение</span></label>
        <details className="text-xs text-slate-500"><summary>Технические сведения</summary><p className="break-all">ID задачи: {id}; версия итогов: {snapshot.summary_version}; ревизия: {snapshot.revision}</p><p className="break-all">Причины: {item.reason_codes.join(', ')}</p></details>
      </article>;
    })}
    {!!snapshot?.items.length && <div className="flex flex-wrap items-center gap-3"><Button disabled={blocked || !eligibleSelection.length || eligibleSelection.length > ASSIGNMENT_BATCH_MAX} onClick={() => { void send(eligibleSelection.map((item) => ({ action_id: item.action.action_id, decision: 'confirm_proposal', participant_id: null }))); }}>Подтвердить проверенные задачи</Button><span className="text-sm text-slate-400">Отмечено: {eligibleSelection.length} · максимум {ASSIGNMENT_BATCH_MAX}</span></div>}
  </section>;
}
