import { useCallback, useEffect, useRef, useState } from 'react';
import { participantsApi } from '../services/participants';
import type { PersonProfile, ProfileDraft } from '../services/participants';
import { aliasesFromText, bindDialogKeyboard, enrollmentError, RequestGate, uncertainOperation } from '../utils/enrollment';
import { EnrollmentWizard } from './EnrollmentWizard';
import { Button } from './ui/Button';
import { Card } from './ui/Card';

interface Props { onClose: () => void; onChanged?: () => void }
interface SaveOperation { draft: ProfileDraft; revision: number; operationId: string; editVersion: number }
function ProfileCard({ profile, onChanged, onEnroll }: { profile: PersonProfile; onChanged: () => void; onEnroll: () => void }) {
  const [name, setName] = useState(profile.display_name);
  const [aliases, setAliases] = useState(profile.aliases.join('\n'));
  const [enabled, setEnabled] = useState(profile.enabled);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(false);
  const [saved, setSaved] = useState(false);
  const edits = useRef(0);
  const gate = useRef(new RequestGate());
  const pending = useRef<SaveOperation | null>(null);
  useEffect(() => { const current = gate.current; return () => current.invalidate(); }, []);
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    const epoch = gate.current.begin(); if (epoch === null) return;
    const operation = pending.current ?? { draft: { display_name: name.trim(), aliases: aliasesFromText(aliases), enabled }, revision: profile.revision, operationId: crypto.randomUUID(), editVersion: edits.current };
    pending.current = operation; setBusy(true); setError(''); setSaved(false);
    try {
      const result = await participantsApi.patch(profile.id, operation.draft, operation.revision, operation.operationId);
      if (!gate.current.current(epoch)) return;
      pending.current = null; setRetry(false);
      if (edits.current === operation.editVersion) { setName(result.display_name); setAliases(result.aliases.join('\n')); setEnabled(result.enabled); setSaved(true); }
      onChanged();
    } catch (failure) {
      if (!gate.current.current(epoch)) return;
      setError(enrollmentError(failure));
      const uncertain = uncertainOperation(failure); setRetry(uncertain);
      if (!uncertain) { pending.current = null; onChanged(); }
    } finally { if (gate.current.current(epoch)) { gate.current.finish(epoch); setBusy(false); } }
  };
  const change = () => { ++edits.current; setSaved(false); };
  return <Card className="space-y-4">
    <form onSubmit={(event) => { void save(event); }} className="space-y-3">
      <p className="text-xs text-slate-400">{profile.enabled ? 'Профиль включён' : 'Профиль выключен'} · revision {profile.revision}</p>
      <label className="field-label">Имя участника<input required maxLength={200} value={name} onChange={(event) => { change(); setName(event.target.value); }} /></label>
      <label className="field-label">Другие имена · по одному в строке<textarea className="w-full rounded-lg border border-white/10 bg-[#15151f] p-3 text-sm text-slate-200" rows={3} value={aliases} onChange={(event) => { change(); setAliases(event.target.value); }} /><span className="field-hint">Изменения применяются к будущим встречам. Состав открытой встречи обновляется отдельно.</span></label>
      <label className="checkbox-label"><input type="checkbox" checked={enabled} onChange={(event) => { change(); setEnabled(event.target.checked); }} /><span>Профиль включён</span></label>
      {error && <p role="alert" className="break-words text-sm text-rose-200">{error}</p>}
      <p role="status" className="text-sm text-mint">{saved ? 'Профиль сохранён' : busy ? 'Сохранение… Черновик можно продолжать редактировать.' : retry ? 'Результат неизвестен. Повтор отправит прежний запрос; текущий черновик сохранится.' : ''}</p>
      <div className="flex flex-wrap gap-2"><Button type="submit" formNoValidate={retry} disabled={busy || (!retry && !name.trim())}>{retry ? 'Повторить сохранение' : 'Сохранить профиль'}</Button><Button variant="ghost" disabled={busy || retry} onClick={onEnroll}>Голосовые образцы</Button></div>
    </form>
  </Card>;
}

export function ParticipantsPanel({ onClose, onChanged }: Props) {
  const [profiles, setProfiles] = useState<PersonProfile[]>([]);
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const modal = useRef<HTMLElement>(null);
  const fetchEpoch = useRef(0);
  const gate = useRef(new RequestGate());
  const editVersion = useRef(0);
  const pendingCreate = useRef<{ name: string; operationId: string; editVersion: number } | null>(null);
  const callbacks = useRef({ onClose, onChanged });
  useEffect(() => { callbacks.current = { onClose, onChanged }; }, [onClose, onChanged]);
  const refresh = useCallback(async () => {
    const epoch = ++fetchEpoch.current;
    try { const items = await participantsApi.list(); if (epoch !== fetchEpoch.current) return; setProfiles(items); setLoading(false); }
    catch (failure) { if (epoch === fetchEpoch.current) { setError(enrollmentError(failure)); setLoading(false); } }
  }, []);
  const changed = useCallback(() => { void refresh(); callbacks.current.onChanged?.(); }, [refresh]);
  useEffect(() => { void Promise.resolve().then(refresh); const current = gate.current; const requests = fetchEpoch; return () => { ++requests.current; current.invalidate(); }; }, [refresh]);
  const close = useCallback(() => { if (!pendingCreate.current) callbacks.current.onClose(); else setError('Сначала завершите повтор создания: результат запроса ещё неизвестен.'); }, []);
  useEffect(() => { if (selected) return; return bindDialogKeyboard(modal.current, close); }, [selected, close]);
  const create = async (event: React.FormEvent) => {
    event.preventDefault(); const epoch = gate.current.begin(); if (epoch === null) return;
    const operation = pendingCreate.current ?? { name: name.trim(), operationId: crypto.randomUUID(), editVersion: editVersion.current };
    pendingCreate.current = operation; setBusy(true); setError('');
    try {
      await participantsApi.create({ display_name: operation.name, aliases: [], enabled: true }, operation.operationId);
      if (!gate.current.current(epoch)) return;
      pendingCreate.current = null; setRetry(false); if (editVersion.current === operation.editVersion) setName(''); changed();
    } catch (failure) {
      if (!gate.current.current(epoch)) return;
      setError(enrollmentError(failure)); const uncertain = uncertainOperation(failure); setRetry(uncertain); if (!uncertain) pendingCreate.current = null;
    } finally { if (gate.current.current(epoch)) { gate.current.finish(epoch); setBusy(false); } }
  };
  const selectedProfile = profiles.find((item) => item.id === selected);
  return <div className="modal-backdrop">
    {selectedProfile ? <EnrollmentWizard key={selectedProfile.id} profile={selectedProfile} onClose={() => setSelected(null)} onChanged={changed} /> : <section ref={modal} role="dialog" aria-modal="true" aria-labelledby="participants-title" className="settings-modal">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 p-5"><h2 id="participants-title" className="text-xl font-semibold">Участники</h2><Button variant="ghost" disabled={busy} onClick={close}>Закрыть</Button></header>
      <div className="space-y-5 p-5">
        <p className="text-sm leading-6 text-slate-300">Постоянные профили используются для состава будущих встреч и локальных голосовых образцов. Каждый образец требует отдельного согласия и проверки человеком.</p>
        {error && <p role="alert" className="break-words text-sm text-rose-200">{error}</p>}
        <form onSubmit={(event) => { void create(event); }} className="space-y-3">
          <label className="field-label">Имя нового участника<input required maxLength={200} value={name} onChange={(event) => { ++editVersion.current; setName(event.target.value); }} /></label>
          <div className="flex flex-wrap gap-2"><Button type="submit" formNoValidate={retry} disabled={busy || (!retry && !name.trim())}>{busy ? 'Создание…' : retry ? 'Повторить создание' : 'Добавить участника'}</Button><Button variant="ghost" onClick={() => { setError(''); void refresh(); }}>Обновить список</Button></div>
        </form>
        <p role="status">{loading ? 'Загрузка профилей…' : profiles.length ? `Профилей: ${profiles.length}` : 'Профилей пока нет'}</p>
        <div className="grid gap-4 sm:grid-cols-2">{profiles.map((profile) => <ProfileCard key={profile.id} profile={profile} onChanged={changed} onEnroll={() => setSelected(profile.id)} />)}</div>
      </div>
    </section>}
  </div>;
}
