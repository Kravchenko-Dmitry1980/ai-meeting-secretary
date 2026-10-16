import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage } from '../services/api';
import { speakersApi } from '../services/speakers';
import type { AddParticipant, BypassCommand, IdentifyCommand, PatchParticipant, ReviewAttribution } from '../services/speakers';
import type { AttributionChange, AttributionSnapshot, IdentificationState, ParticipantRoster, PersonProfile } from '../types/api';

type Command = { kind: 'add'; body: AddParticipant } | { kind: 'patch'; participantId: string; body: PatchParticipant }
  | { kind: 'review'; body: ReviewAttribution } | { kind: 'identify'; body: IdentifyCommand } | { kind: 'bypass'; body: BypassCommand };
interface Data { scope: string; roster: ParticipantRoster; snapshot: AttributionSnapshot; profiles: PersonProfile[] }

export function useSpeakerWorkflow(meetingId: string, version: number, selectionEpoch = 0, onStaleVersion?: (meetingId: string, version: number) => void) {
  const scope = `${meetingId}:${version}:${selectionEpoch}`;
  const commandKey = `${meetingId}:${version}`;
  const [data, setData] = useState<Data | null>(null);
  const [status, setStatus] = useState({ scope, busy: false, pending: false, error: '' });
  const setBusy = (busy: boolean) => setStatus((old) => ({ ...(old.scope === scope ? old : { scope, pending: false, error: '' }), busy }));
  const setPending = (pending: boolean) => setStatus((old) => ({ ...(old.scope === scope ? old : { scope, busy: false, error: '' }), pending }));
  const setError = useCallback((error: string) => setStatus((old) => ({ ...(old.scope === scope ? old : { scope, busy: false, pending: false }), error })), [scope]);
  const [localReceipt, setLocalReceipt] = useState<{ scope: string; state: IdentificationState } | null>(null);
  const [pendingKeys, setPendingKeys] = useState<string[]>([]);
  const commands = useRef(new Map<string, Command>());
  const locked = useRef(false);
  const epoch = useRef(0);
  const readEpoch = useRef(0);
  const refresh = useCallback(async () => {
    if (!meetingId) return;
    const owner = epoch.current; const read = ++readEpoch.current;
    try {
      const [roster, snapshot, profiles] = await Promise.all([speakersApi.roster(meetingId), speakersApi.attribution(meetingId, version), speakersApi.profiles()]);
      if (owner !== epoch.current || read !== readEpoch.current) return;
      if (roster.meeting_id !== meetingId || snapshot.meeting_id !== meetingId || snapshot.transcript_version !== version) throw new Error('Сервер вернул данные другой встречи или версии. Обновите состояние.');
      const value = { scope, roster, snapshot, profiles }; setData(value);
    } catch (failure) {
      if (owner !== epoch.current || read !== readEpoch.current) return;
      const response = failure as { status?: number; message?: string };
      if (response.status === 404 && response.message === 'Transcript version not found') {
        // Upload may advance the server version before the selected meeting refresh reaches React.
        setError('');
        onStaleVersion?.(meetingId, version);
        return;
      }
      setError(errorMessage(failure));
    }
  }, [meetingId, version, scope, setError, onStaleVersion]);
  useEffect(() => {
    void Promise.resolve().then(() => { setStatus({ scope, busy: false, pending: false, error: '' }); void refresh(); });
    let polling = false;
    const poll = window.setInterval(() => { if (!locked.current && !polling) { polling = true; void refresh().finally(() => { polling = false; }); } }, 15000);
    const epochs = epoch, reads = readEpoch;
    return () => { window.clearInterval(poll); ++epochs.current; ++reads.current; locked.current = false; };
  }, [refresh, scope]);

  const execute = async (captured: Command): Promise<boolean> => {
    if (locked.current) return false;
    locked.current = true; commands.current.set(commandKey, captured); const owner = epoch.current;
    setPendingKeys((keys) => keys.includes(commandKey) ? keys : [...keys, commandKey]);
    ++readEpoch.current; setBusy(true); setError('');
    try {
      switch (captured.kind) {
        case 'add': await speakersApi.add(meetingId, captured.body); break;
        case 'patch': await speakersApi.patch(meetingId, captured.participantId, captured.body); break;
        case 'review': await speakersApi.review(meetingId, captured.body); break;
        case 'identify': await speakersApi.identify(meetingId, captured.body); break;
        case 'bypass': {
          const result = await speakersApi.bypass(meetingId, captured.body);
          if (owner === epoch.current) setLocalReceipt({ scope, state: result }); break;
        }
      }
      if (commands.current.get(commandKey) === captured) { commands.current.delete(commandKey); setPendingKeys((keys) => keys.filter((key) => key !== commandKey)); }
      if (owner !== epoch.current) return false;
      setPending(false); await refresh(); return true;
    } catch (failure) {
      const status = (failure as { status?: number })?.status;
      const uncertain = status == null || status >= 500 || status === 408;
      if (!uncertain && commands.current.get(commandKey) === captured) { commands.current.delete(commandKey); setPendingKeys((keys) => keys.filter((key) => key !== commandKey)); }
      if (owner !== epoch.current) return false;
      setPending(uncertain);
      setError(status === 409 ? 'Данные изменились (409). Черновик сохранён. Проверьте обновлённые состав и ревизию перед новым сохранением.'
        : uncertain ? `${errorMessage(failure)} Результат неизвестен. Повтор отправит прежний запрос и ID; новых команд до ответа не будет.` : errorMessage(failure));
      if (!uncertain) await refresh(); return false;
    } finally { if (owner === epoch.current) { locked.current = false; setBusy(false); } }
  };
  const capture = (make: (value: Data, operationId: string) => Command): Promise<boolean> => {
    // Capture the revision rendered with the decision. A newer GET that has
    // not reached the UI cannot silently grant authority to an older draft.
    const value = data;
    if (commands.current.has(commandKey) || locked.current || value?.scope !== scope) return Promise.resolve(false);
    return execute(make(value, crypto.randomUUID()));
  };
  const current = data?.scope === scope ? data : null;
  return {
    roster: current?.roster ?? null, snapshot: current?.snapshot ?? null, profiles: current?.profiles ?? [], busy: status.scope === scope && status.busy, pending: pendingKeys.includes(commandKey) && !(status.scope === scope && status.busy), error: status.scope === scope ? status.error : '', lastLocalState: localReceipt?.scope === scope ? localReceipt.state : null, refresh,
    repeat: () => { const captured = commands.current.get(commandKey); return captured ? execute(captured) : Promise.resolve(false); },
    add: (draft: Omit<AddParticipant, 'expected_roster_revision' | 'operation_id'>) => capture((value, id) => ({ kind: 'add', body: { ...draft, expected_roster_revision: value.roster.roster_revision, operation_id: id } })),
    patch: (participantId: string, draft: Omit<PatchParticipant, 'expected_roster_revision' | 'operation_id'>) => capture((value, id) => ({ kind: 'patch', participantId, body: { ...draft, expected_roster_revision: value.roster.roster_revision, operation_id: id } })),
    review: (changes: AttributionChange[]) => capture((value, id) => ({ kind: 'review', body: { transcript_version: version, expected_revision: value.snapshot.revision, operation_id: id, changes } })),
    identify: (retry = false) => capture((value, id) => ({ kind: 'identify', body: { transcript_version: version, expected_revision: value.snapshot.revision, operation_id: id, retry } })),
    bypass: () => capture((value, id) => ({ kind: 'bypass', body: { transcript_version: version, expected_revision: value.snapshot.revision, operation_id: id } })),
  };
}
export type SpeakerWorkflow = ReturnType<typeof useSpeakerWorkflow>;
