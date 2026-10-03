import { useCallback, useEffect, useRef, useState } from 'react';
import { api, errorMessage } from '../services/api';
import type { AppConfig, AudioChunk, DeviceList, EventSnapshot, IdentificationState, Meeting, ModelCatalog, ProcessingJob, ProcessingMode, RecordingState, SegmentPage, Speaker, Summary, Usage } from '../types/api';

export function useSecretary() {
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectionRevision, setSelectionRevision] = useState(0);
  const [meeting, setMeeting] = useState<Meeting | null>(null);
  const [jobs, setJobs] = useState<ProcessingJob[]>([]);
  const [identification, setIdentification] = useState<IdentificationState | null>(null);
  const [chunks, setChunks] = useState<AudioChunk[]>([]);
  const [speakers, setSpeakers] = useState<Speaker[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [summaryStatus, setSummaryStatus] = useState('new');
  const [segments, setSegments] = useState<SegmentPage>({ items: [], total: 0, transcript_version: 0 });
  const [pageOffset, setPageOffset] = useState(0);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [models, setModels] = useState<ModelCatalog | null>(null);
  const [devices, setDevices] = useState<DeviceList | null>(null);
  const [recording, setRecording] = useState<RecordingState | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [globalError, setGlobalError] = useState<string | null>(null);
  const [meetingError, setMeetingError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [sseConnected, setSseConnected] = useState(false);
  const currentId = useRef<string | null>(null);
  const currentSelectionRevision = useRef(0);
  const loadGeneration = useRef(0);
  const globalLoadGeneration = useRef(0);
  const actionInFlight = useRef(false);
  const actionOwner = useRef<number | null>(null);
  const error = actionError ?? meetingError ?? globalError;
  const setError = (value: string | null) => {
    setActionError(value);
    // Explicit user dismissal can clear all visible errors. Successful reads
    // below clear only their own error and cannot erase another failed action.
    if (value === null) { setMeetingError(null); setGlobalError(null); }
  };

  const refreshGlobal = useCallback(async () => {
    const generation = ++globalLoadGeneration.current;
    const data = await Promise.allSettled([api.meetings(), api.config(), api.models(), api.devices()]);
    if (generation !== globalLoadGeneration.current) return;
    if (data[0].status === 'fulfilled') {
      const list = data[0].value;
      setMeetings(list);
      setSelectedId((previous) => previous ?? list[0]?.id ?? null);
    }
    if (data[1].status === 'fulfilled') setConfig(data[1].value);
    if (data[2].status === 'fulfilled') setModels(data[2].value);
    if (data[3].status === 'fulfilled') setDevices(data[3].value);
    const failure = data.find((item) => item.status === 'rejected');
    setGlobalError(failure?.status === 'rejected' ? errorMessage(failure.reason) : null);
    setLoading(false);
  }, []);

  const refreshMeeting = useCallback(async () => {
    if (!selectedId || currentId.current !== selectedId || currentSelectionRevision.current !== selectionRevision) return;
    const id = selectedId;
    const generation = ++loadGeneration.current;
    const data = await Promise.allSettled([api.meeting(id), api.jobs(id), api.chunks(id), api.segments(id, pageOffset), api.summary(id), api.recording(id), api.usage(id), api.speakers(id)]);
    if (currentId.current !== id || currentSelectionRevision.current !== selectionRevision || generation !== loadGeneration.current) return;
    if (data[0].status === 'fulfilled') {
      setMeeting(data[0].value);
      const value = data[0].value;
      setMeetings((previous) => previous.map((item) => item.id === id ? value : item));
    }
    if (data[1].status === 'fulfilled') setJobs(data[1].value);
    if (data[2].status === 'fulfilled') setChunks(data[2].value);
    if (data[3].status === 'fulfilled') setSegments(data[3].value);
    if (data[4].status === 'fulfilled') {
      const result = data[4].value;
      setSummary('overview' in result ? result : null);
      setSummaryStatus(result.status);
    }
    if (data[5].status === 'fulfilled') setRecording(data[5].value);
    if (data[6].status === 'fulfilled') setUsage(data[6].value);
    if (data[7].status === 'fulfilled') setSpeakers(data[7].value);
    if (data[0].status === 'fulfilled' && data[1].status === 'fulfilled') {
      const version = data[0].value.transcript_version;
      const local = data[1].value.filter((job) => job.stage === 'identify_speakers' && job.version === version && job.run_id)
        .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.updated_at.localeCompare(a.updated_at))[0];
      if (!local?.run_id) setIdentification(null);
      else {
        try {
          const result = await api.identificationRun(id, local.run_id);
          if (currentId.current !== id || currentSelectionRevision.current !== selectionRevision || generation !== loadGeneration.current) return;
          if (result.run_id !== local.run_id || result.transcript_version !== version) throw new Error('Состояние узнавания относится к другой версии. Обновите встречу.');
          setIdentification(result);
        } catch (failure) {
          if (currentId.current !== id || currentSelectionRevision.current !== selectionRevision || generation !== loadGeneration.current) return;
          setIdentification(null); setMeetingError(errorMessage(failure)); return;
        }
      }
    }
    const failure = data.find((item) => item.status === 'rejected');
    setMeetingError(failure?.status === 'rejected' ? errorMessage(failure.reason) : null);
  }, [selectedId, pageOffset, selectionRevision]);

  useEffect(() => {
    const timer = window.setTimeout(() => { void refreshGlobal(); }, 0);
    return () => window.clearTimeout(timer);
  }, [refreshGlobal]);
  useEffect(() => {
    const timer = window.setTimeout(() => { void refreshMeeting(); }, 0);
    return () => window.clearTimeout(timer);
  }, [refreshMeeting]);
  useEffect(() => {
    if (!selectedId) return;
    currentId.current = selectedId;
    const stream = new EventSource(api.eventsUrl(selectedId));
    let lastSnapshot = '';
    let connected = false;
    let pendingTimer: number | undefined;
    stream.onopen = () => { if (currentSelectionRevision.current !== selectionRevision) return; connected = true; setSseConnected(true); };
    stream.onerror = () => { if (currentSelectionRevision.current !== selectionRevision) return; connected = false; setSseConnected(false); };
    const onSnapshot = (event: MessageEvent<string>) => {
      if (currentId.current !== selectedId || currentSelectionRevision.current !== selectionRevision) return;
      try {
        const snapshot: EventSnapshot = JSON.parse(event.data);
        if (snapshot.meeting?.id !== selectedId || currentSelectionRevision.current !== selectionRevision) return;
        const fingerprint = JSON.stringify({
          meeting: { id: snapshot.meeting.id, title: snapshot.meeting.title, status: snapshot.meeting.status,
            transcript_version: snapshot.meeting.transcript_version, duration_ms: snapshot.meeting.duration_ms, error: snapshot.meeting.error },
          jobs: snapshot.jobs.map(({ id, stage, status, attempts, error }) => ({ id, stage, status, attempts, error })),
          segment_count: snapshot.segment_count,
        });
        if (fingerprint === lastSnapshot) return;
        lastSnapshot = fingerprint;
        setMeeting(snapshot.meeting);
        setJobs(snapshot.jobs);
        if (pendingTimer !== undefined) window.clearTimeout(pendingTimer);
        pendingTimer = window.setTimeout(() => { void refreshMeeting(); }, 400);
      } catch { setMeetingError('Не удалось прочитать событие сервера. Обновите состояние встречи.'); }
    };
    stream.addEventListener('status', onSnapshot as EventListener);
    const recoveryPoll = window.setInterval(() => { if (!connected) void refreshMeeting(); }, 15000);
    // Local progress changes are not part of the SSE fingerprint. Poll the
    // exact run through the same selection/load guards even while SSE is open.
    let pollingProgress = false;
    const progressPoll = window.setInterval(() => { if (!pollingProgress) { pollingProgress = true; void refreshMeeting().finally(() => { pollingProgress = false; }); } }, 5000);
    return () => {
      stream.close();
      if (pendingTimer !== undefined) window.clearTimeout(pendingTimer);
      window.clearInterval(recoveryPoll);
      window.clearInterval(progressPoll);
    };
  }, [selectedId, selectionRevision, refreshMeeting]);

  const selectMeeting = (id: string) => {
    // Selecting the same meeting still starts a fresh load after its artifacts are cleared.
    setSelectionRevision(++currentSelectionRevision.current);
    ++loadGeneration.current;
    currentId.current = id; setSelectedId(id); setPageOffset(0);
    setMeeting(meetings.find((item) => item.id === id) ?? null);
    setJobs([]); setIdentification(null); setChunks([]); setSpeakers([]); setSummary(null); setRecording(null); setUsage(null);
    setSegments({ items: [], total: 0, transcript_version: 0 }); setSummaryStatus('new'); setMeetingError(null); setActionError(null); setSseConnected(false);
  };
  const act = async (operation: () => Promise<unknown>) => {
    if (actionInFlight.current) return false;
    actionInFlight.current = true;
    actionOwner.current = currentSelectionRevision.current;
    setBusy(true); setActionError(null);
    try { await operation(); await refreshGlobal(); await refreshMeeting(); return actionOwner.current === currentSelectionRevision.current; }
    catch (failure) { if (actionOwner.current === currentSelectionRevision.current) setActionError(errorMessage(failure)); return false; }
    finally { actionInFlight.current = false; actionOwner.current = null; setBusy(false); }
  };
  const createMeeting = async (title: string, mode: ProcessingMode = 'ordinary'): Promise<Meeting> => {
    const owner = currentSelectionRevision.current;
    const result = await api.create(title, mode);
    setMeetings((previous) => [result, ...previous]);
    if (owner !== currentSelectionRevision.current) throw new Error('Встреча создана в библиотеке. Выбор изменился; запись или импорт для неё не запущены.');
    setSelectionRevision(++currentSelectionRevision.current);
    if (actionInFlight.current && actionOwner.current === owner) actionOwner.current = currentSelectionRevision.current;
    ++loadGeneration.current;
    currentId.current = result.id; setSelectedId(result.id); setMeeting(result); setPageOffset(0);
    setJobs([]); setIdentification(null); setChunks([]); setSpeakers([]); setSummary(null); setRecording(null); setUsage(null);
    setSegments({ items: [], total: 0, transcript_version: 0 }); setSummaryStatus('new'); setMeetingError(null); setSseConnected(false);
    return result;
  };
  return { meetings, selectedId, selectionRevision, meeting, jobs, identification, chunks, speakers, summary, summaryStatus, segments, pageOffset, setPageOffset,
    config, models, devices, recording, usage, error, setError, busy, loading, sseConnected,
    selectMeeting, invalidateActions: () => setSelectionRevision(++currentSelectionRevision.current), act, createMeeting, refreshMeeting, refreshGlobal };
}
