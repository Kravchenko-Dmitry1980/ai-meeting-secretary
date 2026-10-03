import { useEffect, useState } from 'react';
import type { SpeakerWorkflow } from '../hooks/useSpeakerWorkflow';
import type { SpeakerAttribution } from '../types/api';
import { attributionStatus, groupChanges, reasonText, reviewGroups } from '../utils/speakers';
import type { ReviewGroup } from '../utils/speakers';
import { timeOffset } from '../utils/format';
import { Button } from './ui/Button';
import { Card } from './ui/Card';
import { api } from '../services/api';

function SourceExample({ item, meetingId, version, onSource }: { item: SpeakerAttribution; meetingId: string; version: number; onSource: (id: string) => void }) {
  const [text, setText] = useState('Загрузка реплики…');
  useEffect(() => {
    let active = true;
    void api.segment(meetingId, item.segment_id).then((page) => {
      const segment = page.items.find((value) => value.id === item.segment_id && value.transcript_version === version);
      if (active) setText(segment?.text ?? 'Источник этой версии недоступен.');
    }).catch(() => { if (active) setText('Не удалось загрузить источник; повторите открытие реплики.'); });
    return () => { active = false; };
  }, [meetingId, version, item.segment_id]);
  return <div className="speaker-example"><p className="whitespace-pre-wrap text-sm leading-6">{text}</p><Button variant="ghost" className="mt-2 px-2 py-1 text-xs" onClick={() => onSource(item.segment_id)}>Источник и аудио · {timeOffset(item.start_ms)}</Button></div>;
}
function GroupReview({ group, workflow, onSource }: { group: ReviewGroup; workflow: SpeakerWorkflow; onSource: (id: string) => void }) {
  const candidates = new Set(group.items.map((item) => item.participant_id));
  const proposed = candidates.size === 1 ? group.items[0]?.participant_id ?? '' : '';
  const [participantId, setParticipantId] = useState(proposed);
  const snapshot = workflow.snapshot!;
  const person = snapshot.participants.find((item) => item.id === proposed);
  return <Card className="space-y-3">
    <h3 className="font-medium">{person?.display_name ?? 'Не определён'} <span className="text-xs text-slate-400">· группа из {group.items.length} реплик</span></h3>
    <p className="text-xs text-slate-400">Примеры речи для проверки. Участник — предложение; оценка сходства не является вероятностью.</p>
    {group.representative.map((item) => <SourceExample key={item.segment_id} item={item} meetingId={snapshot.meeting_id} version={snapshot.transcript_version} onSource={onSource} />)}
    <label className="field-label">Участник группы<select value={participantId} onChange={(event) => setParticipantId(event.target.value)}><option value="">Не определён</option>{snapshot.participants.filter((item) => item.enabled).map((item) => <option value={item.id} key={item.id}>{item.display_name}</option>)}</select></label>
    <Button disabled={!group.items.length || workflow.busy || workflow.pending || (Boolean(participantId) && !snapshot.participants.some((item) => item.enabled && item.id === participantId))} onClick={() => { void workflow.review(groupChanges(group, participantId || null)); }}>Подтвердить {group.items.length} реплик группы · v{snapshot.transcript_version}</Button>
    {group.disputed.length > 0 && <p className="text-xs text-amber-200">Спорных реплик вне подтверждения группы: {group.disputed.length}. Проверьте их отдельно ниже.</p>}
  </Card>;
}

export function SingleSpeakerReview({ item, workflow, onSource }: { item: SpeakerAttribution; workflow: SpeakerWorkflow; onSource?: (id: string) => void }) {
  const [participantId, setParticipantId] = useState(item.participant_id ?? '');
  const snapshot = workflow.snapshot!;
  const enabled = snapshot.participants.filter((participant) => participant.enabled);
  return <div className="speaker-single space-y-2">
    <p className="text-xs text-slate-400">{attributionStatus(item.status)} · {item.reason_codes.map(reasonText).join(' · ')}</p>
    {onSource && <SourceExample item={item} meetingId={snapshot.meeting_id} version={snapshot.transcript_version} onSource={onSource} />}
    {(item.review_candidates?.length ?? 0) > 0 && <details className="text-xs text-slate-400"><summary>Кандидаты для проверки · без вероятности</summary>{item.review_candidates?.map((candidate) => <p key={candidate.participant_id}>{snapshot.participants.find((participant) => participant.id === candidate.participant_id)?.display_name ?? 'Участник отсутствует'} · сходство {candidate.raw_score.toFixed(3)}</p>)}</details>}
    <div className="flex flex-wrap items-end gap-2"><label className="field-label flex-1">Участник реплики<select value={participantId} onChange={(event) => setParticipantId(event.target.value)}><option value="">Не определён</option>{enabled.map((participant) => <option key={participant.id} value={participant.id}>{participant.display_name}</option>)}</select></label><Button variant="ghost" disabled={workflow.busy || workflow.pending || (Boolean(participantId) && !enabled.some((participant) => participant.id === participantId))} onClick={() => { void workflow.review([{ segment_id: item.segment_id, participant_id: participantId || null }]); }}>Подтвердить реплику</Button></div>
  </div>;
}
export function SpeakerReview({ workflow, onSource }: { workflow: SpeakerWorkflow; onSource: (id: string) => void }) {
  const [groupPage, setGroupPage] = useState(0);
  const [reviewPage, setReviewPage] = useState(0);
  const snapshot = workflow.snapshot;
  if (!snapshot) return <Card>Загрузка проверки участников…</Card>;
  const groups = reviewGroups(snapshot.items);
  const disputed = snapshot.items.filter((item) => item.status === 'unknown' || item.status === 'conflict' || item.reason_codes.some((code) => /overlap|conflict/.test(code)));
  const visibleGroups = groups.filter((group) => group.items.length > 1 || group.items.some((item) => item.participant_id));
  const groupOffset = Math.min(groupPage * 6, Math.max(0, Math.floor((visibleGroups.length - 1) / 6) * 6));
  const reviewOffset = Math.min(reviewPage * 20, Math.max(0, Math.floor((disputed.length - 1) / 20) * 20));
  return <div className="space-y-4">
    <Card><h2 className="text-lg font-medium">Проверка участников · v{snapshot.transcript_version}</h2><p className="mt-2 text-sm leading-6 text-slate-400">Подтвердите группу по примерам или исправьте отдельную реплику в расшифровке. Исправление личности не изменяет текст, таймкоды, канал и исходное аудио и не запускает платную обработку.</p><p className="mt-2 text-xs text-slate-500">Attribution revision {snapshot.revision} · roster revision {snapshot.roster_revision}</p>{snapshot.automatic_overlay_stale && <p role="status" className="mt-3 text-sm text-amber-200">Автоматические имена устарели и требуют проверки. {snapshot.reason_codes?.map(reasonText).join(' · ')}</p>}</Card>
    <div className="speaker-review-grid">{visibleGroups.slice(groupOffset, groupOffset + 6).map((group) => <GroupReview key={group.id} group={group} workflow={workflow} onSource={onSource} />)}</div>
    {visibleGroups.length > 6 && <div className="flex flex-wrap items-center gap-3 text-xs text-slate-400"><Button variant="ghost" disabled={groupOffset === 0} onClick={() => setGroupPage(Math.max(0, Math.floor(groupOffset / 6) - 1))}>Предыдущие группы</Button><span>Группы {groupOffset + 1}–{Math.min(groupOffset + 6, visibleGroups.length)} из {visibleGroups.length}</span><Button variant="ghost" disabled={groupOffset + 6 >= visibleGroups.length} onClick={() => setGroupPage(Math.floor(groupOffset / 6) + 1)}>Следующие группы</Button></div>}
    <Card className="space-y-4"><h3 className="font-medium">Неизвестные и спорные реплики · {disputed.length}</h3>{disputed.length === 0 ? <p className="text-sm text-slate-400">Неизвестных и спорных реплик в текущем снимке нет.</p> : disputed.slice(reviewOffset, reviewOffset + 20).map((item) => <SingleSpeakerReview key={item.segment_id} item={item} workflow={workflow} onSource={onSource} />)}{disputed.length > 20 && <div className="flex flex-wrap items-center gap-3 text-xs text-slate-400"><Button variant="ghost" disabled={reviewOffset === 0} onClick={() => setReviewPage(Math.max(0, Math.floor(reviewOffset / 20) - 1))}>Предыдущие спорные реплики</Button><span>{reviewOffset + 1}–{Math.min(reviewOffset + 20, disputed.length)} из {disputed.length}</span><Button variant="ghost" disabled={reviewOffset + 20 >= disputed.length} onClick={() => setReviewPage(Math.floor(reviewOffset / 20) + 1)}>Следующие спорные реплики</Button></div>}</Card>
  </div>;
}
