import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../services/api';
import { participantsApi } from '../services/participants';
import type { Enrollment, EnrollmentRecording, PersonProfile } from '../services/participants';
import { bindDialogKeyboard, canConfirm, captureNeedsClosure, enrollmentError, enrollmentNeedsPoll, recordingNeedsPoll, recordingStatusText, statusLabel, uncertainOperation, validateSample } from '../utils/enrollment';
import { Button } from './ui/Button';
import { Card } from './ui/Card';

interface Props { profile: PersonProfile; onClose: () => void; onChanged?: () => void }
interface PendingAction { kind: 'start' | 'stop' | 'upload' | 'confirm' | 'delete'; run: () => Promise<unknown>; beforeIds: string[]; target?: string; capture?: Pick<EnrollmentRecording, 'recording_id' | 'generation'> }
export function EnrollmentWizard({ profile, onClose, onChanged }: Props) {
  const [enrollments, setEnrollments] = useState<Enrollment[]>([]);
  const [recording, setRecording] = useState<EnrollmentRecording | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [consent, setConsent] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [microphones, setMicrophones] = useState<{ id: string; name: string }[]>([]);
  const [microphone, setMicrophone] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [review, setReview] = useState({ key: '', listened: false, singleSpeaker: false });
  const [preview, setPreview] = useState(false);
  const [closeNotice, setCloseNotice] = useState(false);
  const [closing, setClosing] = useState(false);
  const modal = useRef<HTMLElement>(null);
  const audio = useRef<HTMLAudioElement>(null);
  const locked = useRef(false);
  const alive = useRef(true);
  const epoch = useRef(0);
  const recordingRef = useRef<EnrollmentRecording | null>(null);
  const materialFingerprint = useRef<string | null>(null);
  const pending = useRef<PendingAction | null>(null);
  const callbacks = useRef({ onClose, onChanged });
  useEffect(() => { callbacks.current = { onClose, onChanged }; }, [onClose, onChanged]);
  const releasePreview = useCallback(() => {
    const player = audio.current;
    if (player) { player.pause(); player.removeAttribute('src'); player.load(); }
    setPreview(false);
  }, []);
  const requestClose = useCallback(() => {
    if (!loaded) { setCloseNotice(true); setError('Сначала дождитесь загрузки состояния записи. Если сервер недоступен, повторите обновление.'); return; }
    if (locked.current) { setCloseNotice(true); setError('Дождитесь ответа и обновления состояния перед закрытием.'); return; }
    if (captureNeedsClosure(recordingRef.current) || pending.current) { setCloseNotice(true); return; }
    releasePreview(); setFile(null); callbacks.current.onClose();
  }, [loaded, releasePreview]);
  useEffect(() => bindDialogKeyboard(modal.current, requestClose), [requestClose]);
  useEffect(() => {
    alive.current = true;
    const requestEpoch = epoch;
    return () => { alive.current = false; ++requestEpoch.current; };
  }, []);
  const refresh = useCallback(async () => {
    if (locked.current) return;
    const requestEpoch = ++epoch.current;
    try {
      const operation = pending.current;
      const recordingId = operation?.kind === 'start' ? undefined
        : operation?.kind === 'stop' ? operation.capture?.recording_id
        : recordingNeedsPoll(recordingRef.current) ? recordingRef.current?.recording_id : undefined;
      const [items, capture] = await Promise.all([participantsApi.enrollments(profile.id), participantsApi.recording(profile.id, recordingId)]);
      if (!alive.current || requestEpoch !== epoch.current) return;
      if (!capture && captureNeedsClosure(recordingRef.current)) {
        setError('Сервер не нашёл известную запись. Закрытие не подтверждено; повторите обновление или остановку.');
      } else { recordingRef.current = capture; setRecording(capture); }
      setEnrollments(items); setLoaded(true);
      const fingerprint = JSON.stringify(items.map((item) => [item.id, item.revision, item.status]));
      if (materialFingerprint.current !== null && fingerprint !== materialFingerprint.current) callbacks.current.onChanged?.();
      materialFingerprint.current = fingerprint;
      if (operation) {
        const target = items.find((item) => item.id === operation.target);
        const reconciled = operation.kind === 'start' ? !!capture && !operation.beforeIds.includes(capture.recording_id)
          : operation.kind === 'stop' ? !!capture && capture.recording_id === operation.capture?.recording_id && capture.generation === operation.capture?.generation && !captureNeedsClosure(capture)
          : operation.kind === 'upload' ? false
          : operation.kind === 'confirm' ? target?.status === 'ready'
          : target?.status === 'revoked' || target?.status === 'cleanup_pending';
        if (reconciled) {
          pending.current = null; setUncertain(false); setError('');
          if (operation.kind === 'start' || operation.kind === 'upload' || operation.kind === 'delete') setConsent(false);
          callbacks.current.onChanged?.();
        }
      }
      if (closing && !captureNeedsClosure(recordingRef.current) && !pending.current) {
        releasePreview(); callbacks.current.onClose();
      }
    } catch (failure) { if (alive.current && requestEpoch === epoch.current) setError(enrollmentError(failure)); }
  }, [profile.id, closing, releasePreview]);
  useEffect(() => {
    let stopped = false;
    let timer: number;
    const poll = async () => { await refresh(); if (!stopped) timer = window.setTimeout(() => { void poll(); }, 1500); };
    void poll();
    const requestEpoch = epoch;
    return () => { stopped = true; window.clearTimeout(timer); ++requestEpoch.current; };
  }, [refresh]);
  const perform = async (action?: PendingAction) => {
    if (locked.current) return;
    const operation = pending.current ?? action;
    if (!operation) return;
    locked.current = true; ++epoch.current; releasePreview(); setBusy(true); setError(''); pending.current = operation;
    try {
      const result = await operation.run();
      if (!alive.current) return;
      if (operation.kind === 'start' || operation.kind === 'stop') { const capture = result as EnrollmentRecording; recordingRef.current = capture; setRecording(capture); }
      else { const enrollment = result as Enrollment; setEnrollments((items) => [enrollment, ...items.filter((item) => item.id !== enrollment.id)]); setSelected(enrollment.id); }
      pending.current = null; setUncertain(false); callbacks.current.onChanged?.();
      if (operation.kind === 'start' || operation.kind === 'upload' || operation.kind === 'delete') setConsent(false);
      if (operation.kind === 'upload') setFile(null);
      releasePreview(); setReview({ key: '', listened: false, singleSpeaker: false });
    } catch (failure) {
      if (!alive.current) return;
      setError(enrollmentError(failure));
      if (uncertainOperation(failure)) setUncertain(true);
      else { pending.current = null; setUncertain(false); }
    } finally {
      locked.current = false;
      if (alive.current) { setBusy(false); await refresh(); }
    }
  };
  const stop = (disposition: 'review' | 'cancel', closeAfter = false) => {
    const capture = recordingRef.current;
    if (!capture) return;
    if (closeAfter) setClosing(true);
    const command = { recording_id: capture.recording_id, generation: capture.generation, disposition, operation_id: crypto.randomUUID() };
    void perform({ kind: 'stop', capture: { recording_id: command.recording_id, generation: command.generation }, beforeIds: [], run: () => participantsApi.stop(profile.id, command) });
  };
  const selectedEnrollment = enrollments.find((item) => item.id === selected) ?? enrollments.find((item) => item.id === recording?.enrollment_id) ?? enrollments.find((item) => item.status === 'awaiting_review' || item.status === 'ready');
  const materialKey = selectedEnrollment ? `${selectedEnrollment.id}:${selectedEnrollment.revision}` : '';
  const previousMaterial = useRef(materialKey);
  useEffect(() => {
    const player = audio.current;
    return () => { if (player) { player.pause(); player.removeAttribute('src'); player.load(); } };
  }, [preview, materialKey]);
  useEffect(() => {
    if (previousMaterial.current === materialKey) return;
    previousMaterial.current = materialKey;
    // Async reset avoids reusing human checks after a polled material revision.
    queueMicrotask(() => { if (alive.current) { releasePreview(); setReview({ key: '', listened: false, singleSpeaker: false }); } });
  }, [materialKey, releasePreview]);
  const blocked = busy || uncertain || !loaded || captureNeedsClosure(recording) || recording?.status === 'processing' || enrollmentNeedsPoll(enrollments);
  const choose = (id: string) => { releasePreview(); setReview({ key: '', listened: false, singleSpeaker: false }); setSelected(id); };
  const updateReview = (field: 'listened' | 'singleSpeaker', value: boolean) => setReview((previous) => ({ ...(previous.key === materialKey ? previous : { key: materialKey, listened: false, singleSpeaker: false }), [field]: value }));
  const loadDevices = async () => {
    try {
      const list = await api.devices(); if (!alive.current) return;
      setMicrophones(list.devices.filter((item) => item.kind === 'microphone'));
      if (!list.available) setError(list.error || 'Микрофоны недоступны. Проверьте локальный сервер.');
    } catch (failure) { if (alive.current) setError(enrollmentError(failure)); }
  };
  const upload = () => {
    if (!file || !consent || blocked) return;
    const validation = validateSample(file); if (validation) { setError(validation); return; }
    const sample = file; const operationId = crypto.randomUUID();
    void perform({ kind: 'upload', beforeIds: enrollments.map((item) => item.id), run: () => participantsApi.upload(profile.id, sample, true, operationId) });
  };
  return <section ref={modal} role="dialog" aria-modal="true" aria-labelledby="enrollment-title" className="settings-modal">
    <header className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 p-5">
      <h2 id="enrollment-title" className="min-w-0 break-words text-xl font-semibold">Голосовой образец: {profile.display_name}</h2>
      <Button variant="ghost" onClick={requestClose}>Закрыть</Button>
    </header>
    <div className="space-y-5 p-5">
      <p className="text-sm leading-6 text-slate-300">Один человек, до 30 секунд и 10 МиБ. Голос хранится локально. Готовый образец не подтверждает точность узнавания: качество в вашей комнате требует отдельной калибровки.</p>
      {!profile.enabled && <p role="status">Профиль выключен. Добавление и подтверждение образцов недоступны.</p>}
      {error && <p role="alert" className="break-words text-sm text-rose-200">{error}</p>}
      <div role="status" aria-live="polite" className="text-sm text-slate-300">{!loaded ? 'Загрузка состояния…' : recording ? recordingStatusText(recording, enrollments) : 'Активной записи нет'}{busy ? ' · Выполняется запрос…' : ''}</div>
      {recording?.reason_codes.map((reason) => <p key={reason} className="text-sm text-amber-200">{enrollmentError(new Error(reason))}</p>)}
      <div className="flex flex-wrap gap-2">
        <Button variant="ghost" disabled={busy} onClick={() => { void refresh(); }}>Обновить состояние</Button>
        {uncertain && <Button disabled={busy} onClick={() => { void perform(); }}>Повторить ту же операцию</Button>}
      </div>
      {closeNotice && <Card className="space-y-3 border-amber-300/30">
        <p role="status">Перед закрытием дождитесь ответа и явного закрытия микрофона. При ошибке остановку можно повторить.</p>
        <div className="flex flex-wrap gap-2">
          <Button disabled={busy || uncertain || !recording || !captureNeedsClosure(recording)} onClick={() => stop('review', true)}>Остановить и сохранить для проверки</Button>
          <Button variant="danger" disabled={busy || uncertain || !recording || !captureNeedsClosure(recording)} onClick={() => stop('cancel', true)}>Отменить запись и закрыть</Button>
          <Button variant="ghost" disabled={busy} onClick={() => { setCloseNotice(false); setClosing(false); }}>Остаться</Button>
        </div>
      </Card>}
      <Card className="space-y-4">
        <label className="checkbox-label"><input type="checkbox" checked={consent} disabled={blocked} onChange={(event) => setConsent(event.target.checked)} /><span>Участник дал согласие на локальное хранение образца и узнавание голоса</span></label>
        <label className="field-label">Аудио- или видеофайл<input type="file" accept="audio/*,video/*" disabled={blocked || !profile.enabled} onChange={(event) => { const sample = event.target.files?.[0] ?? null; setFile(sample); setError(sample ? validateSample(sample) ?? '' : ''); }} /></label>
        <Button disabled={blocked || !profile.enabled || !file || !consent || !!validateSample(file)} onClick={upload}>Загрузить образец</Button>
        <div className="border-t border-white/10 pt-4">
          <Button variant="ghost" disabled={blocked || !profile.enabled} onClick={() => { void loadDevices(); }}>Показать микрофоны</Button>
          <label className="field-label mt-3">Микрофон<select value={microphone} disabled={blocked} onChange={(event) => setMicrophone(event.target.value)}><option value="">Выберите микрофон</option>{microphones.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
          <div className="mt-3 flex flex-wrap gap-2">
            <Button disabled={blocked || !profile.enabled || !consent || !microphone} onClick={() => { const id = crypto.randomUUID(); const device = microphone; const beforeIds = recording ? [recording.recording_id] : []; void perform({ kind: 'start', beforeIds, run: () => participantsApi.start(profile.id, device, true, id) }); }}>Начать запись · максимум 30 с</Button>
            <Button disabled={busy || uncertain || !captureNeedsClosure(recording)} onClick={() => stop('review')}>Остановить для проверки</Button>
            <Button variant="danger" disabled={busy || uncertain || !captureNeedsClosure(recording)} onClick={() => stop('cancel')}>Отменить запись</Button>
          </div>
        </div>
      </Card>
      <label className="field-label">Сохранённые образцы<select value={selectedEnrollment?.id ?? ''} disabled={busy} onChange={(event) => choose(event.target.value)}><option value="">Нет выбранного образца</option>{enrollments.map((item) => <option key={item.id} value={item.id}>Образец {item.material_version} · {statusLabel(item.status)}</option>)}</select></label>
      {selectedEnrollment && <Card className="space-y-3">
        <h3 className="font-semibold">{statusLabel(selectedEnrollment.status)}</h3>
        {selectedEnrollment.reason_codes.map((reason) => <p key={reason} className="text-sm text-amber-200">{enrollmentError(new Error(reason))}</p>)}
        {['awaiting_review', 'ready'].includes(selectedEnrollment.status) && <>
          <Button variant="ghost" disabled={busy || captureNeedsClosure(recording) || recording?.status === 'processing'} onClick={() => { if (!locked.current && !captureNeedsClosure(recordingRef.current)) setPreview(true); }}>Открыть прослушивание</Button>
          {preview && <audio key={materialKey} ref={audio} controls preload="none" className="w-full" aria-label="Прослушивание выбранного образца" src={participantsApi.previewUrl(profile.id, selectedEnrollment)} onError={() => setError('Не удалось прослушать образец. Обновите состояние и проверьте его revision.')} />}
        </>}
        {selectedEnrollment.status === 'awaiting_review' && <>
          <label className="checkbox-label"><input type="checkbox" checked={review.key === materialKey && review.listened} disabled={busy || uncertain} onChange={(event) => updateReview('listened', event.target.checked)} /><span>Я прослушал(а) весь образец</span></label>
          <label className="checkbox-label"><input type="checkbox" checked={review.key === materialKey && review.singleSpeaker} disabled={busy || uncertain} onChange={(event) => updateReview('singleSpeaker', event.target.checked)} /><span>В образце говорит только этот участник</span></label>
          <Button disabled={blocked || review.key !== materialKey || !canConfirm(selectedEnrollment, review.listened, review.singleSpeaker, profile.enabled)} onClick={() => { const target = selectedEnrollment; const id = crypto.randomUUID(); void perform({ kind: 'confirm', target: target.id, beforeIds: [], run: () => participantsApi.confirm(profile.id, target, id) }); }}>Подтвердить образец</Button>
        </>}
        {!['revoked', 'cancelled', 'cleanup_pending'].includes(selectedEnrollment.status) && <Button variant="danger" disabled={busy || uncertain || captureNeedsClosure(recording)} onClick={() => { const target = selectedEnrollment; const id = crypto.randomUUID(); void perform({ kind: 'delete', target: target.id, beforeIds: [], run: () => participantsApi.remove(profile.id, target, id) }); }}>Удалить образец и отозвать согласие</Button>}
      </Card>}
      {(recordingNeedsPoll(recording) || enrollmentNeedsPoll(enrollments)) && <p className="text-xs text-slate-400">Состояние проверяется на сервере. Закрытие HTTP-запроса не останавливает устройство.</p>}
    </div>
  </section>;
}
