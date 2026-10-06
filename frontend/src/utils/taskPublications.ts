import { ApiError } from '../services/api.ts';
import type { DeliveryReceipt, PublicationCandidate, PublicationContext, PublicationIntent, PublicationItemReceipt, PublicationScope, PreviewPublications, PublishCommand, PublishPreview } from '../services/taskPublications.ts';

export interface PublicationDraft {
  selected: boolean; assigneeId: string; dueResolution: 'unresolved' | 'date' | 'none'; dueLocal: string;
  intent: PublicationIntent | ''; targetPublicationId: string; separateId: string | null;
}
export const publicationScopeEqual = (a: PublicationScope, b: PublicationScope): boolean =>
  a.meeting_id === b.meeting_id && a.transcript_version === b.transcript_version && a.summary_version === b.summary_version && a.destination_project_id === b.destination_project_id;
export const publicationEligible = (candidate: PublicationCandidate): boolean => candidate.eligible
  && !candidate.reason_codes.some((code) => ['team_member_unmapped', 'team_member_ambiguous', 'team_member_mapping_conflict', 'team_member_mapping_required', 'participant_disabled'].includes(code));
export function initialPublicationDraft(candidate: PublicationCandidate): PublicationDraft {
  return { selected: false, assigneeId: candidate.assignee_id ?? '', dueResolution: 'unresolved', dueLocal: '',
    intent: candidate.possible_supersedes.length ? '' : 'publish', targetPublicationId: '', separateId: null };
}
export function moscowInputToUtc(input: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(input);
  if (!match) throw new Error('Выберите конкретную дату и время по Москве.');
  const [year, month, day, hour, minute] = match.slice(1).map(Number);
  const date = new Date(Date.UTC(year, month - 1, day, hour, minute));
  if (year < 100 || date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day || date.getUTCHours() !== hour || date.getUTCMinutes() !== minute) throw new Error('Проверьте календарную дату и время.');
  return new Date(date.getTime() - 3 * 60 * 60 * 1000).toISOString();
}
export function formatPublicationDue(value: string | null | undefined): string {
  if (!value) return 'Без срока';
  if (!Number.isFinite(Date.parse(value))) return 'Дата требует проверки';
  return `${new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Europe/Moscow' }).format(new Date(value))} · Москва`;
}
export function capturePublicationSelections(context: PublicationContext, drafts: Record<string, PublicationDraft>): PreviewPublications {
  const chosen = context.candidates.filter((row) => drafts[row.action_id]?.selected);
  if (!chosen.length || chosen.length > 100 || new Set(chosen.map((row) => row.action_id)).size !== chosen.length) throw new Error('Выберите от 1 до 100 разных задач.');
  return { scope: { ...context.scope }, selections: chosen.map((row) => {
    const draft = drafts[row.action_id];
    if (!publicationEligible(row) || !row.evidence_quote || !row.source_segment_ids.length) throw new Error('Источник задачи требует проверки.');
    if (context.members.filter((member) => member.id === draft.assigneeId).length !== 1) throw new Error('Выберите действующего участника команды.');
    if (row.assignee_id && row.assignee_id !== draft.assigneeId) throw new Error('Измените связь участника с аккаунтом команды в настройках назначения.');
    if (draft.dueResolution === 'unresolved') throw new Error('Подтвердите конкретную дату или выберите «Без срока».');
    const intent = draft.intent;
    if (!intent || !['publish', 'link', 'propose_update', 'create_separate'].includes(intent) || (row.possible_supersedes.length && intent === 'publish')) throw new Error('Выберите действие с прежней задачей.');
    let target: string | null = null;
    if (intent === 'link' || intent === 'propose_update') {
      const matches = row.possible_supersedes.filter((item) => item.publication_id === draft.targetPublicationId && item.verified && item.task_id !== null && item.task_id !== undefined);
      if (matches.length !== 1) throw new Error('Выберите проверенную прежнюю задачу.');
      target = draft.targetPublicationId;
    }
    if (intent === 'create_separate' && !draft.separateId) throw new Error('Подтвердите создание отдельной задачи.');
    return { action_id: row.action_id, assignee_id: draft.assigneeId, due_resolution: draft.dueResolution,
      due_at: draft.dueResolution === 'date' ? moscowInputToUtc(draft.dueLocal) : null, intent,
      target_publication_id: target, separate_id: intent === 'create_separate' ? draft.separateId : null };
  }) };
}
function freeze<T>(value: T): T {
  if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); }
  return value;
}
function sameWatermarks(a: PublishPreview['watermarks'], b: PublicationContext['watermarks']): boolean {
  return a.assignment_revision === b.assignment_revision && a.roster_revision === b.roster_revision
    && a.attribution_revision === b.attribution_revision && a.context_hash === b.context_hash;
}
export function capturePublicationCommand(preview: PublishPreview, context: PublicationContext, operationId: string, now: number): PublishCommand {
  if (!publicationScopeEqual(preview.scope, context.scope) || !sameWatermarks(preview.watermarks, context.watermarks)) throw new Error('Источник изменился. Подготовьте новый предпросмотр.');
  if (preview.expires_at && (!Number.isFinite(Date.parse(preview.expires_at)) || Date.parse(preview.expires_at) <= now)) throw new Error('Предпросмотр истёк. Подготовьте его заново.');
  if (!preview.items.length || preview.items.some((item) => !item.due_confirmed || !item.evidence_quote || !context.members.some((member) => member.id === item.assignee_id))) throw new Error('Предпросмотр требует проверки.');
  return freeze(JSON.parse(JSON.stringify({ ...preview, operation_id: operationId })) as PublishCommand);
}
export function publicationApplied(receipt: DeliveryReceipt, item: PublicationItemReceipt): boolean {
  const gateway = item.gateway_receipt; const state = item.execution_state;
  return receipt.acceptance_receipt.operation_id === receipt.operation_id && receipt.acceptance_receipt.decision === 'accepted'
    && state.state === 'applied' && !!gateway && gateway.execution_state.state === 'applied'
    && gateway.acceptance_receipt.decision === 'accepted' && gateway.acceptance_receipt.operation_id === item.delivery_operation_id
    && state.task_id !== null && state.task_id !== undefined && state.task_id === gateway.execution_state.task_id
    && !!gateway.execution_state.verified_at && Number.isFinite(Date.parse(gateway.execution_state.verified_at))
    && !!gateway.current && gateway.current.task_id === state.task_id && gateway.current.project_id === receipt.scope.destination_project_id;
}
export function publicationItemLabel(receipt: DeliveryReceipt, item: PublicationItemReceipt): string {
  if (publicationApplied(receipt, item)) return item.intent === 'link' ? 'Связано' : item.intent === 'propose_update' ? 'Предложение передано' : 'Опубликовано';
  const labels = { queued: 'Ожидает отправки', running: 'Отправляется', reconciling: 'Проверяется результат', uncertain: 'Результат неизвестен · требуется сверка', conflict: 'Состояние изменилось · требуется проверка', rejected: 'Не принято', applied: 'Результат не подтверждён' };
  return labels[item.execution_state.state] ?? 'Результат не подтверждён';
}
export function publicationPending(receipt: DeliveryReceipt): boolean {
  return receipt.acceptance_receipt.decision === 'accepted' && receipt.items.some((item) => ['queued', 'running', 'reconciling', 'uncertain'].includes(item.execution_state.state) || (item.execution_state.state === 'applied' && !publicationApplied(receipt, item)));
}
export function mergePublicationReceipt(previous: DeliveryReceipt | undefined, incoming: DeliveryReceipt, scope: PublicationScope): DeliveryReceipt {
  if (!publicationScopeEqual(incoming.scope, scope) || incoming.acceptance_receipt.operation_id !== incoming.operation_id || new Set(incoming.items.map((item) => item.action_id)).size !== incoming.items.length) throw new Error('Получена квитанция другой операции.');
  if (previous && (previous.operation_id !== incoming.operation_id || JSON.stringify(previous.acceptance_receipt) !== JSON.stringify(incoming.acceptance_receipt) || previous.items.length !== incoming.items.length)) throw new Error('Квитанция операции изменилась.');
  return { ...incoming, items: incoming.items.map((item) => {
    if (!Number.isSafeInteger(item.execution_state.revision) || item.execution_state.revision < 0) throw new Error('Некорректная версия квитанции.');
    const old = previous?.items.find((row) => row.action_id === item.action_id);
    if (previous && !old) throw new Error('Квитанция относится к другой задаче.');
    if (old && (old.publication_id !== item.publication_id || old.delivery_operation_id !== item.delivery_operation_id || old.intent !== item.intent)) throw new Error('Квитанция относится к другой задаче.');
    if (old && item.execution_state.revision < old.execution_state.revision) return old;
    if (old && item.execution_state.revision === old.execution_state.revision && item.execution_state.state !== old.execution_state.state) throw new Error('Противоречивое состояние квитанции.');
    if (old && item.execution_state.revision === old.execution_state.revision && (item.execution_state.task_id !== old.execution_state.task_id || item.execution_state.verified_at !== old.execution_state.verified_at)) throw new Error('Противоречивое подтверждение квитанции.');
    if (old && publicationApplied(previous!, old) && item.execution_state.state !== 'applied') throw new Error('Подтверждённая операция не может снова ожидать отправки.');
    return item;
  }) };
}
const reasons: Record<string, string> = {
  team_member_selection_required: 'Выберите ответственного из команды.', team_member_mapping_required: 'Настройте связь участника с аккаунтом команды.',
  publication_due_requires_resolution: 'Выберите конкретную дату или «Без срока».', due_requires_resolution: 'Выберите конкретную дату или «Без срока».',
  publication_supersedes_requires_decision: 'Выберите действие с прежней задачей.', possible_supersedes_requires_decision: 'Выберите действие с прежней задачей.',
  quote_not_found: 'Цитата не подтверждена источником.', assignment_not_confirmed: 'Сначала проверьте назначение ответственного.',
  participant_disabled: 'Участник отключён; настройте его связь с командой.', source_stale: 'Источник изменился.', correction_required: 'Требуется отдельное исправление.',
  team_member_unmapped: 'Настройте связь голосового профиля с участником команды.', team_member_ambiguous: 'Несколько аккаунтов связаны с профилем. Исправьте настройку команды.',
  team_member_mapping_conflict: 'Выбранный аккаунт не совпадает с профилем. Исправьте настройку команды.', team_member_unavailable: 'Участник команды недоступен. Проверьте настройку команды.',
  due_resolution_required: 'Выберите конкретную дату или «Без срока».', supersedes_decision_required: 'Выберите действие с прежней задачей.',
  already_published: 'Задача уже опубликована; проверьте квитанцию ниже.', publication_already_accepted: 'Публикация уже принята; проверьте состояние ниже.',
  publication_target_unavailable: 'Прежняя карточка недоступна. Обновите её состояние.', publication_target_ambiguous: 'Прежняя карточка неоднозначна. Требуется проверка.',
  publication_payload_too_large: 'Задача превышает допустимый размер. Уточните её перед публикацией.',
};
export const publicationReason = (code: string): string => reasons[code] ?? 'Проверьте источник, назначение и выбранные значения.';
export function publicationError(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) return 'Источник или задача изменились. Черновик сохранён; обновите предпросмотр.';
  if (error instanceof ApiError && error.status === 403) return 'Недостаточно прав для публикации в этот проект.';
  if (error instanceof ApiError && error.status === 422) return 'Проверьте выбранные задачи, ответственных и сроки.';
  if (error instanceof ApiError && error.status === 413) return 'Задача превышает допустимый размер. Уточните её перед публикацией.';
  return 'Не удалось получить подтверждение. Проверьте состояние операции.';
}
