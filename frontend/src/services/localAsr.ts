import { request } from './api';
import type { LocalAsrClusterCommand, LocalAsrClusterPreview, LocalAsrRun, LocalAsrRunSummary, LocalAsrSegmentCommand } from '../types/localAsr';

const base = (meetingId: string) => `/meetings/${encodeURIComponent(meetingId)}/local-asr/runs`;
const runPath = (meetingId: string, runId: string) => `${base(meetingId)}/${encodeURIComponent(runId)}`;
export const localAsrApi = {
  list: (meetingId: string) => request<LocalAsrRunSummary[]>(base(meetingId)),
  read: (meetingId: string, runId: string) => request<LocalAsrRun>(runPath(meetingId, runId)),
  preview: (meetingId: string, runId: string, label: string) => request<LocalAsrClusterPreview>(`${runPath(meetingId, runId)}/clusters/${encodeURIComponent(label)}/preview`),
  reviewCluster: (meetingId: string, runId: string, command: LocalAsrClusterCommand) => request<LocalAsrRun>(`${runPath(meetingId, runId)}/cluster-reviews`, { method: 'POST', body: JSON.stringify(command) }),
  reviewSegment: (meetingId: string, runId: string, command: LocalAsrSegmentCommand) => request<LocalAsrRun>(`${runPath(meetingId, runId)}/segment-reviews`, { method: 'POST', body: JSON.stringify(command) }),
  exportUrl: (meetingId: string, runId: string) => `/api/v1${runPath(meetingId, runId)}/export`,
};
