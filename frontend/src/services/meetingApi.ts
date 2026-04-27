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
  if (normalized.includes('prepare')) return 'preparing';
  if (normalized.includes('transcrib')) return 'transcribing';
  if (normalized.includes('speaker')) return 'detecting_speakers';
  if (normalized.includes('summary')) return 'writing_summary';
  if (normalized.includes('task')) return 'extracting_tasks';
  if (normalized.includes('final')) return 'finalizing';
  if (normalized.includes('complete') || normalized.includes('done')) return 'completed';
  if (normalized.includes('fail') || normalized.includes('error')) return 'failed';
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
  const response = await fetch(`${settings.apiUrl}${path}`, {
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
  return {
    id: String(raw.id ?? meetingId),
    status: mapStatus(String(raw.status ?? raw.state ?? 'uploaded')),
    estimatedSeconds: Number(raw.estimated_seconds ?? 0) || undefined,
    error: typeof raw.error === 'string' ? raw.error : undefined,
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
