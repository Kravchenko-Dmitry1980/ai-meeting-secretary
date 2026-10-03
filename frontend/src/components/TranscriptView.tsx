import { ChevronLeft, ChevronRight, FileText, Search } from 'lucide-react';
import { useState } from 'react';
import type { SegmentPage, Speaker, Summary, TranscriptSegment } from '../types/api';
import { Badge } from './ui/Badge';
import { Button } from './ui/Button';
import { Card } from './ui/Card';
import { timeOffset } from '../utils/format';
import type { SpeakerWorkflow } from '../hooks/useSpeakerWorkflow';
import { SingleSpeakerReview } from './SpeakerReview';
import { attributionStatus } from '../utils/speakers';
const channelName = (value: string) => ({ import: 'Импорт', mixed: 'Общий канал', microphone: 'Микрофон', system: 'Системный звук' })[value] ?? value;
const precisionName = (value: string) => ({ word: 'По словам', segment: 'По сегменту', chunk: 'Граница фрагмента', unknown: 'Таймкоды отсутствуют' })[value] ?? value;
interface Props { page: SegmentPage; speakers: Speaker[]; summary: Summary | null; offset: number; onPage: (offset: number) => void; highlightedId: string | null; onSeek: (segment: TranscriptSegment) => void; speakerWorkflow?: SpeakerWorkflow }
export function TranscriptView({ page, speakers, summary, offset, onPage, highlightedId, onSeek, speakerWorkflow }: Props) {
  const [mode, setMode] = useState<'raw' | 'readable'>('raw');
  const [query, setQuery] = useState('');
  const visible = page.items.filter((item) => item.text.toLocaleLowerCase('ru-RU').includes(query.toLocaleLowerCase('ru-RU')));
  return <div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="inline-flex rounded-lg border border-white/10 bg-black/10 p-1">
        <button type="button" className={`reader-mode ${mode === 'raw' ? 'active' : ''}`} onClick={() => setMode('raw')}>Исходная</button>
        <button type="button" className={`reader-mode ${mode === 'readable' ? 'active' : ''}`} onClick={() => setMode('readable')}>Читаемая версия</button>
      </div>
      <Badge>Расшифровка v{page.transcript_version}</Badge>
    </div>
    {mode === 'readable' ? <Card>{summary?.readable_transcript ? <><p className="mb-4 text-xs text-slate-400">Отредактировано моделью на основе исходной расшифровки v{summary.transcript_version}.</p><p className="whitespace-pre-wrap text-sm leading-7 text-slate-200">{summary.readable_transcript}</p></> : <p className="text-sm leading-6 text-slate-400">Читаемая версия появится после формирования итогов. Исходные сегменты сохранены отдельно.</p>}</Card> : <>
      <label className="search-field"><Search size={16} className="text-slate-500" /><input type="search" aria-label="Поиск по текущей странице расшифровки" placeholder="Поиск по текущей странице" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      {page.total === 0 ? <Card className="empty-results"><FileText size={26} className="text-slate-500" /><h3>Здесь будет расшифровка</h3><p>Текст появляется по мере обработки сохранённых фрагментов записи. Без API-ключа аудио остаётся на диске.</p></Card> : <Card className="p-0"><div className="divide-y divide-white/[0.06]">
        {visible.length === 0 && <p className="p-6 text-sm text-slate-400">На этой странице совпадений нет.</p>}
        {visible.map((segment) => {
          const snapshot = speakerWorkflow?.snapshot?.transcript_version === page.transcript_version ? speakerWorkflow.snapshot : null;
          const attribution = snapshot?.items.find((item) => item.segment_id === segment.id);
          const participant = snapshot?.participants.find((item) => item.id === attribution?.participant_id);
          return <article key={segment.id} id={`segment-${segment.id}`} className={`transcript-line ${highlightedId === segment.id ? 'highlighted' : ''}`}>
          <button type="button" className="time-button" disabled={segment.start_ms == null} onClick={() => onSeek(segment)} aria-label={`Воспроизвести с ${timeOffset(segment.start_ms)}`}>{timeOffset(segment.start_ms)}{segment.timing_precision === 'chunk' && <span>≈</span>}</button>
          <div className="min-w-0 flex-1"><div className="mb-2 flex flex-wrap items-center gap-2 text-[11px] text-slate-500"><span className="channel-label">{channelName(segment.channel)}</span><span>{precisionName(segment.timing_precision)}</span><span>{segment.speaker_id ? `${speakers.find((speaker) => speaker.id === segment.speaker_id)?.provider_label ?? 'Спикер'} · метка в пределах фрагмента` : 'Диаризация отсутствует'}</span></div><p className="whitespace-pre-wrap text-sm leading-7 text-slate-200">{segment.text}</p>{attribution && speakerWorkflow && <details className="mt-3"><summary className="text-xs text-slate-300">{participant?.display_name ?? 'Не определён'} · {attributionStatus(attribution.status)}{snapshot?.automatic_overlay_stale && attribution.method !== 'manual' ? ' · автоматическое имя устарело' : ''} · исправить реплику</summary><SingleSpeakerReview key={attribution.segment_id} item={attribution} workflow={speakerWorkflow} /></details>}</div>
        </article>})}
      </div></Card>}
      {page.total > 0 && <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-slate-500"><span>Фрагменты {offset + 1}–{Math.min(offset + page.items.length, page.total)} из {page.total}</span><div className="flex gap-2"><Button variant="ghost" className="px-3 py-2" disabled={offset === 0} onClick={() => onPage(Math.max(0, offset - 50))}><ChevronLeft size={15} />Назад</Button><Button variant="ghost" className="px-3 py-2" disabled={offset + 50 >= page.total} onClick={() => onPage(offset + 50)}>Далее<ChevronRight size={15} /></Button></div></div>}
    </>}
  </div>;
}
