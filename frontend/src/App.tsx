import { AlertCircle, ArrowDownToLine, AudioLines, CalendarDays, CheckCircle2, Clock3, FileAudio, FolderOpen, HardDrive, KeyRound, LoaderCircle, Mic, Monitor, Plus, Radio, RefreshCw, Search, Settings, ShieldCheck, Square, UploadCloud, Users, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { LogoMark } from './components/LogoMark';
import { MeetingResults, SourcePreview } from './components/MeetingResults';
import { ProcessingStages } from './components/ProcessingStages';
import { SettingsPanel } from './components/SettingsPanel';
import { ParticipantsPanel } from './components/ParticipantsPanel';
import { MeetingRoster, NewMeetingParticipants } from './components/MeetingRoster';
import type { RosterDraft } from './components/MeetingRoster';
import { SpeakerReview } from './components/SpeakerReview';
import { TranscriptView } from './components/TranscriptView';
import { Badge } from './components/ui/Badge';
import { Button } from './components/ui/Button';
import { Card } from './components/ui/Card';
import { Tabs } from './components/ui/Tabs';
import { useSecretary } from './hooks/useSecretary';
import { useSpeakerWorkflow } from './hooks/useSpeakerWorkflow';
import { api, errorMessage } from './services/api';
import { speakersApi } from './services/speakers';
import type { AddParticipant } from './services/speakers';
import type { Meeting, ProcessingMode, TranscriptSegment } from './types/api';
import { formatBinaryBytes, importLimitHint, timeOffset } from './utils/format';
import { buildProcessingState, requiresTranscriptionRetry } from './utils/processing';

const meetingStatusText: Record<string, string> = {
  new: 'Новая встреча', uploading: 'Загрузка файла', preparing: 'Подготовка аудио', prepared: 'Аудио подготовлено', queued: 'В очереди',
  recording: 'Запись', transcribe: 'Расшифровка', transcribing: 'Расшифровка', summarize: 'Формирование итогов', identify_speakers: 'Локальное узнавание участников', identified: 'Локальный результат сохранён',
  waiting_config: 'Нужен API-ключ / настройка', paused_budget: 'Лимит расходов', partial_error: 'Частичная ошибка',
  ready: 'Готово', partial_ready: 'Готово по сохранённой части', done: 'Готово', succeeded: 'Готово', interrupted: 'Запись прервана', recorded: 'Запись сохранена', failed: 'Ошибка', cancelled: 'Отменено', transcribed: 'Расшифровка сохранена',
};
const prettyDate = (value: string) => new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }).format(new Date(value));
const money = (value: number | null | undefined) => value == null ? 'Неизвестно' : `${value.toLocaleString('ru-RU', { maximumFractionDigits: 2 })} ₽`;
const tabs = ['Итоги', 'Задачи', 'Участники встречи', 'Расшифровка'];

function App() {
  const model = useSecretary();
  const [newWorkspace, setNewWorkspace] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [participantsOpen, setParticipantsOpen] = useState(false);
  const [profilePanelGeneration, setProfilePanelGeneration] = useState(0);
  const [processingMode, setProcessingMode] = useState<ProcessingMode>('ordinary');
  const [rosterDraft, setRosterDraft] = useState<RosterDraft>({ profileIds: [], guests: [] });
  const closeSettings = useCallback(() => setSettingsOpen(false), []);
  const [mode, setMode] = useState<'import' | 'record'>('import');
  const [title, setTitle] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [search, setSearch] = useState('');
  const [tab, setTab] = useState('Итоги');
  const [microphoneId, setMicrophoneId] = useState('default');
  const [systemId, setSystemId] = useState('default');
  const [voiceMicrophoneId, setVoiceMicrophoneId] = useState('default');
  const [voiceSystemId, setVoiceSystemId] = useState('off');
  const [autoProcess, setAutoProcess] = useState(true);
  const [highlightedId, setHighlightedId] = useState<string | null>(null);
  const [sourcePreview, setSourcePreview] = useState<TranscriptSegment | null>(null);
  const [sourcePreviewScope, setSourcePreviewScope] = useState('');
  const [exportFormat, setExportFormat] = useState('md');
  const [audioChannel, setAudioChannel] = useState('');
  const [assignmentRefresh, setAssignmentRefresh] = useState(0);
  const fileInput = useRef<HTMLInputElement>(null);
  const audio = useRef<HTMLAudioElement>(null);
  const pendingSeek = useRef<number | null>(null);
  const sourceRequest = useRef(0);
  const navigationEpoch = useRef(0);
  const setupPlans = useRef(new Map<string, { meeting: Meeting; items: Omit<AddParticipant, 'expected_roster_revision' | 'operation_id'>[]; index: number; revision: number | null; pending: AddParticipant | null }>());

  const selected = !newWorkspace ? model.meeting : null;
  const sourceScope = `${selected?.id ?? ''}:${selected?.transcript_version ?? ''}:${model.selectionRevision}`;
  useEffect(() => {
    const requests = sourceRequest, seekRequest = pendingSeek;
    return () => { ++requests.current; seekRequest.current = null; };
  }, [selected?.id, selected?.transcript_version, model.selectionRevision]);
  const effectiveMode = selected?.processing_mode ?? processingMode;
  const voiceMode = effectiveMode === 'voice_identification';
  const speakerWorkflow = useSpeakerWorkflow(selected?.id ?? '', selected?.transcript_version ?? 0, model.selectionRevision);
  const identityRevision = `${speakerWorkflow.roster?.roster_revision ?? ''}:${speakerWorkflow.snapshot?.revision ?? ''}`;
  const lastIdentity = useRef<{ scope: string; revision: string } | null>(null);
  const refreshMeeting = model.refreshMeeting;
  const selectionRevision = model.selectionRevision;
  useEffect(() => {
    if (!selected || !speakerWorkflow.roster || !speakerWorkflow.snapshot) return;
    const scope = `${selected.id}:${selected.transcript_version}:${selectionRevision}`;
    const previous = lastIdentity.current;
    lastIdentity.current = { scope, revision: identityRevision };
    // SSE omits local identity changes. Refresh the legacy owner/export view
    // when this scope's canonical roster or attribution changes locally.
    if (previous?.scope === scope && previous.revision !== identityRevision) void refreshMeeting();
  }, [selected, refreshMeeting, selectionRevision, speakerWorkflow.roster, speakerWorkflow.snapshot, identityRevision]);
  const assignmentRefreshKey = `${model.selectionRevision}:${identityRevision}:${assignmentRefresh}`;
  const activeRecording = !newWorkspace && Boolean(model.recording?.active || model.recording?.recording || ['recording', 'running'].includes(model.recording?.status ?? ''));
  const microphones = model.devices?.devices.filter((device) => device.kind === 'microphone') ?? [];
  const systemDevices = model.devices?.devices.filter((device) => device.kind === 'system') ?? [];
  const selectedJobs = selected ? model.jobs : [];
  const cloudBlocked = Boolean(model.config && (!model.config.key_configured || !model.config.cloud_enabled));
  const cloudDisabledWithKey = Boolean(model.config?.key_configured && !model.config.cloud_enabled);
  const cloudNoticeTitle = cloudDisabledWithKey ? 'Облачная обработка отключена' : 'Для облачной обработки нужен API-ключ';
  const localCandidate = model.identification ?? speakerWorkflow.lastLocalState;
  const localState = localCandidate?.transcript_version === selected?.transcript_version ? localCandidate : null;
  const processing = selected ? buildProcessingState(selected, selectedJobs, model.chunks, localState) : null;
  const incompleteJobs = processing?.currentJobs.filter((job) => ['failed', 'cancelled', 'uncertain', 'paused_budget', 'waiting_config'].includes(job.status)) ?? [];
  const needsTranscriptionRetry = requiresTranscriptionRetry(selected, selectedJobs, model.chunks);
  const visibleMeetings = model.meetings.filter((item) => item.title.toLocaleLowerCase('ru-RU').includes(search.toLocaleLowerCase('ru-RU')));
  const hasAudio = selected && model.chunks.length > 0;

  const chooseFile = (next: File | undefined) => {
    if (!next) return;
    setFile(next);
    if (!title) setTitle(next.name.replace(/\.[^.]+$/, '').slice(0, 250));
  };
  const closeSource = () => {
    ++sourceRequest.current;
    pendingSeek.current = null;
    setSourcePreview(null); setHighlightedId(null);
  };
  const clearSource = () => { closeSource(); setAudioChannel(''); };
  const beginNew = () => { ++navigationEpoch.current; model.invalidateActions(); clearSource(); setNewWorkspace(true); setTitle(''); setFile(null); setRosterDraft({ profileIds: [], guests: [] }); model.setError(null); };
  const ensureMeeting = async (): Promise<Meeting> => {
    const navigation = navigationEpoch.current;
    let plan = selected ? setupPlans.current.get(selected.id) : undefined;
    if (!plan) {
      // This draft belongs only to the visible new-workspace form. An existing
      // selected meeting uses its own roster or its previously captured plan.
      const useNewRosterDraft = voiceMode && !selected;
      if (useNewRosterDraft && rosterDraft.guests.some((name) => !name.trim())) throw new Error('Укажите имя каждого гостя или уберите пустую карточку.');
      const items = useNewRosterDraft ? [...rosterDraft.profileIds.map((id) => ({ person_profile_id: id, enabled: true })), ...rosterDraft.guests.map((name) => ({ display_name: name.trim(), enabled: true }))] : [];
      const result = selected?.status === 'new' && model.chunks.length === 0 ? selected : await model.createMeeting(title.trim() || (mode === 'record' ? `Встреча ${new Date().toLocaleDateString('ru-RU')}` : 'Импорт записи'), effectiveMode);
      if (navigation !== navigationEpoch.current) throw new Error('Выбор встречи изменился. Импорт или запись не запущены.');
      plan = { meeting: result, items, index: 0, revision: items.length ? null : 0, pending: null }; setupPlans.current.set(result.id, plan);
      clearSource(); setNewWorkspace(false); setTab('Итоги');
    }
    if (plan.revision == null) {
      const roster = await speakersApi.roster(plan.meeting.id); plan.revision = roster.roster_revision;
      if (navigation !== navigationEpoch.current) throw new Error('Выбор встречи изменился. Импорт или запись не запущены.');
    }
    while (plan.index < plan.items.length) {
      plan.pending ??= { ...plan.items[plan.index], expected_roster_revision: plan.revision, operation_id: crypto.randomUUID() };
      try {
        const roster = await speakersApi.add(plan.meeting.id, plan.pending);
        plan.revision = roster.roster_revision; ++plan.index; plan.pending = null;
        if (navigation !== navigationEpoch.current) throw new Error('Выбор изменился. Состав сохранён в созданной встрече; импорт или запись не запущены.');
      } catch (failure) {
        const status = (failure as { status?: number })?.status;
        if (status != null && status < 500 && status !== 408) {
          plan.pending = null;
          if (status === 409) { const roster = await speakersApi.roster(plan.meeting.id); plan.revision = roster.roster_revision; }
        }
        throw new Error(`${errorMessage(failure)} Аудио ещё не отправлено. Повтор действия продолжит состав этой встречи; неизвестный запрос будет повторён с прежним ID.`, { cause: failure });
      }
    }
    return plan.meeting;
  };
  const importFile = async () => {
    if (!file) return;
    const success = await model.act(async () => {
      const meeting = await ensureMeeting();
      await api.upload(meeting.id, file);
    });
    if (success) setFile(null);
  };
  const startRecording = async () => {
    const resolve = (choice: string, list: typeof microphones) => choice === 'off' ? '' : choice === 'default' ? (list.find((device) => device.default) ?? list[0])?.id ?? '' : choice;
    const mic = resolve(voiceMode ? voiceMicrophoneId : microphoneId, microphones);
    const system = resolve(voiceMode ? voiceSystemId : systemId, systemDevices);
    if (!mic && !system) { model.setError('Выберите микрофон или устройство системного звука.'); return; }
    await model.act(async () => {
      const meeting = await ensureMeeting();
      const state = await api.startRecording(meeting.id, mic, system, autoProcess);
      if (state.error || ['error', 'unavailable', 'failed'].includes(state.status)) throw new Error(state.error || 'Запись не запущена. Проверьте устройства.');
    });
  };
  const seek = (segment: TranscriptSegment, sourceToken?: number) => {
    const request = sourceToken ?? ++sourceRequest.current;
    pendingSeek.current = null;
    if (segment.start_ms != null && audio.current) {
      if (segment.channel !== audioChannel) {
        pendingSeek.current = segment.start_ms / 1000;
        setAudioChannel(segment.channel);
      } else {
        audio.current.currentTime = segment.start_ms / 1000;
        void audio.current.play().catch(() => { if (request === sourceRequest.current) model.setError('Не удалось воспроизвести аудио. Используйте кнопку плеера.'); });
      }
    }
  };
  const jumpToSource = async (segmentId: string, version = selected?.transcript_version) => {
    if (!selected) return;
    const request = ++sourceRequest.current;
    pendingSeek.current = null;
    try {
      const page = await api.segment(selected.id, segmentId, version);
      if (request !== sourceRequest.current) return;
      if (page.transcript_version !== version && page.transcript_version != null) throw new Error('Источник относится к другой версии расшифровки. Обновите встречу.');
      const segment = page.items.find((item) => item.id === segmentId);
      if (!segment) throw new Error('Исходный фрагмент этой версии не найден.');
      if (segment.meeting_id !== selected.id || segment.transcript_version !== version) throw new Error('Источник относится к другой встрече или версии расшифровки. Обновите встречу.');
      setSourcePreview(segment); setSourcePreviewScope(sourceScope); setHighlightedId(segmentId);
      if (version === selected.transcript_version) setTab('Расшифровка');
      seek(segment, request);
    } catch (failure) { if (request === sourceRequest.current) model.setError(errorMessage(failure)); }
  };

  const workspace = <Card className="capture-card p-0">
    <div className="capture-tabs"><button className={mode === 'import' ? 'active' : ''} type="button" onClick={() => setMode('import')}><UploadCloud size={17} />Готовая запись</button><button className={mode === 'record' ? 'active' : ''} type="button" onClick={() => setMode('record')}><Mic size={17} />Живая встреча</button></div>
    <div className="p-6 sm:p-8">
      <label className="field-label mb-5">Название встречи<input value={title} maxLength={250} onChange={(event) => setTitle(event.target.value)} placeholder="Например, Планирование релиза" /></label>
      <label className="field-label mb-4">Режим новой встречи<select value={effectiveMode} disabled={model.busy || Boolean(selected)} onChange={(event) => setProcessingMode(event.target.value as ProcessingMode)}><option value="ordinary">Обычная встреча</option><option value="voice_identification">Общий микрофон · узнавание участников</option></select><span className="field-hint">Режим сохраняется для встречи. Узнавание использует Aiesa и локальные образцы; качество и калибровка проверяются отдельно.</span></label>
      {voiceMode && !selected && <NewMeetingParticipants key={profilePanelGeneration} draft={rosterDraft} onChange={setRosterDraft} disabled={model.busy} onProfiles={() => setParticipantsOpen(true)} />}
      {mode === 'import' ? <>
        <div className={`drop-zone ${dragging ? 'dragging' : ''}`} onDragOver={(event) => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); chooseFile(event.dataTransfer.files[0]); }}>
          <span className="upload-icon"><UploadCloud size={28} /></span>
          <h3 className="mt-4 text-lg font-medium">{file ? file.name : 'Перетащите запись встречи'}</h3>
          <p className="mt-2 text-sm text-slate-400">{file ? `${formatBinaryBytes(file.size)} · лимит ${model.config ? formatBinaryBytes(model.config.max_upload_bytes) : 'не получен'} · исходный файл сохранится локально` : importLimitHint(model.config?.max_upload_bytes)}</p>
          <input ref={fileInput} id="audio-file" type="file" className="sr-only" accept="audio/*,video/*,.m4a,.mkv,.webm,.ogg,.flac" onChange={(event) => chooseFile(event.target.files?.[0])} aria-label="Выбрать аудио или видео встречи" />
          <Button variant="ghost" className="mt-5" onClick={() => fileInput.current?.click()} disabled={model.busy}>{file ? 'Выбрать другой файл' : 'Выбрать файл'}</Button>
          <p className="mt-4 text-[11px] text-slate-500">WAV, MP3, M4A, FLAC, MP4, MKV, WEBM и другие форматы FFmpeg</p>
        </div>
        <div className="mt-5 flex flex-wrap items-center justify-between gap-3"><p className="flex items-center gap-2 text-xs text-slate-500"><HardDrive size={14} />Файл сохраняется перед облачной обработкой</p><Button onClick={() => { void importFile(); }} disabled={!file || model.busy}>{model.busy ? <><LoaderCircle size={16} className="animate-spin" />Загрузка…</> : <><UploadCloud size={16} />Импортировать запись</>}</Button></div>
      </> : <>
        <div className="grid gap-4 sm:grid-cols-2"><label className="field-label"><span className="flex items-center gap-2"><Mic size={15} />Микрофон</span><select value={voiceMode ? voiceMicrophoneId : microphoneId} onChange={(event) => (voiceMode ? setVoiceMicrophoneId : setMicrophoneId)(event.target.value)} disabled={model.busy || activeRecording}><option value="default">По умолчанию</option><option value="off">Не записывать</option>{microphones.map((device) => <option key={device.id} value={device.id}>{device.name}</option>)}</select></label><label className="field-label"><span className="flex items-center gap-2"><Monitor size={15} />Системный звук (WASAPI)</span><select value={voiceMode ? voiceSystemId : systemId} onChange={(event) => (voiceMode ? setVoiceSystemId : setSystemId)(event.target.value)} disabled={model.busy || activeRecording}><option value="default">По умолчанию</option><option value="off">Не записывать</option>{systemDevices.map((device) => <option key={device.id} value={device.id}>{device.name}</option>)}</select></label></div>
        {voiceMode && <p className="mt-3 text-xs text-slate-400">Для встречи в одной комнате выбран общий микрофон; системный звук первоначально выключен. Включайте его только для нужного источника.</p>}
        {!model.devices?.available && <p role="status" className="mt-4 rounded-xl border border-amber-300/20 bg-amber-300/5 p-4 text-sm leading-6 text-amber-100">{model.devices?.error || 'Аудиоустройства пока не доступны. Обновите список устройств.'}</p>}
        <label className="checkbox-label mt-5"><input type="checkbox" checked={autoProcess} onChange={(event) => setAutoProcess(event.target.checked)} disabled={activeRecording} />Обрабатывать сохранённые фрагменты во время записи</label>
        <p className="mt-3 text-xs leading-6 text-slate-500">Микрофон и системный звук — каналы записи. Имена участников определяются отдельно. Запись сохраняется частями; сеть и API-ключ не влияют на захват.</p>
        <div className="mt-6 flex flex-wrap items-center justify-between gap-3"><Button variant="ghost" onClick={() => { void model.refreshGlobal(); }} disabled={model.busy}><RefreshCw size={14} />Обновить устройства</Button><Button onClick={() => { void startRecording(); }} disabled={model.busy || !model.devices?.available || activeRecording}><Mic size={16} />{model.busy ? 'Запуск…' : 'Начать запись'}</Button></div>
      </>}
    </div>
  </Card>;

  return <div className="app-shell">
    <aside className="sidebar">
      <a className="brand" href="#" onClick={(event) => { event.preventDefault(); beginNew(); }}><LogoMark /><div><span className="brand-name">Secretary<span className="brand-dot">.</span></span><p>Ваши встречи. По существу.</p></div></a>
      <Button onClick={beginNew} className="new-meeting-button"><Plus size={17} />Новая встреча</Button>
      <div className="sidebar-section-label">БИБЛИОТЕКА <span>{model.meetings.length}</span></div>
      <label className="search-field sidebar-search"><Search size={14} /><input type="search" value={search} onChange={(event) => setSearch(event.target.value)} aria-label="Поиск встреч" placeholder="Найти встречу" /></label>
      <nav aria-label="Список встреч" className="meeting-list">
        {model.loading ? <p className="sidebar-empty"><LoaderCircle className="animate-spin" size={16} />Загрузка встреч…</p> : visibleMeetings.length === 0 ? <div className="sidebar-empty"><FolderOpen size={23} /><p>{search ? 'Ничего не найдено' : 'Здесь появятся ваши встречи'}</p></div> : visibleMeetings.map((item) => <button key={item.id} type="button" className={`meeting-entry ${!newWorkspace && model.selectedId === item.id ? 'selected' : ''}`} onClick={() => { ++navigationEpoch.current; clearSource(); model.selectMeeting(item.id); setNewWorkspace(false); setTab('Итоги'); }}>
          <span className="meeting-entry-icon"><FileAudio size={16} /></span><span className="min-w-0 flex-1"><span className="meeting-entry-title">{item.title}</span><span className="meeting-entry-meta">{prettyDate(item.created_at)}</span><span className={`meeting-entry-status ${item.status === 'ready' ? 'ready' : ''}`}>{meetingStatusText[item.status] ?? item.status}</span></span>
        </button>)}
      </nav>
      <div className="sidebar-bottom"><Button variant="ghost" className="w-full justify-start border-transparent bg-transparent" onClick={() => setParticipantsOpen(true)}><Users size={17} />Участники</Button><Button variant="ghost" className="w-full justify-start border-transparent bg-transparent" onClick={() => setSettingsOpen(true)} disabled={!model.config}><Settings size={17} />Настройки</Button><div className="local-note"><ShieldCheck size={15} /><span>Записи хранятся на этом компьютере</span></div></div>
    </aside>

    <main className="main-pane">
      <header className="topbar"><div className="flex items-center gap-2 text-xs text-slate-500"><span className="connection-dot" /><span>Локальное рабочее пространство</span></div><div className="flex items-center gap-3"><Badge className={model.config?.key_configured && model.config.cloud_enabled ? 'border-mint/20 text-mint' : 'border-amber-300/20 text-amber-200'}><span className="h-1.5 w-1.5 rounded-full bg-current" />{model.config == null ? 'Проверка подключения…' : !model.config.cloud_enabled ? 'Облачная обработка отключена' : model.config.key_configured ? 'Polza · ключ настроен' : 'Облако не настроено'}</Badge><button type="button" className="icon-button" title="Обновить состояние" aria-label="Обновить состояние" onClick={() => { void model.refreshGlobal(); void model.refreshMeeting(); }}><RefreshCw size={16} /></button></div></header>
      <div className="workspace">
        {model.error && <div className="notice error-notice" role="alert"><AlertCircle size={19} /><div className="min-w-0 flex-1"><p className="font-medium">Не удалось завершить действие</p><p className="mt-1 whitespace-pre-wrap text-sm">{model.error}</p></div><button className="icon-button" type="button" aria-label="Закрыть сообщение" onClick={() => model.setError(null)}><X size={16} /></button></div>}
        {!selected ? <>
          <div className="welcome-heading"><p className="eyebrow">МЕНЬШЕ РУТИНЫ. БОЛЬШЕ ЯСНОСТИ.</p><h1>Сохраните разговор.<br /><span>Соберите главное.</span></h1><p>Запишите встречу или загрузите готовый файл.<br />Расшифровка, решения и задачи — в одном месте.</p></div>
          {cloudBlocked && <div className="notice setup-notice"><KeyRound size={18} /><div className="flex-1"><p className="font-medium">{cloudNoticeTitle}</p><p className="mt-1 text-xs leading-5">{cloudDisabledWithKey ? 'Запись и импорт доступны локально. Включите облачную обработку в настройках, когда будете готовы.' : 'Вы можете импортировать и сохранять записи уже сейчас.'}</p></div><Button variant="ghost" className="px-3 py-2 text-xs" onClick={() => setSettingsOpen(true)} disabled={!model.config}>Настроить</Button></div>}
          {workspace}
          <div className="principles-grid"><div><HardDrive size={18} /><h3>Запись остаётся у вас</h3><p>Аудио постепенно сохраняется на диск.</p></div><div><AudioLines size={18} /><h3>Русский и технологии</h3><p>Облачная расшифровка сохранённых фрагментов.</p></div><div><CheckCircle2 size={18} /><h3>Опора на источник</h3><p>Решения и поручения связаны с текстом встречи.</p></div></div>
        </> : <>
          <div className="meeting-heading"><div className="min-w-0"><p className="eyebrow">ВСТРЕЧА</p><h1>{selected.title}</h1><div className="meeting-meta"><span><CalendarDays size={13} />{prettyDate(selected.created_at)}</span><span><Clock3 size={13} />{selected.duration_ms == null ? 'Длительность уточняется' : timeOffset(selected.duration_ms)}</span><Badge>{meetingStatusText[selected.status] ?? selected.status}</Badge></div></div><div className="export-control"><select value={exportFormat} aria-label="Формат экспорта" onChange={(event) => setExportFormat(event.target.value)}><option value="md">Markdown</option><option value="txt">TXT</option><option value="json">JSON</option><option value="docx">DOCX</option></select><a className="button rounded-xl border border-white/10 bg-white/[0.03] px-4 py-2.5 text-sm font-semibold text-slate-200 transition hover:bg-white/[0.07]" href={api.exportUrl(selected.id, exportFormat)} download><ArrowDownToLine size={15} />Экспорт</a></div></div>
          {activeRecording && <Card className="recording-banner"><span className="recording-dot" /><div className="flex-1"><p className="font-medium">Идёт запись встречи</p><p className="mt-1 text-xs text-slate-400">Сохранено фрагментов: {model.chunks.length}. Облачная обработка не блокирует захват.</p></div><Button variant="danger" onClick={() => { void model.act(() => api.stopRecording(selected.id)); }} disabled={model.busy}><Square size={13} fill="currentColor" />Завершить запись</Button></Card>}
          {cloudBlocked && <div className="notice setup-notice"><KeyRound size={18} /><div className="flex-1"><p className="font-medium">{cloudNoticeTitle}</p><p className="mt-1 text-xs leading-5">{cloudDisabledWithKey ? 'Аудио сохраняется локально. Включите облачную обработку в настройках, затем запустите обработку этой встречи.' : 'Аудио сохраняется локально. После настройки ключа запустите обработку этой встречи.'}</p></div><Button variant="ghost" className="px-3 py-2 text-xs" onClick={() => setSettingsOpen(true)}>Настроить</Button></div>}
          {selected.error && !processing?.currentJobs.some((job) => job.error === selected.error) && <div className="notice error-notice" role="alert"><AlertCircle size={18} /><p className="whitespace-pre-wrap text-sm">{selected.error}</p></div>}
          <MeetingRoster key={`${selected.id}:${selected.transcript_version}:${model.selectionRevision}`} workflow={speakerWorkflow} />
          {speakerWorkflow.error && <div role="alert" className="notice error-notice">{speakerWorkflow.error}</div>}
          {speakerWorkflow.pending && <div className="notice setup-notice"><p className="flex-1 text-sm">Результат изменения участников неизвестен. Текущий черновик сохранён; повтор использует захваченные версию, revision и ID.</p><Button disabled={speakerWorkflow.busy} onClick={() => { void speakerWorkflow.repeat().then(() => model.refreshMeeting()); }}>Повторить прежний запрос</Button></div>}
          {selected.status === 'new' && !hasAudio && !activeRecording ? workspace : <>
            <Card className="audio-card"><div className="flex flex-wrap items-center justify-between gap-3"><div className="flex items-center gap-3"><span className="audio-icon"><AudioLines size={21} /></span><div><p className="text-sm font-medium">Запись встречи</p><p className="mt-1 text-xs text-slate-500">{hasAudio ? `${model.chunks.length} сохранённых фрагментов · ${timeOffset(selected.duration_ms)}` : 'Подготовка файла к воспроизведению'}</p></div></div><Badge className="text-slate-400"><HardDrive size={12} />Локально</Badge></div>{model.chunks.some((chunk) => chunk.channel === 'microphone' || chunk.channel === 'system') && <label className="field-label mt-4 max-w-xs">Канал воспроизведения<select value={audioChannel} onChange={(event) => { ++sourceRequest.current; pendingSeek.current = null; setAudioChannel(event.target.value); }}><option value="">Основной канал</option>{model.chunks.some((chunk) => chunk.channel === 'microphone') && <option value="microphone">Микрофон</option>}{model.chunks.some((chunk) => chunk.channel === 'system') && <option value="system">Системный звук</option>}</select></label>}{hasAudio && <audio ref={audio} controls preload="metadata" src={api.audioUrl(selected.id, audioChannel)} key={`${selected.id}-${audioChannel}-${activeRecording ? model.chunks.length : 'complete'}`} onLoadedMetadata={() => { if (pendingSeek.current != null && audio.current) { const request = sourceRequest.current; audio.current.currentTime = pendingSeek.current; pendingSeek.current = null; void audio.current.play().catch(() => { if (request === sourceRequest.current) model.setError('Для воспроизведения нажмите кнопку плеера.'); }); } }} onError={() => model.setError('Аудио пока недоступно для воспроизведения. Запись на диске сохранена.')} className="mt-5 w-full" />}</Card>
            {processing && <ProcessingStages state={processing} busy={model.busy || speakerWorkflow.busy || speakerWorkflow.pending || activeRecording} connected={model.sseConnected} onCancel={(ids) => { void model.act(async () => { for (const id of ids) await api.cancel(id); }); }} onSettings={() => setSettingsOpen(true)} onParticipants={() => setParticipantsOpen(true)} onIdentify={() => { void speakerWorkflow.identify(true).then(() => model.refreshMeeting()); }} onBypass={() => { void speakerWorkflow.bypass().then(() => model.refreshMeeting()); }} />}
            {incompleteJobs.some((job) => job.status === 'uncertain') && <div className="notice setup-notice"><AlertCircle size={18} /><p className="text-sm leading-6">Статус облачного запроса неизвестен. Он мог быть принят и оплачен. Автоматический повтор приостановлен; проверьте запрос в кабинете Polza.</p></div>}
            <div className="processing-actions"><div className="flex flex-wrap gap-2">{processing?.stages[0].jobs.some((job) => job.status === 'failed' || job.status === 'cancelled') && !hasAudio && <Button variant="ghost" disabled={model.busy || activeRecording || processing.active} onClick={() => { void model.act(() => api.process(selected.id, 'prepare', true)); }}><RefreshCw size={14} />Повторить подготовку аудио</Button>}<Button variant="ghost" disabled={model.busy || !hasAudio || activeRecording || processing?.active || speakerWorkflow.busy || speakerWorkflow.pending} onClick={() => { void model.act(() => api.process(selected.id, 'transcribe', needsTranscriptionRetry)); }}><RefreshCw size={14} />{processing?.active ? 'Обработка запущена' : needsTranscriptionRetry || model.segments.total > 0 ? 'Продолжить обработку' : 'Запустить обработку'}</Button><Button variant="ghost" disabled={model.busy || !processing?.transcriptionComplete || !processing?.identificationComplete || activeRecording || processing?.active || speakerWorkflow.busy || speakerWorkflow.pending} onClick={() => { void model.act(() => api.regenerateSummary(selected.id)); }}>Пересоздать итоги · платный анализ</Button></div><span className="events-label"><Radio size={12} className={model.sseConnected ? 'text-mint' : 'text-amber-200'} />{model.sseConnected ? 'Статус обновляется автоматически' : 'Восстанавливаем связь · проверка каждые 15 с'}</span></div>
            <p className="mb-5 text-xs leading-6 text-slate-400">Пересоздание итогов — новый платный анализ с текущими именами, составом и настройками. Прежние итоги сохраняются. «Продолжить обработку» возобновляет остановленный этап и его сохранённый контекст; ручные правки сами не запускают облачную обработку.</p>
            {(model.error?.includes('route_change_required') || selected.error?.includes('route_change_required')) && <div className="notice setup-notice"><div className="flex-1"><p className="font-medium">Для замены маршрута нужна новая версия расшифровки</p><p className="mt-1 text-sm leading-6">Это отдельный запуск: всё аудио может снова пройти платный STT. Старые результаты и квитанции сохранятся. Обычное продолжение использует прежний маршрут.</p></div><Button disabled={model.busy || activeRecording || processing?.active} onClick={() => { void model.act(() => api.process(selected.id, 'transcribe', false, true)); }}>Создать новую версию · повторный платный STT</Button></div>}
            {(model.summary?.excluded_items_count ?? 0) > 0 && <div className="notice setup-notice" role="status"><AlertCircle size={18} /><div><p className="font-medium">Некоторые пункты требуют проверки</p><p className="mt-1 text-sm leading-6">Не включено неподтверждённых пунктов из черновиков: {model.summary?.excluded_items_count}. Сверьте решения и задачи с исходной расшифровкой.</p></div></div>}
            <div className="results-heading"><Tabs tabs={tabs} active={tab} onChange={setTab} /><span className="text-xs text-slate-500">{model.segments.total} текстовых фрагментов</span></div>
            {sourcePreview && sourcePreview.meeting_id === selected.id && sourcePreviewScope === sourceScope && <div className="mb-4"><SourcePreview segment={sourcePreview} onClose={closeSource} /></div>}
            {tab === 'Участники встречи' ? <SpeakerReview key={`${selected.id}:${selected.transcript_version}:${model.selectionRevision}`} workflow={speakerWorkflow} onSource={(id) => { void jumpToSource(id); }} /> : tab === 'Расшифровка' ? <TranscriptView key={`${selected.id}:${selected.transcript_version}:${model.selectionRevision}`} page={model.segments} speakers={model.speakers} summary={model.summary} offset={model.pageOffset} onPage={model.setPageOffset} highlightedId={highlightedId} onSeek={seek} speakerWorkflow={speakerWorkflow} /> : <MeetingResults summary={model.summary} status={model.summaryStatus} tab={tab} onSource={(id, version) => { void jumpToSource(id, version); }} assignments={{ meetingId: selected.id, roster: speakerWorkflow.roster?.participants ?? [], refreshKey: assignmentRefreshKey, onChanged: (snapshot) => { if (snapshot.meeting_id === selected.id && snapshot.transcript_version === model.summary?.transcript_version && snapshot.summary_version === model.summary?.summary_version) { setAssignmentRefresh((value) => value + 1); void model.refreshMeeting(); } } }} />}
            <div className="usage-strip"><div><span>Подтверждено провайдером</span><strong>{money(model.usage?.confirmed_rub)}</strong></div><div><span>Предварительная оценка</span><strong>{money(model.usage?.estimated_rub)}</strong></div><div><span>Неизвестный расход</span><strong>{model.usage ? `${model.usage.unknown_count} запросов` : 'Не загружен'}</strong></div><div><span>{model.config?.local_cost_limits_enabled === false ? 'Контроль расходов' : 'Лимит на встречу'}</span><strong>{model.config?.local_cost_limits_enabled === false ? 'В Polza' : money(model.config?.meeting_budget_rub)}</strong></div></div>
          </>}
        </>}
        <footer className="workspace-footer"><span>Secretary · Локальная запись, облачная обработка по вашему выбору</span><span className="flex items-center gap-1.5"><ShieldCheck size={12} />Ссылки на источники</span></footer>
      </div>
    </main>
    {settingsOpen && model.config && <SettingsPanel config={model.config} catalog={model.models} busy={model.busy} onClose={closeSettings} onSave={async (draft) => model.act(() => api.saveConfig(draft))} />}
    {participantsOpen && <ParticipantsPanel onClose={() => { setParticipantsOpen(false); setProfilePanelGeneration((value) => value + 1); void speakerWorkflow.refresh(); }} onChanged={() => { void speakerWorkflow.refresh(); }} />}
  </div>;
}
export default App;
