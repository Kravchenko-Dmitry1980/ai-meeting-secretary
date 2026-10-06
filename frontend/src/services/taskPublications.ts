import { request } from './api.ts';
import type { components } from '../generated/api';

export type PublicationContext = components['schemas']['PublicationContext'];
export type PublicationCandidate = components['schemas']['PublicationCandidate'];
export type PublicationScope = components['schemas']['PublicationScope'];
export type PublicationSelection = components['schemas']['PublicationSelection'];
export type PreviewPublications = components['schemas']['PreviewPublications'];
export type PublicationPreviewResult = components['schemas']['PublicationPreviewResult'];
export type PublishPreview = components['schemas']['PublishPreview'];
export type PublishCommand = components['schemas']['PublishCommand'];
export type DeliveryReceipt = components['schemas']['DeliveryReceipt'];
export type PublicationItemReceipt = components['schemas']['PublicationItemReceipt'];
export type PublicationIntent = PublicationSelection['intent'];

type Requester = <T>(path: string, init?: RequestInit) => Promise<T>;
const root = (id: string) => `/meetings/${encodeURIComponent(id)}/task-publications`;
export function createTaskPublicationsClient(send: Requester = request) {
  return {
    context: (id: string) => send<PublicationContext>(`${root(id)}/context`),
    preview: (id: string, body: PreviewPublications) => send<PublicationPreviewResult>(`${root(id)}/preview`, { method: 'POST', body: JSON.stringify(body) }),
    confirm: (id: string, body: PublishCommand) => send<DeliveryReceipt>(root(id), { method: 'POST', body: JSON.stringify(body) }),
    list: (id: string) => send<DeliveryReceipt[]>(root(id)),
    read: (id: string, operationId: string) => send<DeliveryReceipt>(`${root(id)}/${encodeURIComponent(operationId)}`),
  };
}
export type TaskPublicationsClient = ReturnType<typeof createTaskPublicationsClient>;
export const taskPublicationsApi = createTaskPublicationsClient();
