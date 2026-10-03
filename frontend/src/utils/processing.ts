import type { AudioChunk, IdentificationState, JobStage, JobStatus, Meeting, ProcessingJob } from '../types/api';

export type StageStatus = JobStatus | 'waiting' | 'partial';
export interface ProcessingStageState {
  id: JobStage;
  label: string;
  status: StageStatus;
  detail: string;
  error: string | null;
  jobs: ProcessingJob[];
  cancelIds: string[];
}

const activeStatuses = ['queued', 'running'];
const statusPriority: JobStatus[] = ['running', 'queued', 'uncertain', 'paused_budget', 'waiting_config', 'failed', 'cancelled'];
const newestFirst = (a: ProcessingJob, b: ProcessingJob) => b.created_at.localeCompare(a.created_at) || b.updated_at.localeCompare(a.updated_at);

// The API returns the whole job history. Choose a current attempt per chunk,
// not the last enqueued chunk (which often remains queued while the first runs).
function currentStageJobs(meeting: Meeting, jobs: ProcessingJob[], stage: JobStage): ProcessingJob[] {
  const candidates = jobs.filter((job) => job.stage === stage && (stage === 'prepare' || job.version === meeting.transcript_version)).sort(newestFirst);
  if (stage === 'identify_speakers') {
    // More than one revision intent can exist beside a running stale attempt.
    // Keep every owned active job cancellable, plus the latest terminal state.
    return candidates.filter((job, index) => index === 0 || activeStatuses.includes(job.status));
  }
  if (stage !== 'transcribe') return candidates.slice(0, 1);
  const latest = new Map<string, ProcessingJob>();
  for (const job of candidates) {
    const key = job.chunk_id ?? job.id;
    if (!latest.has(key)) latest.set(key, job);
  }
  return [...latest.values()];
}

function stageDetail(id: JobStage, status: StageStatus, completed: number, total: number): string {
  if (status === 'running') return id === 'prepare' ? 'Сохраняем и подготавливаем аудио на компьютере.'
    : id === 'transcribe' ? 'Обрабатываем фрагмент аудио через Polza. Готовый текст появится после ответа.' : 'Формируем содержание, решения и задачи по расшифровке.';
  if (status === 'queued') return 'Задание сохранено в очереди. Ожидаем начала обработки.';
  if (status === 'waiting_config') return id === 'transcribe'
    ? 'Проверьте ключ и разрешение облачной обработки в настройках, затем нажмите «Продолжить обработку». Модель этой версии закреплена; её замена требует новой платной версии расшифровки.'
    : 'Проверьте ключ, модель и разрешение облачной обработки в настройках, затем продолжите.';
  if (status === 'paused_budget') return id === 'transcribe'
    ? 'Проверьте лимит встречи и допустимый максимум тарифа закреплённой модели в настройках, затем нажмите «Продолжить обработку».'
    : 'Проверьте лимит встречи и максимальные тарифы в настройках перед продолжением.';
  if (status === 'uncertain') return 'Polza могла принять и оплатить запрос. Проверьте его статус в кабинете перед повтором.';
  if (status === 'failed') return 'Этап завершился с ошибкой. Сохранённые данные доступны; причина указана ниже.';
  if (status === 'cancelled') return 'Новые запросы этого этапа отменены. Сохранённые результаты доступны.';
  if (status === 'partial') return 'Часть записи ещё не расшифрована. Можно продолжить обработку.';
  if (status === 'succeeded') return id === 'prepare' ? 'Аудио сохранено и доступно для воспроизведения.'
    : id === 'transcribe' ? 'Все фрагменты этой версии расшифрованы.' : 'Результат сохранён. Проверьте итоги и задачи по источникам.';
  if (id === 'prepare') return 'Ожидаем запись или импорт аудио.';
  if (id === 'transcribe') return total > 0 ? 'Аудио готово. Запустите обработку, когда будете готовы.' : 'Ожидаем подготовку аудио.';
  return completed < total ? 'Итоги появятся после полной расшифровки. Сначала завершите этот этап.' : 'Ожидаем запуск итогов после расшифровки.';
}

export function buildProcessingState(meeting: Meeting, jobs: ProcessingJob[], chunks: AudioChunk[], identification: IdentificationState | null = null) {
  const transcriptJobs = currentStageJobs(meeting, jobs, 'transcribe');
  const chunkIds = new Set(chunks.map((chunk) => chunk.id));
  const completedChunks = new Set(transcriptJobs.filter((job) => job.status === 'succeeded' && job.chunk_id && chunkIds.has(job.chunk_id)).map((job) => job.chunk_id)).size;
  const totalChunks = chunkIds.size;
  const transcriptionComplete = totalChunks > 0 && completedChunks === totalChunks;
  const definitions: { id: JobStage; label: string }[] = [
    { id: 'prepare', label: 'Подготовка аудио' },
    { id: 'transcribe', label: 'Расшифровка' },
    ...(meeting.processing_mode === 'voice_identification' ? [{ id: 'identify_speakers' as const, label: 'Узнавание участников · локально' }] : []),
    { id: 'summarize', label: 'Итоги и задачи' },
  ];
  const identifyJobs = currentStageJobs(meeting, jobs, 'identify_speakers');
  const bypassAcknowledged = identification && 'bypass_acknowledged' in identification && identification.bypass_acknowledged === true;
  const localOutcome = bypassAcknowledged ? 'bypassed' : identification?.outcome ?? identifyJobs[0]?.local_outcome;
  const identificationComplete = meeting.processing_mode !== 'voice_identification' || ['completed', 'bypassed'].includes(localOutcome ?? '');
  const stages: ProcessingStageState[] = definitions.map(({ id, label }) => {
    const stageJobs = id === 'transcribe' ? transcriptJobs : currentStageJobs(meeting, jobs, id);
    let status: StageStatus = statusPriority.find((state) => stageJobs.some((job) => job.status === state)) ?? 'waiting';
    if (id === 'transcribe' && transcriptionComplete) status = 'succeeded';
    else if (status === 'waiting' && id === 'transcribe' && completedChunks > 0) status = 'partial';
    else if (status === 'waiting' && (stageJobs.some((job) => job.status === 'succeeded') || (id === 'prepare' && totalChunks > 0))) status = 'succeeded';
    const stageError = ['failed', 'uncertain', 'waiting_config', 'paused_budget'].includes(status)
      ? stageJobs.find((job) => job.status === status && job.error)?.error ?? null : null;
    let detail = stageDetail(id, status, completedChunks, totalChunks);
    if (id === 'transcribe' && status !== 'uncertain' && stageError?.includes('заданный предел тарифа ниже')) {
      detail = 'Максимальный тариф закреплённой модели ниже доступного тарифа Polza. Измените допустимый максимум для этой модели в настройках, затем нажмите «Продолжить обработку». Для смены модели распознавания нужна новая версия расшифровки; распознавание всех фрагментов может оплачиваться повторно.';
    }
    if (id === 'identify_speakers') {
      if (localOutcome === 'bypassed') { status = 'succeeded'; detail = 'Узнавание явно пропущено. Автоматические имена не подтверждены; назначьте участников вручную.'; }
      else if (localOutcome === 'completed' && stageJobs[0]?.status === 'succeeded') detail = 'Локальный результат сохранён. Не определённые участники остаются для ручной проверки.';
      else if (localOutcome === 'runtime_unavailable') detail = 'Локальный движок недоступен. Откройте участников для настройки образцов, повторите локальный этап или явно продолжите без узнавания.';
      else if (localOutcome === 'stale') detail = 'Состав, профили или ручные решения изменились. Прежний расчёт не применён; повторите локальный этап.';
      else if (status === 'running') detail = 'Сравниваем голоса локально. Обработка реплик ещё не означает сохранение результата; ожидаем публикацию.';
      else if (status === 'cancelled') detail = 'Локальное узнавание отменено. Сохранённая расшифровка доступна для ручной проверки.';
      else if (status === 'waiting') detail = 'Локальное узнавание начнётся после сохранения всей расшифровки и завершения записи.';
      else if (status === 'failed') detail = 'Локальный этап остановлен. Повтор не запускает STT или платные итоги.';
    } else if (id === 'summarize' && status === 'waiting' && !identificationComplete) detail = 'Итоги ожидают локального узнавания или явного продолжения без него.';
    return {
      id, label, status, jobs: stageJobs,
      detail,
      error: ['failed', 'uncertain', 'waiting_config', 'paused_budget'].includes(status) ? stageError : null,
      cancelIds: stageJobs.filter((job) => ['queued', 'running', 'waiting_config', 'paused_budget'].includes(job.status))
        .sort((a, b) => Number(a.status === 'running') - Number(b.status === 'running')).map((job) => job.id),
    };
  });
  const primaryStage = statusPriority.map((status) => stages.find((stage) => stage.status === status)).find((stage) => stage != null)
    ?? stages.find((stage) => stage.status === 'partial')
    ?? stages.find((stage) => stage.status === 'waiting')
    ?? stages[stages.length - 1];
  const currentJobs = stages.flatMap((stage) => stage.jobs);
  const activeChunk = transcriptJobs.find((job) => job.status === 'running')?.chunk_id;
  const activeChunkIndex = chunks.findIndex((chunk) => chunk.id === activeChunk);
  return {
    stages, primaryStage, currentJobs, completedChunks, totalChunks, transcriptionComplete, identificationComplete, identification, localOutcome,
    active: currentJobs.some((job) => activeStatuses.includes(job.status)),
    activeChunkNumber: activeChunkIndex < 0 ? null : activeChunkIndex + 1,
    failedChunks: transcriptJobs.filter((job) => ['failed', 'uncertain'].includes(job.status)).length,
    queuedChunks: transcriptJobs.filter((job) => job.status === 'queued').length,
  };
}

export type ProcessingState = ReturnType<typeof buildProcessingState>;

export function processingErrorPresentation(error: string | null) {
  if (!error) return null;
  if (error.includes('заданный предел тарифа ниже')) {
    return { message: 'Максимальный тариф ниже доступного тарифа Polza. Проверьте допустимый максимум в настройках, затем нажмите «Продолжить обработку».', details: error };
  }
  const diagnosticStart = error.search(/ \[(?:код Polza|параметр|trace_id):/);
  return diagnosticStart < 0 ? { message: error, details: null }
    : { message: error.slice(0, diagnosticStart), details: error.slice(diagnosticStart + 1) };
}

export function requiresTranscriptionRetry(meeting: Meeting | null, jobs: ProcessingJob[], chunks: AudioChunk[]): boolean {
  if (!meeting || chunks.length === 0) return false;
  const state = buildProcessingState(meeting, jobs, chunks);
  // A failed current attempt can coexist with paused jobs. Meeting status alone
  // cannot decide whether retry is needed. Never request a new complete version.
  return !state.active && !state.transcriptionComplete
    && state.stages[1].jobs.some((job) => ['failed', 'cancelled', 'uncertain'].includes(job.status));
}
