import { useEffect, useRef, useState } from 'react';
import { speakersApi } from '../services/speakers';
import type { AddParticipant } from '../services/speakers';
import type { MeetingParticipant, PersonProfile } from '../types/api';
import type { SpeakerWorkflow } from '../hooks/useSpeakerWorkflow';
import { Button } from './ui/Button';
import { Card } from './ui/Card';

export interface RosterDraft { profileIds: string[]; guests: string[] }
export function NewMeetingParticipants({ draft, onChange, disabled, onProfiles }: { draft: RosterDraft; onChange: (draft: RosterDraft) => void; disabled: boolean; onProfiles: () => void }) {
  const [profiles, setProfiles] = useState<PersonProfile[]>([]);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    void speakersApi.profiles().then((items) => { if (active) setProfiles(items); }).catch(() => { if (active) setError('Не удалось загрузить профили. Обновите список участников.'); });
    return () => { active = false; };
  }, []);
  return <fieldset disabled={disabled} className="speaker-roster-draft space-y-3">
    <legend className="text-sm font-medium">Кто присутствует на встрече</legend>
    <p className="text-xs leading-6 text-slate-400">Выберите присутствующих. Гость существует только в этой встрече. Голос не записывается при выборе профиля.</p>
    {error && <p role="alert" className="text-sm text-rose-200">{error}</p>}
    {profiles.filter((profile) => profile.enabled).map((profile) => <label className="checkbox-label" key={profile.id}><input type="checkbox" checked={draft.profileIds.includes(profile.id)} onChange={(event) => onChange({ ...draft, profileIds: event.target.checked ? [...draft.profileIds, profile.id] : draft.profileIds.filter((id) => id !== profile.id) })} />{profile.display_name}</label>)}
    {profiles.length === 0 && <p className="text-xs text-slate-400">Постоянные профили добавляются вручную на экране «Участники».</p>}
    {draft.guests.map((name, index) => <div className="flex flex-wrap gap-2" key={index}><label className="field-label flex-1">Имя гостя {index + 1}<input required maxLength={250} value={name} onChange={(event) => onChange({ ...draft, guests: draft.guests.map((value, i) => i === index ? event.target.value : value) })} /></label><Button variant="ghost" onClick={() => onChange({ ...draft, guests: draft.guests.filter((_, i) => i !== index) })}>Убрать гостя {index + 1}</Button></div>)}
    <div className="flex flex-wrap gap-2"><Button variant="ghost" onClick={() => onChange({ ...draft, guests: [...draft.guests, ''] })}>Добавить гостя</Button><Button variant="ghost" onClick={onProfiles}>Постоянные участники и образцы</Button></div>
  </fieldset>;
}

export function ParticipantEditor({ item, workflow }: { item: MeetingParticipant; workflow: SpeakerWorkflow }) {
  const [name, setName] = useState(item.display_name);
  const [aliases, setAliases] = useState(item.aliases.join('\n'));
  const [enabled, setEnabled] = useState(item.enabled);
  const [saved, setSaved] = useState(false);
  const edits = useRef(0);
  const change = () => { ++edits.current; setSaved(false); };
  const draftMatchesCanonical = name.trim() === item.display_name
    && JSON.stringify(aliases.split('\n').map((value) => value.trim()).filter(Boolean)) === JSON.stringify(item.aliases)
    && enabled === item.enabled;
  const save = async (event: React.FormEvent) => {
    event.preventDefault(); const edit = edits.current;
    const success = await workflow.patch(item.id, { display_name: name.trim(), aliases: aliases.split('\n').map((value) => value.trim()).filter(Boolean), enabled, apply_profile: false });
    if (success && edit === edits.current) setSaved(true);
  };
  return <form onSubmit={(event) => { void save(event); }} className="roster-editor space-y-3">
    <p className="text-xs text-slate-400">{item.person_profile_id ? 'Снимок постоянного профиля' : 'Гость этой встречи'} · {item.enabled ? 'Присутствует' : 'Выключен'}</p>
    <label className="field-label">Имя во встрече<input required maxLength={250} value={name} onChange={(event) => { change(); setName(event.target.value); }} /></label>
    <label className="field-label">Варианты имени во встрече<textarea rows={2} value={aliases} onChange={(event) => { change(); setAliases(event.target.value); }} /></label>
    <label className="checkbox-label"><input type="checkbox" checked={enabled} onChange={(event) => { change(); setEnabled(event.target.checked); }} />Участник включён</label>
    <div className="flex flex-wrap gap-2"><Button type="submit" disabled={workflow.busy || workflow.pending || !name.trim()}>Сохранить состав</Button>{item.person_profile_id && <Button variant="ghost" disabled={workflow.busy || workflow.pending} onClick={() => { setSaved(false); void workflow.patch(item.id, { apply_profile: true }); }}>Применить актуальное имя профиля</Button>}<Button variant="ghost" disabled={workflow.busy || workflow.pending} onClick={() => { change(); setName(item.display_name); setAliases(item.aliases.join('\n')); setEnabled(item.enabled); }}>Загрузить сохранённые значения</Button></div>
    <p role="status" className="text-xs text-mint">{saved && draftMatchesCanonical ? 'Этот черновик сохранён' : ''}</p>
  </form>;
}

export function MeetingRoster({ workflow }: { workflow: SpeakerWorkflow }) {
  const [profileId, setProfileId] = useState('');
  const [guestName, setGuestName] = useState('');
  const edits = useRef(0);
  const roster = workflow.roster;
  const add = async (draft: Omit<AddParticipant, 'expected_roster_revision' | 'operation_id'>) => {
    const edit = edits.current;
    if (await workflow.add(draft) && edit === edits.current) { setProfileId(''); setGuestName(''); }
  };
  return <Card className="space-y-4">
    <details className="speaker-roster-details"><summary>Состав встречи · revision {roster?.roster_revision ?? '…'} · {roster?.participants.length ?? 0} участников</summary>
      <p className="my-4 text-xs leading-6 text-slate-400">Имена и aliases сохранены отдельно для встречи. Изменения увеличивают revision и требуют проверки автоматических имён. Постоянный профиль не меняется.</p>
      <div className="speaker-roster-grid">{roster?.participants.map((item) => <ParticipantEditor key={item.id} item={item} workflow={workflow} />)}</div>
      <form className="mt-4 space-y-3" onSubmit={(event) => { event.preventDefault(); void add({ display_name: guestName.trim(), enabled: true }); }}><label className="field-label">Добавить гостя встречи<input required maxLength={250} value={guestName} onChange={(event) => { ++edits.current; setGuestName(event.target.value); }} /></label><Button type="submit" disabled={!guestName.trim() || !roster || workflow.busy || workflow.pending}>Добавить гостя встречи</Button></form>
      <div className="mt-4 flex flex-wrap items-end gap-3"><label className="field-label flex-1">Добавить постоянного участника<select value={profileId} onChange={(event) => { ++edits.current; setProfileId(event.target.value); }}><option value="">Выберите профиль</option>{workflow.profiles.filter((profile) => profile.enabled && !roster?.participants.some((item) => item.person_profile_id === profile.id)).map((profile) => <option value={profile.id} key={profile.id}>{profile.display_name}</option>)}</select></label><Button disabled={!profileId || !roster || workflow.busy || workflow.pending} onClick={() => { void add({ person_profile_id: profileId, enabled: true }); }}>Добавить из профиля</Button></div>
    </details>
  </Card>;
}
