import { request } from './api.ts';
import type { components } from '../generated/api';

// Canonical Task5b schemas. The UI deliberately always captures participant_id
// (including explicit null) to retain the reviewed immutable command contract.
// participant_id is a meeting attendee. assignee_profile_id is a company person
// who did not attend; it is omitted when the choice is an attendee.
export type AssignmentScope = components['schemas']['AssignmentScope'];
type GeneratedAssignment = components['schemas']['ResolvedAssignment'];
export type ResolvedAssignment = Omit<GeneratedAssignment, 'basis'> & {
  basis: GeneratedAssignment['basis'] | 'all_attendees';
  audience?: 'person' | 'all_attendees' | 'unassigned';
  assignee_profile_id?: string | null;
};
export type AssignmentBasis = ResolvedAssignment['basis'];
export type AssignmentStatus = ResolvedAssignment['status'];
export type AssignmentSnapshot = Omit<components['schemas']['AssignmentSnapshot'], 'items'> & { items: ResolvedAssignment[] };
type GeneratedChange = Required<components['schemas']['TaskAssignmentChange']>;
export type AssignmentChange = Omit<GeneratedChange, 'decision' | 'assignee_profile_id'> & {
  decision: GeneratedChange['decision'] | 'assign_all' | 'leave_unassigned';
  assignee_profile_id?: string | null;
};
export type AssignmentCommand = Omit<components['schemas']['ReviewTaskAssignments'], 'changes'> & { changes: AssignmentChange[] };
export type AssignmentReviewResult = components['schemas']['AssignmentReviewResult'];
export type AssignmentAutofillResult = AssignmentReviewResult & { applied: number; remaining: number; reused: boolean };
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
    autofill: (id: string) => send<AssignmentAutofillResult>(`${root(id)}/autofill`, { method: 'POST', body: '{}' }),
  };
}
export const taskAssignmentsApi = createTaskAssignmentsClient();
