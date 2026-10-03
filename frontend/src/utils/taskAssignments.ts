import { ApiError } from '../services/api.ts';
import type { AssignmentChange, AssignmentCommand, AssignmentSnapshot, ResolvedAssignment } from '../services/taskAssignments.ts';

export interface AssignmentParticipant { id: string; display_name: string; enabled: boolean }
export const ASSIGNMENT_BATCH_MAX = 1000;
export function assignmentStamp(item: ResolvedAssignment): string {
  return JSON.stringify([item.action.scope, item.action.action_id, item.revision, item.dependency_signature, item.participant_id, item.status, item.confirm_eligible]);
}
export function canConfirmAssignment(item: ResolvedAssignment, roster: readonly AssignmentParticipant[]): boolean {
  // Revalidated evidence may require human review while remaining eligible.
  // The server owns eligibility; an explicit click supplies that human review.
  return ['proposed', 'needs_review'].includes(item.status) && ['named_person', 'self_commitment'].includes(item.basis)
    && item.confirm_eligible && item.participant_id !== null &&
    roster.filter((person) => person.id === item.participant_id && person.enabled).length === 1;
}
export function captureAssignmentCommand(snapshot: AssignmentSnapshot, changes: AssignmentChange[], roster: readonly AssignmentParticipant[], operationId: string): AssignmentCommand {
  if (!changes.length || changes.length > ASSIGNMENT_BATCH_MAX || new Set(changes.map((item) => item.action_id)).size !== changes.length) throw new Error('Отметьте от 1 до 1000 разных задач.');
  if (!operationId || [snapshot.transcript_version, snapshot.summary_version, snapshot.revision, snapshot.attribution_revision, snapshot.roster_revision].some((value) => !Number.isSafeInteger(value) || value < 0)) throw new Error('Обновите состояние задач перед сохранением.');
  for (const change of changes) {
    const matches = snapshot.items.filter((item) => item.action.action_id === change.action_id);
    const item = matches[0];
    if (matches.length !== 1 || item.action.scope.meeting_id !== snapshot.meeting_id || item.action.scope.transcript_version !== snapshot.transcript_version || item.action.scope.summary_version !== snapshot.summary_version) throw new Error('Задача относится к другой версии итогов.');
    if (change.decision === 'confirm_proposal' && (snapshot.captured_context_hash === null || !canConfirmAssignment(item, roster) || (change.participant_id !== null && change.participant_id !== item.participant_id))) throw new Error('Предложение изменилось или требует ручного выбора.');
    if (change.decision === 'set_manual' && (!change.participant_id || roster.filter((person) => person.id === change.participant_id && person.enabled).length !== 1)) throw new Error('Выберите доступного участника этой встречи.');
    if (change.decision === 'clear' && change.participant_id !== null) throw new Error('При снятии назначения ответственный должен быть пустым.');
    if (!['confirm_proposal', 'set_manual', 'clear'].includes(change.decision)) throw new Error('Неизвестное действие с задачей.');
  }
  // Freeze the full CAS tuple and every change, so retry cannot observe later drafts.
  return Object.freeze({ transcript_version: snapshot.transcript_version, summary_version: snapshot.summary_version,
    attribution_revision: snapshot.attribution_revision, roster_revision: snapshot.roster_revision,
    expected_revision: snapshot.revision, operation_id: operationId,
    changes: Object.freeze(changes.map((change) => Object.freeze({ ...change }))) as unknown as AssignmentChange[],
  });
}
export function assignmentUncertain(error: unknown): boolean {
  return !(error instanceof ApiError) || error.status >= 500 || error.status === 408 || (error.status === 409 && error.message === 'operation_in_progress');
}
export function assignmentError(error: unknown): string {
  if (assignmentUncertain(error)) return 'Результат сохранения неизвестен. Повторите тот же запрос; новые правки останутся в черновике.';
  if (error instanceof ApiError && error.status === 409) return 'Состояние изменилось. Черновик сохранён; актуальные назначения обновляются. Проверьте их перед новым сохранением.';
  if (error instanceof ApiError && error.status === 404) return 'Итоги или назначения этой версии пока недоступны. Обновите состояние после подготовки итогов.';
  return 'Не удалось сохранить назначение. Проверьте доступность участника и актуальность источника.';
}
const reasons: Record<string, string> = {
  draft_evidence_valid: 'Есть основание для предложения; нужна проверка человеком.',
  human_confirmed_assignment: 'Назначение проверено человеком.', human_manual_choice: 'Ответственный выбран человеком.', human_cleared: 'Ответственный снят человеком.',
  named_owner_missing: 'Названный человек отсутствует в составе встречи.', named_owner_ambiguous: 'Имя соответствует нескольким участникам.',
  missing_named_owner: 'В предложении нет имени ответственного.', generic_named_owner: 'Общее обозначение не определяет человека.', named_owner_not_in_quote: 'Имя не подтверждено этой цитатой.',
  missing_evidence: 'Дословная цитата отсутствует.', quote_not_found: 'Цитата не найдена в источнике.', ambiguous_quote_anchor: 'Цитата встречается несколько раз.',
  missing_sources: 'Нет ссылок на исходные реплики.', missing_source: 'Исходная реплика недоступна.', foreign_source: 'Источник относится к другой расшифровке.', scope_mismatch: 'Версии источника и итогов различаются.',
  duplicate_source_reference: 'Ссылки на источники повторяются.', duplicate_source_id: 'Источник неоднозначен.', duplicate_action_evidence: 'Несколько задач имеют одинаковое основание.',
  unknown_basis: 'Основание назначения не определено.', insufficient_commitment_evidence: 'Личное обязательство не установлено.', collective_or_indefinite_commitment: 'Коллективная фраза не определяет одного ответственного.',
  self_requires_commitment: 'Личное обязательство требует отдельной проверки.', missing_commitment_source: 'Не указана реплика личного обязательства.', commitment_source_not_cited: 'Реплика обязательства отсутствует среди источников.', commitment_anchor_mismatch: 'Цитата не соответствует реплике обязательства.',
  contradiction_question: 'Источник содержит вопрос.', contradiction_hypothesis: 'Источник содержит предположение.', contradiction_reported_speech: 'Источник передаёт чужие слова.', contradiction_negation: 'Источник содержит отрицание.',
  identity_missing: 'Личность говорящего не установлена.', identity_stale: 'Привязка говорящего изменилась.', identity_unmapped: 'Говорящий не связан с участником.', identity_ambiguous: 'Привязка говорящего неоднозначна.', identity_ambiguous_author: 'Автора реплики нельзя определить однозначно.', identity_foreign: 'Привязка относится к другой расшифровке.',
  participant_missing: 'Участник отсутствует в составе встречи.', participant_disabled: 'Участник отключён.', participant_foreign: 'Участник относится к другой встрече.', participant_ambiguous: 'Участник определён неоднозначно.',
  manual_evidence_changed: 'Источник ручного назначения изменился.', source_evidence_changed: 'Исходное основание изменилось.', derived_evidence_changed: 'Предложение ответственного изменилось.', carryover_requires_human_review: 'Предложение из прежних итогов требует новой проверки.',
  legacy_assignment_projection: 'Прежние итоги доступны без сохранённой проверки назначений.',
};
export function assignmentReason(code: string): string {
  if (reasons[code]) return reasons[code];
  if (code.startsWith('manual_') && reasons[code.slice(7)]) return `Ручное назначение: ${reasons[code.slice(7)]}`;
  if (code.startsWith('semantic_')) return 'Смысл фразы требует проверки: она не доказывает принятие обязательства.';
  if (code.startsWith('identity_')) return 'Личность автора или наложение голосов требует проверки.';
  return 'Основание назначения требует проверки.';
}
