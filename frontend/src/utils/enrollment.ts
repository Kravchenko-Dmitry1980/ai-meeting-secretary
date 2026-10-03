import { ApiError, errorMessage } from '../services/api.ts';
import type { Enrollment, EnrollmentRecording } from '../services/participants.ts';

export const SAMPLE_MAX_BYTES = 10 * 1024 * 1024;
export const SAMPLE_MAX_SECONDS = 30;
export function validateSample(file: Pick<File, 'size'>, duration?: number): string | null {
  if (!file.size) return 'Выберите непустой аудио- или видеофайл.';
  if (file.size > SAMPLE_MAX_BYTES) return 'Файл превышает 10 МиБ. Подготовьте более короткий образец.';
  if (duration !== undefined && (!Number.isFinite(duration) || duration <= 0 || duration > SAMPLE_MAX_SECONDS)) return 'Образец должен длиться не более 30 секунд.';
  return null;
}
export function aliasesFromText(text: string): string[] {
  return [...new Set(text.split('\n').map((value) => value.trim()).filter(Boolean))];
}
export function captureNeedsClosure(recording: EnrollmentRecording | null): boolean {
  return !!recording && ['starting', 'recording', 'stopping', 'cleanup_pending'].includes(recording.status);
}
export function recordingNeedsPoll(recording: EnrollmentRecording | null): boolean {
  return captureNeedsClosure(recording) || recording?.status === 'processing';
}
export function enrollmentNeedsPoll(enrollments: Enrollment[]): boolean {
  return enrollments.some((item) => item.status === 'pending' || item.status === 'cleanup_pending');
}
export function canConfirm(enrollment: Enrollment, listening: boolean, singleSpeaker: boolean, enabled: boolean): boolean {
  return enabled && enrollment.consent_confirmed && enrollment.status === 'awaiting_review' && listening && singleSpeaker;
}
export class RequestGate {
  private epoch = 0;
  private locked = false;
  begin(): number | null { if (this.locked) return null; this.locked = true; return ++this.epoch; }
  current(epoch: number): boolean { return epoch === this.epoch; }
  finish(epoch: number): void { if (this.current(epoch)) this.locked = false; }
  invalidate(): void { ++this.epoch; this.locked = false; }
}
export function uncertainOperation(error: unknown): boolean {
  return !(error instanceof ApiError) || error.status >= 500 || (error.status === 409 && error.message === 'operation_in_progress');
}
const explanations: Record<string, string> = {
  consent_required: 'Сначала подтвердите согласие участника.', human_review_required: 'Прослушайте образец и подтвердите одного говорящего.',
  profile_disabled: 'Профиль выключен. Включите его перед добавлением образца.', profile_not_found: 'Профиль отсутствует. Обновите список.',
  stale_material: 'Образец изменился. Обновите состояние и проверьте его заново.', stale_recording: 'Запись изменилась. Обновите её UUID и generation.',
  capture_busy: 'Микрофон занят другой записью. Остановите её и повторите.', inference_busy: 'Локальная обработка занята. Дождитесь её завершения.',
  operation_in_progress: 'Запрос уже выполняется. Обновите состояние; повтор использует тот же идентификатор.',
  operation_payload_mismatch: 'Идентификатор принадлежит другой операции. Обновите состояние перед новым действием.',
  operation_interrupted: 'Операция прервана при перезапуске. Нужен новый образец.', recording_interrupted: 'Запись прервана. Создайте новый образец.',
  capture_start_failed: 'Не удалось открыть микрофон. Обновите состояние и проверьте устройство.',
  capture_cleanup_pending: 'Микрофон ещё не закрыт. Повторите остановку и дождитесь подтверждения.',
  private_cleanup_pending: 'Удаление локального образца ещё не завершено. Обновите состояние.',
  worker_cleanup_pending: 'Обработка ещё освобождает образец. Дождитесь очистки.',
  runtime_missing: 'Локальный движок не установлен. Настройте голосовой движок на сервере.',
  runtime_unverified: 'Локальный движок не проверен. Завершите его настройку на сервере.',
  runtime_metadata_unavailable: 'Метаданные локального движка недоступны. Проверьте его настройку.',
  dpapi_unavailable: 'Локальная защита голоса недоступна. Проверьте настройку Secretary в Windows.',
  material_unavailable: 'Образец недоступен. Обновите состояние или создайте новый.',
  capture_material_invalid: 'Запись не прошла проверку. Запишите одного человека заново.',
  capture_interrupted: 'Запись прервана. Обновите состояние и запишите образец заново.',
  enrollment_not_found: 'Образец отсутствует. Обновите список образцов.', recording_not_found: 'Запись отсутствует. Обновите её состояние.',
  listening_required: 'Прослушайте весь образец перед подтверждением.', single_speaker_review_required: 'Проверьте, что говорит только один участник.',
  energy_screening_only: 'Техническая проверка звука не заменяет прослушивание человеком.',
  overlong_audio: 'Образец длиннее 30 секунд. Подготовьте более короткий файл.',
  audio_payload_limit: 'Файл превышает допустимый размер. Подготовьте образец до 10 МиБ.',
  silent_audio: 'В образце не обнаружен пригодный звук. Запишите голос заново.',
  clipped_audio: 'Звук перегружен. Уменьшите громкость микрофона и запишите заново.',
  insufficient_usable_speech: 'Недостаточно пригодной речи. Запишите более отчётливый образец одного человека.',
  truncated_audio: 'Файл повреждён или обрезан. Подготовьте другой образец.',
  invalid_audio: 'Формат звука не прошёл проверку. Подготовьте другой файл.',
  decode_error: 'Не удалось прочитать звук. Подготовьте другой файл.',
  runtime_incompatible: 'Версия локального движка несовместима. Проверьте его настройку.',
  process_timeout: 'Локальная обработка превысила время ожидания. Проверьте состояние перед повтором.',
  process_memory_limit: 'Локальной обработке не хватило памяти. Закройте лишние приложения и повторите.',
  process_cleanup_failed: 'Локальный процесс ещё не завершён. Дождитесь освобождения ресурсов.',
  cancelled: 'Операция отменена. Для нового образца начните новое действие.',
  loopback_peer_required: 'Прослушивание доступно с локального компьютера Secretary.',
  material_mapping_mismatch: 'Материал образца изменился. Обновите состояние перед проверкой.',
  dpapi_failed: 'Не удалось открыть локально защищённый образец. Проверьте Windows-сеанс Secretary.',
};
export function enrollmentError(error: unknown): string {
  const message = errorMessage(error);
  if (explanations[message]) return explanations[message];
  if (error instanceof ApiError && error.status === 409) return 'Состояние изменилось. Черновик сохранён; обновите состояние перед повтором.';
  if (/^[a-z][a-z0-9_]+$/.test(message)) return 'Локальный образец не прошёл обработку. Обновите состояние; при повторной ошибке проверьте настройку движка.';
  return message;
}
export function statusLabel(status: string): string {
  return ({ pending: 'Подготовка образца', awaiting_review: 'Нужно прослушать и проверить', ready: 'Образец готов', revoked: 'Образец удалён',
    failed: 'Ошибка', interrupted: 'Прервано', cancelled: 'Отменено', cleanup_pending: 'Ожидание закрытия и очистки',
    starting: 'Открытие микрофона', recording: 'Идёт запись', stopping: 'Закрытие микрофона', processing: 'Подготовка записанного образца' } as Record<string, string>)[status] ?? status;
}
export function recordingStatusText(recording: EnrollmentRecording, enrollments: readonly Enrollment[]): string {
  const seconds = Math.floor(recording.duration_ms / 1000);
  if (recordingNeedsPoll(recording)) return `${statusLabel(recording.status)} · ${seconds} / 30 с`;
  if (recording.status !== 'awaiting_review') return `${statusLabel(recording.status)} · ${seconds} с`;
  const material = enrollments.find((item) => item.id === recording.enrollment_id && item.person_profile_id === recording.person_profile_id);
  return `Запись завершена · ${seconds} с${material ? ` · ${statusLabel(material.status)}` : ''}`;
}
export function bindDialogKeyboard(element: HTMLElement | null, close: () => void): () => void {
  const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  element?.querySelector<HTMLElement>('button:not(:disabled),input:not(:disabled)')?.focus();
  const handle = (event: KeyboardEvent) => {
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (event.key !== 'Tab') return;
    const controls = Array.from(element?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),audio[controls]') ?? []);
    const first = controls[0]; const last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  };
  document.addEventListener('keydown', handle);
  return () => { document.removeEventListener('keydown', handle); previous?.focus(); };
}
