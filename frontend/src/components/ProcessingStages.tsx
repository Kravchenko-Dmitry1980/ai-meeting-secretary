import { AlertCircle, CheckCircle2, Circle, Clock3, LoaderCircle, PauseCircle } from 'lucide-react';
import type { ProcessingState, StageStatus } from '../utils/processing';
import { processingErrorPresentation } from '../utils/processing';
import { Button } from './ui/Button';
import { counter } from '../utils/speakers';

const statusText: Record<StageStatus, string> = {
  queued: 'В очереди', running: 'Выполняется', waiting_config: 'Нужна настройка',
  paused_budget: 'Проверка лимитов', succeeded: 'Готово', failed: 'Ошибка',
  cancelled: 'Отменено', uncertain: 'Нужно проверить запрос', waiting: 'Ожидание', partial: 'Готово частично',
};
const headline: Record<StageStatus, string> = {
  queued: 'Обработка в очереди', running: 'Обработка идёт', waiting_config: 'Обработка ждёт настройки',
  paused_budget: 'Обработка приостановлена по лимитам', succeeded: 'Обработка завершена',
  failed: 'Обработка остановилась с ошибкой', cancelled: 'Обработка отменена',
  uncertain: 'Требуется проверка облачного запроса', waiting: 'Следующий этап ещё не запущен', partial: 'Запись расшифрована частично',
};

function StatusIcon({ status, large = false }: { status: StageStatus; large?: boolean }) {
  const size = large ? 25 : 18;
  if (status === 'running') return <LoaderCircle size={size} className="processing-spinner" aria-hidden="true" />;
  if (status === 'succeeded') return <CheckCircle2 size={size} aria-hidden="true" />;
  if (status === 'failed' || status === 'uncertain') return <AlertCircle size={size} aria-hidden="true" />;
  if (status === 'queued') return <Clock3 size={size} aria-hidden="true" />;
  if (status === 'waiting_config' || status === 'paused_budget' || status === 'cancelled') return <PauseCircle size={size} aria-hidden="true" />;
  return <Circle size={size} aria-hidden="true" />;
}

interface Props { state: ProcessingState; busy: boolean; connected: boolean; onCancel: (ids: string[]) => void; onSettings: () => void; onParticipants?: () => void; onIdentify?: () => void; onBypass?: () => void }
export function ProcessingStages({ state, busy, connected, onCancel, onSettings, onParticipants, onIdentify, onBypass }: Props) {
  const { primaryStage, stages, totalChunks, completedChunks } = state;
  const hasStarted = state.currentJobs.length > 0;
  const error = primaryStage.id === 'identify_speakers' && primaryStage.error ? { message: primaryStage.detail, details: primaryStage.error } : processingErrorPresentation(primaryStage.error);
  if (!hasStarted && totalChunks === 0) return null;
  const title = !hasStarted ? 'Запись готова к обработке' : headline[primaryStage.status];
  const local = stages.find((stage) => stage.id === 'identify_speakers');
  const progress = state.identification?.progress;
  const processed = counter(progress, 'observations_processed');
  const total = counter(progress, 'observations_total');
  const elapsed = counter(progress, 'elapsed_seconds');
  return <section aria-label="Состояние обработки" className="processing-panel">
    <div className={`processing-overview ${primaryStage.status}`} role="status" aria-live="polite" aria-atomic="true">
      <div className="processing-overview-heading">
        <span className="processing-overview-icon"><StatusIcon status={primaryStage.status} large /></span>
        <div className="min-w-0 flex-1">
          <p className="processing-eyebrow">{!connected ? 'Последний полученный статус' : primaryStage.label}</p>
          <h2>{title}</h2>
          <p className="processing-explanation">{error?.message ?? primaryStage.detail}</p>
        </div>
        {state.activeChunkNumber != null && <span className="processing-current-chunk">Фрагмент {state.activeChunkNumber} из {totalChunks}</span>}
      </div>
      {totalChunks > 0 && <div className="processing-progress">
        <div className="processing-progress-caption"><span>Расшифровано фрагментов</span><strong>{completedChunks} из {totalChunks}</strong></div>
        <progress max={totalChunks} value={completedChunks} aria-label={`Расшифровано ${completedChunks} из ${totalChunks} аудиофрагментов`} />
        <div className="processing-progress-note">
          <span>{state.queuedChunks > 0 ? `В очереди: ${state.queuedChunks}` : state.transcriptionComplete ? 'Расшифровка сохранена' : 'Готовые фрагменты сохраняются по мере обработки'}</span>
          {state.failedChunks > 0 && <span className="processing-failure-count">Требуют внимания: {state.failedChunks}</span>}
        </div>
      </div>}
      {primaryStage.id !== 'identify_speakers' && ['waiting_config', 'paused_budget', 'failed'].includes(primaryStage.status) && <Button variant="ghost" className="mt-4 px-3 py-2 text-xs" disabled={busy} onClick={onSettings}>Открыть настройки обработки</Button>}
      {error?.details && <details className="processing-diagnostics"><summary>Технические подробности</summary><p>{error.details}</p></details>}
    </div>
    {local && <div className="local-identification-progress">
      <p className="text-sm font-medium">Локальное узнавание · {state.localOutcome === 'runtime_unavailable' ? 'движок недоступен' : state.localOutcome === 'bypassed' ? 'явно пропущено' : local.status === 'succeeded' ? 'результат сохранён' : 'состояние по ответу сервера'}</p>
      {processed != null && total != null ? <><p className="mt-2 text-xs text-slate-400">Обработано реплик: {processed} из {total} · извлечено признаков: {counter(progress, 'observations_embedded') ?? '—'} · пропущено: {counter(progress, 'observations_skipped') ?? '—'}{elapsed != null ? ` · серверное время: ${elapsed.toFixed(1)} с` : ''}</p>{total > 0 && <progress className="mt-2 w-full" max={total} value={Math.min(processed, total)} aria-label={`Локально обработано ${processed} из ${total} реплик; результат ${local.status === 'succeeded' ? 'сохранён' : 'ещё не сохранён'}`} />}</> : <p className="mt-2 text-xs text-slate-400">Счётчики ещё не получены. Проценты и время не оцениваются.</p>}
      <div className="mt-3 flex flex-wrap gap-2"><Button variant="ghost" onClick={onParticipants} disabled={busy}>Профили и образцы</Button><Button variant="ghost" onClick={onIdentify} disabled={busy || state.active || !state.transcriptionComplete}>Повторить только локальное узнавание</Button>{state.localOutcome === 'runtime_unavailable' && <Button variant="ghost" onClick={onBypass} disabled={busy || state.active}>Продолжить без узнавания · вручную</Button>}</div>
      <p className="mt-2 text-xs leading-6 text-slate-400">Повтор этого этапа не запускает STT или новые платные итоги. Если первых итогов ещё нет, после локального результата нажмите «Продолжить обработку».</p>
      {state.localOutcome === 'runtime_unavailable' && <p className="mt-2 text-xs leading-6 text-slate-400">Для автоматического сравнения настройте локальный голосовой движок на этом компьютере, затем повторите локальный этап. Пока можно продолжить явно: неизвестные личности и ручные решения сохранятся. Затем нажмите «Продолжить обработку», чтобы получить первые итоги. Назначать имена вручную можно во вкладке «Участники встречи».</p>}
    </div>}
    <div className="stage-grid">
      {stages.map((stage) => <div key={stage.id} className={`stage-item ${stage.status === 'succeeded' ? 'done' : stage.status}`}>
        <StatusIcon status={stage.status} />
        <div className="min-w-0 flex-1"><p className="text-xs font-medium">{stage.label}</p><p className="stage-status">{statusText[stage.status]}</p><p className="stage-detail">{stage.detail}</p></div>
        {stage.cancelIds.length > 0 && <Button variant="ghost" className="px-2 py-1 text-[10px]" disabled={busy} onClick={() => onCancel(stage.cancelIds)} aria-label={`Отменить этап: ${stage.label}`}>Отмена</Button>}
      </div>)}
    </div>
    {state.active && <p className="processing-cancel-note">{state.currentJobs.some((job) => job.stage !== 'identify_speakers' && ['queued', 'running'].includes(job.status)) ? 'Отмена остановит очередь этапа. Уже отправленный облачный запрос может завершиться и быть оплачен.' : 'Отмена остановит локальное узнавание. Платный STT не повторяется; расшифровка остаётся доступной.'}</p>}
  </section>;
}
