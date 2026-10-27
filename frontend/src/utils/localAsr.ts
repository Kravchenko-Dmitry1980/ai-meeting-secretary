import type { LocalAsrClusterCommand, LocalAsrClusterPreview, LocalAsrRun, LocalAsrSegment } from '../types/localAsr';

export function asrTime(milliseconds: number): string {
  if (!Number.isSafeInteger(milliseconds) || milliseconds < 0) return 'Время неизвестно';
  const totalSeconds = Math.floor(milliseconds / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor(totalSeconds / 60) % 60;
  const seconds = totalSeconds % 60;
  return `${hours ? `${hours}:` : ''}${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(milliseconds % 1000).padStart(3, '0')}`;
}

export function audioFragment(startedMs: number, endedMs: number): string {
  if (!Number.isSafeInteger(startedMs) || !Number.isSafeInteger(endedMs) || startedMs < 0 || endedMs <= startedMs) {
    throw new Error('У фрагмента нет корректных границ для прослушивания.');
  }
  return `#t=${startedMs / 1000},${endedMs / 1000}`;
}

export function clusterCommand(preview: LocalAsrClusterPreview, selected: readonly number[], participantId: string, operationId: string): LocalAsrClusterCommand {
  const shown = preview.shown_segment_ordinals;
  const eligible = new Set(preview.eligible_segment_ordinals);
  const selectedSet = new Set(selected);
  if (!participantId || !operationId || selected.length === 0 || selectedSet.size !== selected.length
      || new Set(shown).size !== shown.length || selected.some((ordinal) => !shown.includes(ordinal) || !eligible.has(ordinal))) {
    throw new Error('Проверьте участника и точный набор реплик перед подтверждением.');
  }
  return {
    operation_id: operationId,
    expected_review_revision: preview.review_revision,
    speaker_label: preview.speaker_label,
    shown_segment_ordinals: [...shown],
    excluded_segment_ordinals: shown.filter((ordinal) => !selectedSet.has(ordinal)),
    participant_id: participantId,
    listening_confirmed: true,
  };
}

export function sameRunScope(run: Pick<LocalAsrRun, 'meeting_id' | 'run_id'>, meetingId: string, runId: string): boolean {
  return run.meeting_id === meetingId && run.run_id === runId;
}

export function sourceChannel(run: LocalAsrRun, sourceIds: readonly string[]): string | null {
  const sources = run.artifacts.source_segments ?? [];
  const allowed = new Set(['import', 'microphone', 'system', 'mixed']);
  const channels = sourceIds.map((id) => sources.find((source) => source.segment_id === id)?.channel);
  const channel = channels[0];
  return sourceIds.length > 0 && channel && allowed.has(channel) && channels.every((item) => item === channel) ? channel : null;
}

export function manualStatus(run: LocalAsrRun, ordinal: number, names: ReadonlyMap<string, string>): string {
  const sourceIds = run.artifacts.segment_sources?.find((item) => item.ordinal === ordinal)?.source_segment_ids ?? [];
  if ((run.source_decisions ?? []).some((item) => sourceIds.includes(item.source_segment_id))) return 'Сохранено ручное решение исходной реплики';
  const review = run.reviews.find((item) => item.segment_ordinal === ordinal);
  if (!review) return 'Имя не подтверждено';
  if (review.status === 'excluded' || review.participant_id == null) return 'Оставлено без имени вручную';
  return `${names.get(review.participant_id) ?? 'Участник недоступен'} · подтверждено вручную`;
}

export function overlappingTurns(run: LocalAsrRun, segment: LocalAsrSegment): boolean {
  return (run.result.speaker_turns ?? []).some((turn) => turn.speaker_label !== segment.speaker_label
    && turn.started_ms < segment.ended_ms && segment.started_ms < turn.ended_ms);
}

export function exclusionReason(reason: string): string {
  const known: Record<string, string> = {
    overlap: 'Наложение голосов', composite: 'Несколько голосов', unknown: 'Голос не определён',
    manual_override: 'Отдельное ручное решение', existing_confirmation: 'Имя уже подтверждено',
    existing_manual_attribution: 'Сохранено прежнее ручное решение', existing_manual_decision: 'Сохранено прежнее ручное решение',
    mixed_source: 'Смешанные исходные реплики', mixed_sources: 'Смешанные исходные реплики',
    no_source: 'Нет связи с исходной репликой', no_clean_audio: 'Нет чистого аудиофрагмента',
    unknown_or_composite: 'Неизвестный или смешанный голос', ambiguous_channel: 'Канал записи не определён однозначно',
    existing_manual_source_decision: 'Сохранено прежнее ручное решение', individual_override: 'Отдельное ручное решение',
    previous_confirmation: 'Имя уже подтверждено', no_independent_turn_evidence: 'Нет отдельного подтверждения интервала голоса',
    overlap_or_mixed_turn: 'Наложение или смешение голосов',
    incomplete_turn_coverage: 'Интервал голоса покрывает реплику не полностью',
  };
  return known[reason] ?? 'Требуется отдельная проверка';
}

export function reviewError(error: unknown): string {
  const status = (error as { status?: number } | null)?.status;
  if (status === 409) return 'Данные изменились. Обновите анализ и заново проверьте набор реплик перед сохранением.';
  if (status == null || status >= 500 || status === 408) return 'Результат сохранения неизвестен. Обновите анализ и проверьте имена перед следующим действием.';
  return error instanceof Error ? error.message : 'Не удалось сохранить решение. Обновите анализ.';
}
