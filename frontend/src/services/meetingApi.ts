import type {
  FrontendSettings,
  MeetingDetails,
  MeetingResults,
  MeetingSummaryResponse,
  SegmentItem,
  TaskItem,
  TranscriptItem,
} from '../types/meeting';

const asRecord = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' ? (value as Record<string, unknown>) : {};

const unwrapObjectPayload = (value: unknown): Record<string, unknown> => {
  const record = asRecord(value);
  const nested = record.data ?? record.result ?? record.payload;
  if (nested && typeof nested === 'object' && !Array.isArray(nested)) {
    return nested as Record<string, unknown>;
  }
  return record;
};

const unwrapArrayPayload = (value: unknown): unknown[] => {
  if (Array.isArray(value)) return value;
  const record = asRecord(value);
  const nested =
    record.items ??
    record.tasks ??
    record.transcript ??
    record.segments ??
    record.data ??
    record.result ??
    record.payload;
  if (Array.isArray(nested)) return nested;
  return [];
};

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

  // Backend can expose multiple status fields simultaneously.
  // If any of them reports completion, prioritize it immediately.
  for (const candidate of directCandidates) {
    if (typeof candidate === 'string' && candidate.trim().toLowerCase() === 'done') {
      return candidate;
    }
  }

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
      if (typeof nestedStatus === 'string' && nestedStatus.trim().toLowerCase() === 'done') {
        return nestedStatus;
      }
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
  const [rawSummary, rawTasks, rawTranscript, rawSegments] = await Promise.all([
    request<unknown>(`/api/v1/meetings/${meetingId}/summary`, settings),
    request<unknown>(`/api/v1/meetings/${meetingId}/tasks`, settings),
    request<unknown>(`/api/v1/meetings/${meetingId}/transcript`, settings),
    request<unknown>(`/api/v1/meetings/${meetingId}/segments`, settings),
  ]);

  const summaryPayload = unwrapObjectPayload(rawSummary);
  const summary: MeetingSummaryResponse = {
    summary: String(summaryPayload.summary ?? summaryPayload.text ?? ''),
    key_decisions: Array.isArray(summaryPayload.key_decisions)
      ? summaryPayload.key_decisions.map((item) => String(item))
      : [],
    action_recap: Array.isArray(summaryPayload.action_recap)
      ? summaryPayload.action_recap.map((item) => String(item))
      : [],
  };

  const tasks: TaskItem[] = unwrapArrayPayload(rawTasks).map((item, index) => {
    const record = asRecord(item);
    return {
      id: String(record.id ?? record.task_id ?? index + 1),
      title: String(record.title ?? record.task ?? record.name ?? ''),
      assignee: record.assignee ? String(record.assignee) : undefined,
      priority:
        record.priority === 'high' || record.priority === 'medium' || record.priority === 'low'
          ? record.priority
          : undefined,
      due_date: record.due_date ? String(record.due_date) : undefined,
      confidence:
        typeof record.confidence === 'number'
          ? record.confidence
          : typeof record.confidence === 'string'
            ? Number(record.confidence)
            : undefined,
      source_quote: record.source_quote ? String(record.source_quote) : undefined,
    };
  });

  const transcriptArray = unwrapArrayPayload(rawTranscript);
  const transcriptRecord = asRecord(rawTranscript);
  const transcript: TranscriptItem[] =
    transcriptArray.length > 0
      ? transcriptArray.map((item) => {
          const record = asRecord(item);
          return {
            speaker: String(record.speaker ?? record.speaker_name ?? 'Неизвестный'),
            timestamp: String(record.timestamp ?? record.time ?? ''),
            text: String(record.text ?? record.content ?? ''),
          };
        })
      : typeof transcriptRecord.transcript === 'string'
        ? transcriptRecord.transcript
            .split('\n')
            .map((line) => line.trim())
            .filter(Boolean)
            .map((line, index) => {
              const withSpeaker = line.match(/^([^[]+)\s+\[([0-9:.]+)[^\]]*\]:\s*(.+)$/);
              if (withSpeaker) {
                return {
                  speaker: withSpeaker[1].trim(),
                  timestamp: withSpeaker[2].trim(),
                  text: withSpeaker[3].trim(),
                };
              }
              return {
                speaker: 'Неизвестный',
                timestamp: String(index + 1),
                text: line,
              };
            })
        : [];

  const segments: SegmentItem[] = unwrapArrayPayload(rawSegments).map((item) => {
    const record = asRecord(item);
    return {
      speaker: String(record.speaker ?? record.speaker_name ?? 'Неизвестный'),
      start: Number(record.start ?? 0) || 0,
      end: Number(record.end ?? 0) || 0,
    };
  });

  if (import.meta.env.DEV) {
    // Temporary debug helper to verify normalized result shape.
    console.log('[meeting-results] normalized', { summary, tasks, transcript, segments });
  }

  return { summary, tasks, transcript, segments };
};
