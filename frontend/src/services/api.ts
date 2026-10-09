import type { AppConfig, AudioChunk, CloudBudgetOperationDetails, CloudBudgetOperationPage, DeviceList, IdentificationState, Meeting, ModelCatalog, PendingSummary, ProcessingJob, ProcessingMode, RecordingState, SegmentPage, Speaker, Summary, Usage } from '../types/api';

let sessionToken: string | null = null;
let sessionRequest: Promise<string> | null = null;
export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) { super(message); this.status = status; }
}
export const errorMessage = (error: unknown): string => error instanceof Error ? error.message : 'Не удалось выполнить запрос.';

async function session(): Promise<string> {
  if (sessionToken) return sessionToken;
  if (sessionRequest) return sessionRequest;
  sessionRequest = fetch('/api/v1/session').then(async (response) => {
    if (!response.ok) throw new ApiError('Не удалось открыть локальный сеанс. Обновите страницу.', response.status);
    const data: { csrf_token: string } = await response.json();
    if (!data.csrf_token) throw new Error('Сервер вернул некорректный сеанс.');
    sessionToken = data.csrf_token;
    return data.csrf_token;
  }).catch((error: unknown) => {
    if (error instanceof ApiError) throw error;
    throw new Error('Локальный сервер недоступен. Проверьте запуск Secretary и повторите действие.', { cause: error });
  }).finally(() => { sessionRequest = null; });
  return sessionRequest;
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = init?.method ?? 'GET';
  const headers = new Headers(init?.headers);
  if (method !== 'GET' && method !== 'HEAD') headers.set('X-Secretary-Token', await session());
  if (typeof init?.body === 'string') headers.set('Content-Type', 'application/json');
  let response: Response;
  try { response = await fetch(`/api/v1${path}`, { ...init, headers }); }
  catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new Error('Локальный сервер недоступен. Проверьте запуск Secretary и повторите действие.', { cause: error });
  }
  if (!response.ok) {
    let message = `Ошибка сервера: HTTP ${response.status}.`;
    try {
      const payload: { detail?: unknown; error?: unknown } = await response.json();
      if (typeof payload.detail === 'string') message = payload.detail;
      else if (typeof payload.error === 'string') message = payload.error;
      else if (Array.isArray(payload.detail)) message = payload.detail.map((item) => {
        const detail = item as { loc?: string[]; msg?: string };
        return `${(detail.loc ?? []).join('.')}: ${detail.msg ?? 'Некорректное значение'}`;
      }).join('; ');
    } catch { /* Preserve the real HTTP error if its body is not JSON. */ }
    if (response.status === 403) sessionToken = null;
    throw new ApiError(message, response.status);
  }
  return await response.json() as T;
}

const write = <T,>(path: string, body: unknown = {}, method = 'POST') => request<T>(path, { method, body: JSON.stringify(body) });
export const api = {
  meetings: () => request<Meeting[]>('/meetings'),
  create: (title: string, processingMode?: ProcessingMode) => write<Meeting>('/meetings', { title, ...(processingMode ? { processing_mode: processingMode } : {}) }),
  meeting: (id: string) => request<Meeting>(`/meetings/${encodeURIComponent(id)}`),
  jobs: (id: string) => request<ProcessingJob[]>(`/meetings/${encodeURIComponent(id)}/jobs`),
  chunks: (id: string) => request<AudioChunk[]>(`/meetings/${encodeURIComponent(id)}/chunks`),
  speakers: (id: string) => request<Speaker[]>(`/meetings/${encodeURIComponent(id)}/speakers`),
  segments: (id: string, offset = 0, limit = 50) => request<SegmentPage>(`/meetings/${encodeURIComponent(id)}/segments?offset=${offset}&limit=${limit}`),
  segment: (id: string, segmentId: string, version?: number) => request<SegmentPage>(`/meetings/${encodeURIComponent(id)}/segments?segment_id=${encodeURIComponent(segmentId)}&limit=1${version == null ? '' : `&version=${version}`}`),
  summary: (id: string) => request<Summary | PendingSummary>(`/meetings/${encodeURIComponent(id)}/summary`),
  recording: (id: string) => request<RecordingState>(`/meetings/${encodeURIComponent(id)}/recording`),
  devices: () => request<DeviceList>('/audio/devices'),
  config: () => request<AppConfig>('/config'),
  saveConfig: (config: Omit<AppConfig, 'key_configured'>) => write<AppConfig>('/config', config, 'PATCH'),
  models: () => request<ModelCatalog>('/models'),
  usage: (id?: string) => request<Usage>(`/usage${id ? `?meeting_id=${encodeURIComponent(id)}` : ''}`),
  cloudBudgetOperations: (status = 'uncertain', limit = 50, offset = 0) => request<CloudBudgetOperationPage>(`/cloud-budget/operations?status=${encodeURIComponent(status)}&limit=${limit}&offset=${offset}`),
  cloudBudgetOperation: (id: string) => request<CloudBudgetOperationDetails>(`/cloud-budget/operations/${encodeURIComponent(id)}`),
  reconcileCloudBudgetOperation: (id: string) => write<{ operation_id: string; status: string; budget: unknown }>(`/cloud-budget/operations/${encodeURIComponent(id)}/reconcile`),
  upload: (id: string, file: File) => {
    const body = new FormData(); body.append('file', file);
    return request<{ job_id: string }>(`/meetings/${encodeURIComponent(id)}/upload`, { method: 'POST', body });
  },
  process: (id: string, stage: 'prepare' | 'transcribe' | 'summarize' = 'transcribe', retry = false, newTranscriptVersion = false) => write<{ job_id: string }>(`/meetings/${encodeURIComponent(id)}/process`, { stage, retry, ...(newTranscriptVersion ? { new_transcript_version: true } : {}) }),
  regenerateSummary: (id: string) => write<{ job_id: string }>(`/meetings/${encodeURIComponent(id)}/process`, { stage: 'summarize', regenerate_summary: true }),
  identificationRun: (id: string, runId: string) => request<IdentificationState>(`/meetings/${encodeURIComponent(id)}/identification-runs/${encodeURIComponent(runId)}`),
  cancel: (id: string) => write<ProcessingJob>(`/jobs/${encodeURIComponent(id)}/cancel`),
  startRecording: (id: string, microphoneId: string, systemId: string, autoProcess: boolean) => write<RecordingState>(`/meetings/${encodeURIComponent(id)}/recording/start`, {
    ...(microphoneId ? { microphone_id: microphoneId } : {}),
    ...(systemId ? { system_id: systemId } : {}), auto_process: autoProcess,
  }),
  stopRecording: (id: string) => write<{ status: string; job_id?: string }>(`/meetings/${encodeURIComponent(id)}/recording/stop`),
  audioUrl: (id: string, channel?: string) => `/api/v1/meetings/${encodeURIComponent(id)}/audio${channel ? `?channel=${encodeURIComponent(channel)}` : ''}`,
  exportUrl: (id: string, format: string) => `/api/v1/meetings/${encodeURIComponent(id)}/export?format=${encodeURIComponent(format)}`,
  eventsUrl: (id: string) => `/api/v1/meetings/${encodeURIComponent(id)}/events`,
};
