import type { AttributionChange, SpeakerAttribution } from '../types/api';

export interface ReviewGroup { id: string; items: SpeakerAttribution[]; disputed: SpeakerAttribution[]; representative: SpeakerAttribution[] }
export const isDisputed = (item: SpeakerAttribution) => item.status === 'conflict' || item.reason_codes.some((code) => /overlap|conflict/.test(code));
export function reviewGroups(items: SpeakerAttribution[]): ReviewGroup[] {
  const groups = new Map<string, SpeakerAttribution[]>();
  for (const item of items) {
    // Never merge anonymous provider labels across chunks or versions.
    const key = item.group_id ?? `segment:${item.segment_id}`;
    const group = groups.get(key) ?? []; group.push(item); groups.set(key, group);
  }
  return [...groups].map(([id, values]) => {
    const eligible = values.filter((item) => !isDisputed(item));
    const representative = [...eligible].sort((a, b) => ((b.end_ms ?? 0) - (b.start_ms ?? 0)) - ((a.end_ms ?? 0) - (a.start_ms ?? 0))).slice(0, 3);
    return { id, items: eligible, representative, disputed: values.filter(isDisputed) };
  });
}
export const groupChanges = (group: ReviewGroup, participantId: string | null): AttributionChange[] => group.items.map((item) => ({ segment_id: item.segment_id, participant_id: participantId }));
export const attributionStatus = (status: string) => ({ proposed: 'Предложение · проверить', confirmed: 'Подтверждено', unknown: 'Не определён', conflict: 'Нужна проверка' })[status] ?? status;
export function reasonText(code: string): string {
  const known: Record<string, string> = {
    no_candidates: 'Нет пригодных голосовых образцов', runtime_unavailable: 'Локальный движок недоступен',
    uncalibrated: 'Калибровка качества ещё не выполнена', calibration_unavailable: 'Калибровка качества ещё не выполнена',
    overlap: 'Наложение речи', group_conflict: 'В группе разные голоса', short_speech: 'Слишком короткая речь',
    automatic_overlay_requires_review: 'Изменились профили или состав встречи; автоматические имена требуют проверки',
    explicit_runtime_bypass: 'Локальное узнавание явно пропущено; доступна ручная привязка',
  };
  return known[code] ?? code;
}
export const counter = (progress: Record<string, unknown> | undefined, key: string): number | null => {
  const value = progress?.[key]; return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
};
