import type {
  FrontendSettings,
  MeetingDetails,
  MeetingResults,
  MeetingSummaryResponse,
  SegmentItem,
  TaskItem,
  TranscriptItem,
} from '../types/meeting';

const mapStatus = (status?: string): MeetingDetails['status'] => {
  const normalized = (status ?? '').toLowerCase();
  if (!normalized) return 'uploaded';
  if (normalized === 'queued') return 'uploaded';
  if (normalized === 'processing' || normalized === 'in_progress') return 'preparing';
  if (normalized === 'audio_ready') return 'preparing';
  if (normalized === 'transcribed') return 'transcribing';
  if (normalized === 'diarized') return 'detecting_speakers';
  if (normalized === 'summarized') return 'writing_summary';
  if (normalized === 'done') return 'completed';
  if (normalized === 'failed') return 'failed';
  if (normalized.includes('prepare')) return 'preparing';
  if (normalized.includes('transcrib')) return 'transcribing';
  if (normalized.includes('speaker') || normalized.includes('diar')) return 'detecting_speakers';
  if (normalized.includes('summary')) return 'writing_summary';
  if (normalized.includes('task')) return 'extracting_tasks';
  if (normalized.includes('final')) return 'finalizing';
  if (normalized.includes('complete') || normalized.includes('done')) return 'completed';
  if (normalized.includes('fail') || normalized.includes('error')) return 'failed';
  return 'uploaded';
};

const extractStatusValue = (raw: Record<string, unknown>): string => {
  const directCandidates = [
    raw.status,
    raw.state,
    raw.stage,
    raw.meeting_status,
    raw.job_status,
    raw.processing_status,
    raw.current_stage,
    raw.pipeline_stage,
  ];

  for (const candidate of directCandidates) {
    if (typeof candidate === 'string' && candidate.trim()) {
      return candidate;
    }
  }

  const nestedCandidates = [raw.job, raw.meeting, raw.data];
  for (const nested of nestedCandidates) {
    if (nested && typeof nested === 'object') {
      const objectValue = nested as Record<string, unknown>;
      const nestedStatus =
        objectValue.status ??
        objectValue.state ??
        objectValue.stage ??
        objectValue.meeting_status ??
        objectValue.job_status;
      if (typeof nestedStatus === 'string' && nestedStatus.trim()) {
        return nestedStatus;
      }
    }
  }

  return 'uploaded';
};

const headers = (settings: FrontendSettings): HeadersInit => ({
  'X-API-Key': settings.apiKey,
  'X-User-Email': settings.userEmail,
});

const request = async <T>(
  path: string,
  settings: FrontendSettings,
  init?: RequestInit,
): Promise<T> => {
  const isLocalBackend =
    settings.apiUrl.includes('localhost:8000') || settings.apiUrl.includes('127.0.0.1:8000');
  const baseUrl = isLocalBackend ? '' : settings.apiUrl;
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: {
      ...headers(settings),
      ...(init?.headers ?? {}),
    },
  });

  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      throw new Error('AUTH_ERROR');
    }
    throw new Error(`Request failed with status ${response.status}`);
  }

  return (await response.json()) as T;
};

export const uploadMeeting = async (
  file: File,
  settings: FrontendSettings,
): Promise<{ meeting_id: string }> => {
  const body = new FormData();
  body.append('file', file);
  return request('/api/v1/meetings/upload', settings, { method: 'POST', body });
};

export const fetchMeetingDetails = async (
  meetingId: string,
  settings: FrontendSettings,
): Promise<MeetingDetails> => {
  const raw = await request<Record<string, unknown>>(`/api/v1/meetings/${meetingId}`, settings);
  const statusValue = extractStatusValue(raw);
  const nestedError =
    typeof raw.error === 'string'
      ? raw.error
      : typeof (raw.job as Record<string, unknown> | undefined)?.error === 'string'
        ? String((raw.job as Record<string, unknown>).error)
        : undefined;
  if (import.meta.env.DEV) {
    // Debugging helper: inspect real polling payload shape in browser console.
    console.info('[meeting-polling]', {
      meetingId,
      raw,
      extracted: {
        meeting_status: raw.meeting_status,
        job_status: raw.job_status,
        stage: raw.stage,
        error: nestedError,
        normalized_status: statusValue,
      },
    });
  }
  return {
    id: String(raw.id ?? meetingId),
    status: mapStatus(statusValue),
    estimatedSeconds: Number(raw.estimated_seconds ?? 0) || undefined,
    error: nestedError,
  };
};

export const fetchMeetingResults = async (
  meetingId: string,
  settings: FrontendSettings,
): Promise<MeetingResults> => {
  const [summary, tasks, transcript, segments] = await Promise.all([
    request<MeetingSummaryResponse>(`/api/v1/meetings/${meetingId}/summary`, settings),
    request<TaskItem[]>(`/api/v1/meetings/${meetingId}/tasks`, settings),
    request<TranscriptItem[]>(`/api/v1/meetings/${meetingId}/transcript`, settings),
    request<SegmentItem[]>(`/api/v1/meetings/${meetingId}/segments`, settings),
  ]);

  return { summary, tasks, transcript, segments };
};
