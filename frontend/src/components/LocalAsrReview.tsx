import { useEffect, useRef, useState } from 'react';
import { api, errorMessage } from '../services/api';
import { localAsrApi } from '../services/localAsr';
import { speakersApi } from '../services/speakers';
import type { MeetingParticipant, ParticipantRoster } from '../types/api';
import type { LocalAsrClusterPreview, LocalAsrRun, LocalAsrRunSummary, LocalAsrSegment } from '../types/localAsr';
import { asrTime as timeOffset, audioFragment, clusterCommand, exclusionReason, manualStatus, overlappingTurns, reviewError, sameRunScope, sourceChannel } from '../utils/localAsr';
import { Button } from './ui/Button';
import { Card } from './ui/Card';

interface Props { meetingId: string }
interface Catalogue { meetingId: string; runs: LocalAsrRunSummary[]; roster: ParticipantRoster }

export function LocalAsrReview({ meetingId }: Props) {
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null);
  const [selection, setSelection] = useState<{ meetingId: string; runId: string } | null>(null);
  const [failure, setFailure] = useState<{ meetingId: string; message: string } | null>(null);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let active = true;
    void Promise.all([localAsrApi.list(meetingId), speakersApi.roster(meetingId)]).then(([runs, roster]) => {
      if (!active) return;
      if (roster.meeting_id !== meetingId || runs.some((run) => run.meeting_id !== meetingId)) throw new Error('Получены данные другой встречи. Обновите страницу.');
      setCatalogue({ meetingId, runs, roster });
      setFailure(null);
    }).catch((error: unknown) => { if (active) setFailure({ meetingId, message: errorMessage(error) }); });
    return () => { active = false; };
  }, [meetingId, reload]);
  const current = catalogue?.meetingId === meetingId ? catalogue : null;
  const error = failure?.meetingId === meetingId ? failure.message : '';
  const selected = selection?.meetingId === meetingId && current?.runs.some((run) => run.run_id === selection.runId)
    ? selection.runId : current?.runs[0]?.run_id;
  return <section aria-label="Локальные анализы голосов" className="space-y-4">
    <Card className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-lg font-medium">Локальные анализы голосов</h2><Button variant="ghost" onClick={() => setReload((value) => value + 1)}>Обновить анализы</Button></div>
      <p className="text-sm leading-6 text-slate-400">Здесь можно проверить голоса в сохранённом анализе и вручную назначить участников. Исходная расшифровка, итоги и задачи сохраняются.</p>
      {error && <p role="alert" className="text-sm text-amber-200">{error}</p>}
      {!current && !error && <p role="status" className="text-sm text-slate-400">Загрузка анализов…</p>}
      {current?.runs.length === 0 && <p className="text-sm text-slate-400">Сохранённых локальных анализов этой встречи пока нет.</p>}
      {current && current.runs.length > 0 && <label className="field-label">Сохранённый анализ<select value={selected ?? ''} onChange={(event) => setSelection({ meetingId, runId: event.target.value })}>
        {current.runs.map((run) => <option key={run.run_id} value={run.run_id}>Анализ {run.revision} · расшифровка v{run.transcript_version}</option>)}
      </select></label>}
    </Card>
    {current && selected && <AnalysisPanel key={`${meetingId}:${selected}:${reload}`} meetingId={meetingId} runId={selected} participants={current.roster.participants} onRefresh={() => setReload((value) => value + 1)} />}
  </section>;
}

interface AnalysisProps extends Props { runId: string; participants: MeetingParticipant[]; onRefresh: () => void }
function AnalysisPanel({ meetingId, runId, participants, onRefresh }: AnalysisProps) {
  const [run, setRun] = useState<LocalAsrRun | null>(null);
  const [error, setError] = useState('');
  const [speakerLabel, setSpeakerLabel] = useState('');
  const [page, setPage] = useState(0);
  const [editingOrdinal, setEditingOrdinal] = useState<number | null>(null);
  useEffect(() => {
    let active = true;
    void localAsrApi.read(meetingId, runId).then((result) => {
      if (!active) return;
      if (!sameRunScope(result, meetingId, runId)) throw new Error('Получен другой анализ. Обновите страницу.');
      setRun(result);
    }).catch((failure: unknown) => { if (active) setError(errorMessage(failure)); });
    return () => { active = false; };
  }, [meetingId, runId]);
  if (error) return <Card role="alert">{error}<Button variant="ghost" className="ml-3" onClick={onRefresh}>Обновить анализ</Button></Card>;
  if (!run || !sameRunScope(run, meetingId, runId)) return <Card role="status">Загрузка сохранённых реплик…</Card>;
  const names = new Map(participants.map((participant) => [participant.id, participant.display_name]));
  const selectedLabel = run.clusters.some((cluster) => cluster.speaker_label === speakerLabel) ? speakerLabel : run.clusters.find((cluster) => cluster.kind === 'single')?.speaker_label ?? '';
  const offset = Math.min(page * 30, Math.max(0, Math.floor((run.result.segment_items.length - 1) / 30) * 30));
  const editing = run.result.segment_items.find((segment) => segment.ordinal === editingOrdinal);
  const save = (result: LocalAsrRun) => {
    if (!sameRunScope(result, meetingId, runId)) throw new Error('Ответ относится к другому анализу. Обновите страницу.');
    setRun(result);
    setEditingOrdinal(null);
  };
  const turns = run.result.speaker_turns ?? [];
  return <div className="space-y-4">
    <Card className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3"><h3 className="font-medium">Анализ {run.revision} · расшифровка v{run.transcript_version}</h3><a className="source-link" href={localAsrApi.exportUrl(meetingId, runId)} download>Скачать анализ и ручные имена · JSON</a></div>
      {run.result.warnings.length > 0 && <div role="status" className="text-sm text-amber-200"><p>Замечания к анализу:</p><ul className="list-disc pl-5">{run.result.warnings.map((warning, index) => <li key={`${index}:${warning}`}>{warning}</li>)}</ul></div>}
      {run.artifacts.alignment?.status !== 'complete' && <p className="text-sm text-amber-200">Пословная привязка может быть неполной. Сохранённый текст остаётся источником; сверяйте спорные места с аудио.</p>}
      <details><summary className="cursor-pointer text-sm text-slate-300">Сохранённый текст</summary><p className="mt-3 whitespace-pre-wrap text-sm leading-7">{run.artifacts.selected_text}</p></details>
    </Card>
    {run.clusters.some((cluster) => cluster.kind === 'single') && <Card className="space-y-3">
      <h3 className="font-medium">Назначить участника группе голоса</h3>
      <p className="text-sm leading-6 text-slate-400">Прослушайте примеры и проверьте точный список реплик. Назначение действует только на выбранные реплики этого анализа. Отдельные ручные решения сохраняются.</p>
      <label className="field-label">Голос в этом анализе<select value={selectedLabel} onChange={(event) => setSpeakerLabel(event.target.value)}>{run.clusters.filter((cluster) => cluster.kind === 'single').map((cluster) => <option key={cluster.speaker_cluster_id} value={cluster.speaker_label}>{cluster.speaker_label}</option>)}</select></label>
      {selectedLabel && <ClusterEditor key={`${run.run_id}:${run.review_revision}:${selectedLabel}`} run={run} speakerLabel={selectedLabel} participants={participants} onSaved={save} onRefresh={onRefresh} />}
    </Card>}
    <Card className="space-y-3">
      <h3 className="font-medium">Реплики анализа · {run.result.segment_items.length}</h3>
      <p className="text-sm text-slate-400">Неизвестные и смешанные голоса сохранены. Ручная правка отдельной реплики имеет приоритет над назначением группе.</p>
      <ol className="space-y-3">{run.result.segment_items.slice(offset, offset + 30).map((segment) => <li key={segment.ordinal} className="rounded-xl border border-white/10 p-3">
        <p className="text-xs text-slate-400">#{segment.ordinal + 1} · {timeOffset(segment.started_ms)}–{timeOffset(segment.ended_ms)} · {segment.speaker_label}</p>
        <p className="mt-2 whitespace-pre-wrap text-sm leading-6">{segment.text}</p>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2"><span className="text-xs text-slate-400">{manualStatus(run, segment.ordinal, names)}</span><Button variant="ghost" className="px-3 py-1.5 text-xs" onClick={() => setEditingOrdinal(segment.ordinal)}>Проверить имя отдельно</Button></div>
      </li>)}</ol>
      {run.result.segment_items.length > 30 && <div className="flex flex-wrap items-center gap-3 text-xs text-slate-400"><Button variant="ghost" disabled={offset === 0} onClick={() => setPage(Math.max(0, Math.floor(offset / 30) - 1))}>Предыдущие реплики</Button><span>{offset + 1}–{Math.min(offset + 30, run.result.segment_items.length)} из {run.result.segment_items.length}</span><Button variant="ghost" disabled={offset + 30 >= run.result.segment_items.length} onClick={() => setPage(Math.floor(offset / 30) + 1)}>Следующие реплики</Button></div>}
      {editing && <SegmentEditor key={`${run.run_id}:${run.review_revision}:${editing.ordinal}`} run={run} segment={editing} participants={participants} onSaved={save} onRefresh={onRefresh} onClose={() => setEditingOrdinal(null)} />}
    </Card>
    <Card>
      <details><summary className="cursor-pointer font-medium">Исходные интервалы голосов · {turns.length}</summary>
        <p className="mt-3 text-sm text-slate-400">Эти интервалы независимы от текстовых реплик. Пересекающиеся интервалы обозначают наложение голосов.</p>
        {turns.length === 0 ? <p className="mt-3 text-sm text-slate-400">Исходные интервалы голосов не переданы.</p> : <div className="mt-3 max-h-96 overflow-auto"><table className="w-full text-left text-xs"><thead><tr><th scope="col" className="p-2">Голос</th><th scope="col" className="p-2">Начало</th><th scope="col" className="p-2">Конец</th></tr></thead><tbody>{turns.map((turn) => <tr key={turn.ordinal}><td className="p-2">{turn.speaker_label}</td><td className="p-2">{timeOffset(turn.started_ms)} <span className="text-slate-500">({turn.started_ms} мс)</span></td><td className="p-2">{timeOffset(turn.ended_ms)} <span className="text-slate-500">({turn.ended_ms} мс)</span></td></tr>)}</tbody></table></div>}
      </details>
    </Card>
  </div>;
}

interface EditorProps { run: LocalAsrRun; participants: MeetingParticipant[]; onSaved: (run: LocalAsrRun) => void; onRefresh: () => void }
function ClusterEditor({ run, speakerLabel, participants, onSaved, onRefresh }: EditorProps & { speakerLabel: string }) {
  const [preview, setPreview] = useState<LocalAsrClusterPreview | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [participantId, setParticipantId] = useState('');
  const [listening, setListening] = useState(false);
  const [checkedSet, setCheckedSet] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [stale, setStale] = useState(false);
  const locked = useRef(false);
  const active = useRef(true);
  useEffect(() => {
    active.current = true;
    void localAsrApi.preview(run.meeting_id, run.run_id, speakerLabel).then((value) => {
      if (!active.current) return;
      if (value.run_id !== run.run_id || value.speaker_label !== speakerLabel || value.review_revision !== run.review_revision) throw new Error('Набор реплик изменился. Обновите анализ перед проверкой.');
      setPreview(value);
      setSelected([...value.eligible_segment_ordinals]);
    }).catch((failure: unknown) => { if (active.current) { setError(errorMessage(failure)); setStale(true); } });
    return () => { active.current = false; };
  }, [run.meeting_id, run.run_id, run.review_revision, speakerLabel]);
  const save = async () => {
    if (locked.current || stale || !preview || !listening || !checkedSet || !participants.some((participant) => participant.enabled && participant.id === participantId)) return;
    locked.current = true;
    setBusy(true);
    try {
      const command = clusterCommand(preview, selected, participantId, crypto.randomUUID());
      const result = await localAsrApi.reviewCluster(run.meeting_id, run.run_id, command);
      if (active.current) onSaved(result);
    } catch (failure) { if (active.current) { setError(reviewError(failure)); setStale(true); } }
    finally { locked.current = false; if (active.current) setBusy(false); }
  };
  if (!preview) return <div role={error ? 'alert' : 'status'} className="text-sm text-slate-400">{error || 'Загрузка точного набора реплик…'}{error && <Button variant="ghost" className="ml-3" onClick={onRefresh}>Обновить анализ</Button>}</div>;
  const eligible = new Set(preview.eligible_segment_ordinals);
  const excluded = new Map(preview.excluded_segments.map((item) => [item.segment_ordinal, item.reason]));
  const samples = preview.samples.map((sample) => ({ ...sample, channel: sample.channel ?? sourceChannel(run, sample.source_segment_ids) }));
  const canListen = samples.length > 0 && samples.every((sample) => sample.channel != null);
  return <div className="space-y-4">
    <div className="space-y-3">{samples.map((sample) => <div key={sample.segment_ordinal} className="rounded-xl border border-white/10 p-3"><p className="mb-2 text-sm">Пример #{sample.segment_ordinal + 1} · {timeOffset(sample.started_ms)}–{timeOffset(sample.ended_ms)}</p><p className="mb-2 whitespace-pre-wrap text-sm text-slate-300">{preview.segments.find((segment) => segment.ordinal === sample.segment_ordinal)?.text}</p>{sample.channel ? <AudioSample meetingId={run.meeting_id} channel={sample.channel} startedMs={sample.started_ms} endedMs={sample.ended_ms} /> : <p className="text-sm text-amber-200">Канал исходной записи не определён; подтвердить группу пока нельзя.</p>}</div>)}</div>
    {preview.eligible_segment_ordinals.length === 0 && <p className="text-sm text-slate-400">Нет реплик для общего назначения. Смешанные, спорные и уже проверенные реплики сохранены отдельно.</p>}
    <fieldset disabled={busy || stale} className="space-y-2"><legend className="mb-2 font-medium">Точный набор · выбрано {selected.length} из {preview.shown_segment_ordinals.length}</legend>
      <p className="text-xs text-slate-400">Снимите отметку с реплик, которые не хотите подписывать. Исключённые реплики и прежние подтверждения не будут изменены.</p>
      <div className="max-h-96 space-y-2 overflow-auto rounded-xl border border-white/10 p-3">{preview.segments.map((segment) => <label key={segment.ordinal} className="flex items-start gap-3 rounded-lg p-2 text-sm"><input className="mt-1" type="checkbox" disabled={!eligible.has(segment.ordinal)} checked={selected.includes(segment.ordinal)} onChange={(event) => { setSelected((values) => event.target.checked ? [...values, segment.ordinal] : values.filter((ordinal) => ordinal !== segment.ordinal)); setCheckedSet(false); }} /><span><span className="text-xs text-slate-400">#{segment.ordinal + 1} · {timeOffset(segment.started_ms)}–{timeOffset(segment.ended_ms)}{excluded.has(segment.ordinal) ? ` · ${exclusionReason(excluded.get(segment.ordinal)!)}` : ''}</span><span className="mt-1 block whitespace-pre-wrap leading-6">{segment.text}</span></span></label>)}</div>
      <label className="field-label">Участник<select value={participantId} onChange={(event) => { setParticipantId(event.target.value); setCheckedSet(false); }}><option value="">Выберите участника</option>{participants.filter((participant) => participant.enabled).map((participant) => <option key={participant.id} value={participant.id}>{participant.display_name}</option>)}</select></label>
      <label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" disabled={!canListen} checked={listening} onChange={(event) => setListening(event.target.checked)} /><span>Я прослушал примеры и проверил, чей это голос.</span></label>
      <label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={checkedSet} onChange={(event) => setCheckedSet(event.target.checked)} /><span>Я проверил показанный список и подтверждаю только {selected.length} отмеченных реплик.</span></label>
      <Button disabled={!canListen || !listening || !checkedSet || !participantId || selected.length === 0 || busy || stale} onClick={() => { void save(); }}>{busy ? 'Сохранение…' : `Подтвердить ${selected.length} выбранных реплик`}</Button>
    </fieldset>
    {error && <div role="alert" className="text-sm text-amber-200"><p>{error}</p><Button variant="ghost" className="mt-2" onClick={onRefresh}>Обновить анализ и проверить</Button></div>}
  </div>;
}

function SegmentEditor({ run, segment, participants, onSaved, onRefresh, onClose }: EditorProps & { segment: LocalAsrSegment; onClose: () => void }) {
  const previous = run.reviews.find((review) => review.segment_ordinal === segment.ordinal);
  const [participantId, setParticipantId] = useState(previous?.participant_id ?? '');
  const [listening, setListening] = useState(false);
  const [busy, setBusy] = useState(false);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState('');
  const [preview, setPreview] = useState<LocalAsrClusterPreview | null>(null);
  const locked = useRef(false);
  const active = useRef(true);
  useEffect(() => {
    active.current = true;
    void localAsrApi.preview(run.meeting_id, run.run_id, segment.speaker_label).then((value) => {
      if (!active.current) return;
      if (value.run_id !== run.run_id || value.review_revision !== run.review_revision || value.speaker_label !== segment.speaker_label) throw new Error('Набор реплик изменился. Обновите анализ.');
      setPreview(value);
    }).catch((failure: unknown) => { if (active.current) { setError(errorMessage(failure)); setStale(true); } });
    return () => { active.current = false; };
  }, [run.meeting_id, run.run_id, run.review_revision, segment.speaker_label]);
  const source = preview?.segments.find((item) => item.ordinal === segment.ordinal);
  const channel = source?.channel ?? (source ? sourceChannel(run, source.source_segment_ids) : null);
  const reason = preview?.excluded_segments.find((item) => item.segment_ordinal === segment.ordinal)?.reason;
  const single = run.clusters.some((cluster) => cluster.speaker_label === segment.speaker_label && cluster.kind === 'single') && !overlappingTurns(run, segment)
    && (reason == null || reason === 'individual_override' || reason === 'previous_confirmation');
  const save = async () => {
    if (locked.current || stale || !listening || !preview || !channel || (participantId && (!single || !participants.some((participant) => participant.id === participantId && participant.enabled)))) return;
    locked.current = true;
    setBusy(true);
    try {
      const result = await localAsrApi.reviewSegment(run.meeting_id, run.run_id, { operation_id: crypto.randomUUID(), expected_review_revision: run.review_revision, segment_ordinal: segment.ordinal, participant_id: participantId || null, listening_confirmed: true });
      if (active.current) onSaved(result);
    } catch (failure) { if (active.current) { setError(reviewError(failure)); setStale(true); } }
    finally { locked.current = false; if (active.current) setBusy(false); }
  };
  return <div className="space-y-3 rounded-xl border border-violet-300/25 p-4">
    <div className="flex items-center justify-between gap-3"><h4 className="font-medium">Проверка реплики #{segment.ordinal + 1}</h4><Button variant="ghost" disabled={busy} onClick={onClose}>Закрыть</Button></div>
    <p className="whitespace-pre-wrap text-sm leading-6">{segment.text}</p>
    {channel ? <AudioSample meetingId={run.meeting_id} channel={channel} startedMs={segment.started_ms} endedMs={segment.ended_ms} /> : <p className="text-sm text-slate-400">{preview ? 'Канал исходной записи не определён.' : 'Загрузка аудиоссылки…'}</p>}
    {!single && <p className="text-sm text-amber-200">{reason ? exclusionReason(reason) : 'В этой реплике голос не определён однозначно'}. Можно сохранить отдельное решение «без имени» для этого анализа.</p>}
    <label className="field-label">Ручное решение<select disabled={busy || stale} value={participantId} onChange={(event) => setParticipantId(event.target.value)}><option value="">Оставить без имени</option>{single && participants.filter((participant) => participant.enabled).map((participant) => <option key={participant.id} value={participant.id}>{participant.display_name}</option>)}</select></label>
    <label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" disabled={!channel || busy || stale} checked={listening} onChange={(event) => setListening(event.target.checked)} /><span>Я прослушал эту реплику и подтверждаю отдельное решение. Оно имеет приоритет над назначением группе.</span></label>
    <Button disabled={!listening || !channel || busy || stale || (Boolean(participantId) && !single)} onClick={() => { void save(); }}>{busy ? 'Сохранение…' : 'Сохранить отдельное решение'}</Button>
    {error && <div role="alert" className="text-sm text-amber-200"><p>{error}</p><Button variant="ghost" className="mt-2" onClick={onRefresh}>Обновить анализ и проверить</Button></div>}
  </div>;
}

function AudioSample({ meetingId, channel, startedMs, endedMs }: { meetingId: string; channel: string; startedMs: number; endedMs: number }) {
  return <audio aria-label={`Аудиофрагмент ${timeOffset(startedMs)}–${timeOffset(endedMs)}`} controls preload="none" className="w-full" src={`${api.audioUrl(meetingId, channel)}${audioFragment(startedMs, endedMs)}`} onPlay={(event) => {
    if (event.currentTarget.currentTime < startedMs / 1000 || event.currentTarget.currentTime >= endedMs / 1000) event.currentTarget.currentTime = startedMs / 1000;
  }} onTimeUpdate={(event) => {
    if (event.currentTarget.currentTime >= endedMs / 1000) event.currentTarget.pause();
  }}>Ваш браузер не поддерживает воспроизведение аудио.</audio>;
}
