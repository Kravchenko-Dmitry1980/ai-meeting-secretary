import { request } from './api.ts';
import type { components } from '../generated/api';

// Canonical Task5b schemas. The UI deliberately always captures participant_id
// (including explicit null) to retain the reviewed immutable command contract.
// participant_id identifies a meeting participant, never a global profile.
export type AssignmentScope = components['schemas']['AssignmentScope'];
export type ResolvedAssignment = components['schemas']['ResolvedAssignment'];
export type AssignmentBasis = ResolvedAssignment['basis'];
export type AssignmentStatus = ResolvedAssignment['status'];
export type AssignmentSnapshot = components['schemas']['AssignmentSnapshot'];
export type AssignmentChange = Required<components['schemas']['TaskAssignmentChange']>;
export type AssignmentCommand = Omit<components['schemas']['ReviewTaskAssignments'], 'changes'> & { changes: AssignmentChange[] };
export type AssignmentReviewResult = components['schemas']['AssignmentReviewResult'];
type Requester = <T>(path: string, init?: RequestInit) => Promise<T>;
const root = (id: string) => `/meetings/${encodeURIComponent(id)}/task-assignments`;
export function createTaskAssignmentsClient(send: Requester = request) {
  return {
    read: (id: string, summaryVersion?: number, revision?: number) => {
      const query = new URLSearchParams();
      for (const [key, value] of [['summary_version', summaryVersion], ['revision', revision]] as const) {
        if (value === undefined) continue;
        if (!Number.isSafeInteger(value) || value < 0) throw new Error('Некорректная версия итогов.');
        query.set(key, String(value));
      }
      return send<AssignmentSnapshot>(`${root(id)}${query.size ? `?${query}` : ''}`);
    },
    review: (id: string, command: AssignmentCommand) => send<AssignmentReviewResult>(root(id), { method: 'PATCH', body: JSON.stringify(command) }),
  };
}
export const taskAssignmentsApi = createTaskAssignmentsClient();
