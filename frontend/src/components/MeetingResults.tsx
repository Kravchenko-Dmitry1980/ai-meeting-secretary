import { ArrowUpRight, Check, CircleHelp, FileText, ListChecks } from 'lucide-react';
import type { EvidenceItem, Summary, TranscriptSegment } from '../types/api';
import { Button } from './ui/Button';
import { Card } from './ui/Card';
import { AssignmentReviewPanel } from './AssignmentReviewPanel';
import type { AssignmentReviewPanelProps } from './AssignmentReviewPanel';
import { TaskPublicationPanel } from './TaskPublicationPanel';

interface Props { summary: Summary | null; status: string; tab: string; onSource: (id: string, version?: number) => void; assignments?: Pick<AssignmentReviewPanelProps, 'meetingId' | 'roster' | 'refreshKey' | 'onChanged'> }
export function MeetingResults({ summary, status, tab, onSource, assignments }: Props) {
  const evidence = (item: EvidenceItem) => (
    <div className="mt-3 flex flex-wrap gap-2">
      {item.source_segment_ids.map((id, index) => <button key={id} type="button" className="source-link" onClick={() => onSource(id, summary?.transcript_version)} aria-label={`Перейти к источнику ${index + 1}`}><ArrowUpRight size={12} />Источник {index + 1}</button>)}
    </div>
  );
  if (!summary) {
    const waitingForTasks = tab === 'Задачи' && status !== 'failed';
    return <Card className="empty-results"><FileText size={26} className="text-slate-500" /><h3>{waitingForTasks ? 'Задачи ещё не сформированы' : 'Итоги ещё не сформированы'}</h3><p>{status === 'failed' ? 'Обработка итогов завершилась ошибкой. Расшифровка сохранена; повторите только этап итогов.' : waitingForTasks ? 'После полной расшифровки и формирования итогов здесь появятся подтверждённые задачи.' : 'После расшифровки здесь появятся содержание, решения и задачи с ссылками на исходные фрагменты.'}</p></Card>;
  }
  if (tab === 'Задачи') return (
    <div className="space-y-4">
      {assignments && summary.meeting_id === assignments.meetingId && <AssignmentReviewPanel key={`${assignments.meetingId}:${summary.transcript_version}:${summary.summary_version}:current`} {...assignments} transcriptVersion={summary.transcript_version} summaryVersion={summary.summary_version} dueDates={Object.fromEntries(summary.action_items.map((item) => [item.id, item.due_date]))} onSource={onSource} />}
      {assignments && summary.meeting_id === assignments.meetingId && <TaskPublicationPanel key={`publication:${summary.meeting_id}:${summary.transcript_version}:${summary.summary_version}`} meetingId={summary.meeting_id} transcriptVersion={summary.transcript_version} summaryVersion={summary.summary_version} refreshKey={assignments.refreshKey} onSource={onSource} />}
      <details open={!assignments} className="space-y-4"><summary className="cursor-pointer text-sm text-slate-300">Задачи и сроки из сохранённых итогов</summary>
      <p className="text-sm leading-6 text-slate-400">Ответственный и срок указаны, если они названы в записи. Каждая задача опирается на источник.</p>
      {summary.action_items.length === 0 ? <Card className="empty-results"><ListChecks size={26} className="text-mint" /><h3>{(summary.excluded_items_count ?? 0) > 0 ? 'Подтверждённых задач нет' : 'Задачи не обнаружены'}</h3><p>{(summary.excluded_items_count ?? 0) > 0 ? 'Часть пунктов из черновиков не прошла проверку. Сверьте поручения с исходной расшифровкой.' : 'Анализ завершён. В этой версии расшифровки не найдены подтверждённые поручения.'}</p></Card> : summary.action_items.map((item, index) => <Card key={item.id} className="task-card">
        <div className="flex items-start gap-3"><span className="task-number">{String(index + 1).padStart(2, '0')}</span><div className="min-w-0 flex-1"><p className="font-medium leading-6 text-slate-100">{item.text}</p><div className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-xs text-slate-400"><span>Ответственный: <strong className="font-medium text-slate-200">{item.owner ?? 'Не назван'}</strong></span><span>Срок: <strong className="font-medium text-slate-200">{item.due_date ?? 'Не назван'}</strong></span></div>{evidence(item)}</div></div>
      </Card>)}
      </details>
    </div>
  );
  return (
    <div className="space-y-5">
      <Card className="overview-card"><div className="section-heading"><FileText size={17} className="text-violet-300" /><h3>Краткое содержание</h3></div><p className="mt-4 whitespace-pre-wrap text-sm leading-7 text-slate-200">{summary.overview || 'В этой версии нет краткого содержания.'}</p><p className="mt-4 text-xs text-slate-500">Итог v{summary.summary_version} · Расшифровка v{summary.transcript_version}</p></Card>
      <div className="grid gap-5 xl:grid-cols-2">
        <Card><div className="section-heading"><Check size={18} className="text-mint" /><h3>Принятые решения</h3><span className="counter-badge">{summary.decisions.length}</span></div><div className="mt-4 space-y-4">{summary.decisions.length === 0 ? <p className="text-sm leading-6 text-slate-400">Подтверждённые решения не найдены.</p> : summary.decisions.map((item, index) => <div key={index} className="evidence-item"><p className="text-sm leading-6 text-slate-200">{item.text}</p>{evidence(item)}</div>)}</div></Card>
        <Card><div className="section-heading"><CircleHelp size={18} className="text-amber-200" /><h3>Открытые вопросы</h3><span className="counter-badge">{summary.open_questions.length}</span></div><div className="mt-4 space-y-4">{summary.open_questions.length === 0 ? <p className="text-sm leading-6 text-slate-400">Открытые вопросы не найдены.</p> : summary.open_questions.map((item, index) => <div key={index} className="evidence-item"><p className="text-sm leading-6 text-slate-200">{item.text}</p>{evidence(item)}</div>)}</div></Card>
      </div>
    </div>
  );
}

export function SourcePreview({ segment, onClose }: { segment: TranscriptSegment; onClose: () => void }) {
  return <Card className="border-violet-300/25 bg-violet-400/5"><div className="flex items-center justify-between gap-3"><p className="eyebrow">ИСХОДНЫЙ ФРАГМЕНТ · V{segment.transcript_version}</p><Button variant="ghost" className="px-3 py-1.5" onClick={onClose}>Закрыть</Button></div><p className="mt-3 whitespace-pre-wrap text-sm leading-7 text-slate-200">{segment.text}</p><p className="mt-3 text-xs text-slate-500">ID: {segment.id}</p></Card>;
}
