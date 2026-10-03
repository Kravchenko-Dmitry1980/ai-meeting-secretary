import { ApiError, request } from './api.ts';

export interface PersonProfile {
  id: string; display_name: string; aliases: string[]; enabled: boolean; revision: number;
  created_at: string; updated_at: string;
}
export type EnrollmentStatus = 'pending' | 'awaiting_review' | 'ready' | 'revoked' | 'failed' | 'interrupted' | 'cancelled' | 'cleanup_pending';
export interface Enrollment {
  id: string; person_profile_id: string; material_version: number; revision: number;
  consent_confirmed: boolean; status: EnrollmentStatus; model_id: string | null;
  model_revision: string | null; created_at: string; revoked_at: string | null;
  reason_codes: string[]; listening_confirmed: boolean; single_speaker_confirmed: boolean;
}
export interface EnrollmentRecording {
  recording_id: string; person_profile_id: string; enrollment_id: string; generation: number;
  status: 'starting' | 'recording' | 'stopping' | 'processing' | 'awaiting_review' | 'cancelled' | 'failed' | 'interrupted' | 'cleanup_pending';
  disposition: 'review' | 'cancel'; duration_ms: number; reason_codes: string[];
}
export interface ProfileDraft { display_name: string; aliases: string[]; enabled: boolean }
export interface StopRecordingCommand { recording_id: string; generation: number; disposition: 'review' | 'cancel'; operation_id: string }
type Requester = <T>(path: string, init?: RequestInit) => Promise<T>;
const root = (id: string) => `/participants/${encodeURIComponent(id)}`;
export function createParticipantsClient(send: Requester = request) {
  const write = <T,>(path: string, body: unknown, method = 'POST') => send<T>(path, { method, body: JSON.stringify(body) });
  return {
    list: () => send<PersonProfile[]>('/participants'),
    create: (draft: ProfileDraft, operation_id: string) => write<PersonProfile>('/participants', { ...draft, operation_id }),
    patch: (id: string, draft: ProfileDraft, expected_revision: number, operation_id: string) => write<PersonProfile>(root(id), { ...draft, expected_revision, operation_id }, 'PATCH'),
    enrollments: (id: string) => send<Enrollment[]>(`${root(id)}/enrollments`),
    upload: (id: string, file: File, consent: boolean, operation_id: string) => {
      const body = new FormData(); body.append('file', file); body.append('consent_confirmed', String(consent)); body.append('operation_id', operation_id);
      return send<Enrollment>(`${root(id)}/enrollments`, { method: 'POST', body });
    },
    start: (id: string, microphone_id: string, consent_confirmed: boolean, operation_id: string) => write<EnrollmentRecording>(`${root(id)}/enrollment-recording/start`, { microphone_id, consent_confirmed, operation_id }),
    stop: (id: string, command: StopRecordingCommand) => write<EnrollmentRecording>(`${root(id)}/enrollment-recording/stop`, command),
    recording: async (id: string, recordingId?: string) => {
      try { return await send<EnrollmentRecording>(`${root(id)}/enrollment-recording${recordingId ? `?recording_id=${encodeURIComponent(recordingId)}` : ''}`); }
      catch (error) { if (error instanceof ApiError && error.status === 404) return null; throw error; }
    },
    confirm: (id: string, enrollment: Enrollment, operation_id: string) => write<Enrollment>(`${root(id)}/enrollments/${encodeURIComponent(enrollment.id)}/confirm`, { expected_revision: enrollment.revision, listening_confirmed: true, single_speaker_confirmed: true, operation_id }),
    remove: (id: string, enrollment: Enrollment, operation_id: string) => write<Enrollment>(`${root(id)}/enrollments/${encodeURIComponent(enrollment.id)}`, { expected_revision: enrollment.revision, operation_id }, 'DELETE'),
    previewUrl: (id: string, enrollment: Enrollment) => `/api/v1${root(id)}/enrollments/${encodeURIComponent(enrollment.id)}/audio-preview?expected_revision=${enrollment.revision}`,
  };
}
export const participantsApi = createParticipantsClient();
