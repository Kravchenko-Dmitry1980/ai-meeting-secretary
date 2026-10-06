import { useId } from 'react';
import { BUCKET_LABELS as bucketLabels, completionValues } from './model';
import type { Actor, AssigneeBinding, Bucket, PublicAction, TaskChange, TaskSnapshot, TeamDraft } from './model';

export interface TeamTaskFormProps {
  actor: Actor;
  task: TaskSnapshot | null;
  members: AssigneeBinding[];
  draft: TeamDraft;
  onDraft: (patch: Partial<TeamDraft>) => void;
  onPreview: () => void;
  error?: string | null;
}

const actionLabels: Record<PublicAction, string> = {
  create: 'Создать задачу', set_state: 'Изменить состояние', assign: 'Назначить ответственного',
  set_due: 'Изменить срок', classify: 'Важность и срочность', comment: 'Добавить комментарий',
  propose_due: 'Предложить срок', rename: 'Изменить название',
};
/** datetime-local deliberately means Moscow wall time, independently of the device zone. */
function utcFromMoscow(value: string): string | undefined {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) return undefined;
  if (value.startsWith('0000-')) return undefined;
  const date = new Date(`${value}:00+03:00`);
  if (!Number.isFinite(date.getTime())) return undefined;
  const roundTrip = new Date(date.getTime() + 3 * 60 * 60 * 1000).toISOString().slice(0, 16);
  return roundTrip === value ? date.toISOString() : undefined;
}

function moscowInput(value: string | null | undefined): string {
  if (!value) return '';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '';
  return new Date(date.getTime() + 3 * 60 * 60 * 1000).toISOString().slice(0, 16);
}

export function TeamTaskForm({ actor, task, members, draft, onDraft, onPreview, error }: TeamTaskFormProps) {
  const id = useId();
  const owner = actor.role === 'owner';
  const ownTask = task?.assignee_id === actor.id;
  const openTask = !!task && task.bucket !== 'done' && task.bucket !== 'cancelled';
  const actions: PublicAction[] = owner ? ['create'] : [];
  if (task) {
    if (owner || ownTask && openTask) actions.push('set_state');
    if (owner) actions.push('assign', 'rename', 'classify', 'set_due');
    if (owner || ownTask) actions.push('propose_due');
    actions.push('comment');
  }
  const allowed = actions.includes(draft.action) && (draft.action === 'create' || task?.task_id === draft.taskId);
  const values = draft.values;
  const update = (patch: Partial<TaskChange>) => onDraft({ values: { ...values, ...patch } });
  const availableMembers = members.filter((member) => member.enabled !== false && (!task || member.project_ids.includes(task.project_id)));
  const isCreate = draft.action === 'create';
  const hasDue = isCreate || draft.action === 'set_due' || draft.action === 'propose_due';
  const clearingDue = values.due_at === null && values.due_confirmed === true;
  const hasAxes = draft.action === 'classify' || isCreate && values.classification_confirmed === true;
  const completionTarget: Bucket = task && (task.important !== false || !task.classification_confirmed)
    && !(owner && task.bucket === 'review') ? 'review' : 'done';
  const completing = values.bucket === 'done' || values.bucket === 'review' && typeof values.result === 'string';
  const selectedState = completing ? 'complete' : values.bucket ?? '';

  return <form className="team-form" aria-label="Черновик команды" onSubmit={(event) => {
    event.preventDefault();
    if (allowed) onPreview();
  }}>
    <label className="team-field" htmlFor={`${id}-action`}>Действие
      <select id={`${id}-action`} value={draft.action} disabled={!actions.length} onChange={(event) => {
        const action = event.target.value as PublicAction;
        onDraft({ action, taskId: action === 'create' ? null : task?.task_id ?? null, values: {}, assignee: null });
      }}>
        {!actions.includes(draft.action) && <option value={draft.action} disabled>{actionLabels[draft.action]} · недоступно</option>}
        {actions.map((action) => <option key={action} value={action}>{actionLabels[action]}</option>)}
      </select>
    </label>
    {!allowed && <p className="team-notice">Выберите доступное действие и задачу. Изменение прав или проекта требует нового черновика.</p>}
    <fieldset disabled={!allowed} className="team-form-fields">
      <legend className="team-sr-only">Параметры команды</legend>
      {(isCreate || draft.action === 'rename') && <label className="team-field" htmlFor={`${id}-title`}>Название
        <input id={`${id}-title`} required maxLength={4000} value={values.title ?? ''} onChange={(event) => update({ title: event.target.value })} />
      </label>}
      {isCreate && <label className="team-field" htmlFor={`${id}-description`}>Описание
        <textarea id={`${id}-description`} maxLength={4000} rows={3} value={values.description ?? ''} onChange={(event) => update({ description: event.target.value })} />
      </label>}
      {(isCreate || draft.action === 'assign') && <label className="team-field" htmlFor={`${id}-assignee`}>Ответственный
        <select id={`${id}-assignee`} required value={values.assignee_id ?? ''} onChange={(event) => {
          const member = availableMembers.find((item) => item.id === event.target.value) ?? null;
          onDraft({ values: { ...values, assignee_id: member?.id }, assignee: member });
        }}>
          <option value="">Выберите участника</option>
          {values.assignee_id && !availableMembers.some((member) => member.id === values.assignee_id)
            && <option value={values.assignee_id} disabled>Участник недоступен · выберите заново</option>}
          {availableMembers.map((member) => <option key={member.id} value={member.id}>{member.display_name ?? `Участник ${member.id}`}</option>)}
        </select>
        {!availableMembers.length && <span className="team-hint">Справочник участников пока недоступен. Назначение требует проверенной связи и её ревизии.</span>}
      </label>}
      {draft.action === 'set_state' && <>
        <label className="team-field" htmlFor={`${id}-bucket`}>Новое состояние
          <select id={`${id}-bucket`} required value={selectedState} onChange={(event) => {
            const next = event.target.value;
            update(next === 'complete' ? { bucket: completionTarget, result: values.result ?? '' }
              : { bucket: next === '' ? undefined : next as Bucket, result: undefined });
          }}>
            <option value="">Выберите состояние</option>
            {(Object.keys(bucketLabels) as Bucket[]).filter((bucket) => bucket !== 'done' && (owner || bucket !== 'inbox' && bucket !== 'cancelled'))
              .map((bucket) => <option key={bucket} value={bucket}>{bucketLabels[bucket]}</option>)}
            <option value="complete">{completionTarget === 'review' ? 'Готово → На проверку' : 'Закрыть с результатом'}</option>
          </select>
        </label>
        {completing && <label className="team-field" htmlFor={`${id}-result`}>Результат выполнения
          <textarea id={`${id}-result`} required maxLength={4000} rows={3} value={values.result ?? ''} onChange={(event) => {
            const result = event.target.value;
            update(task && result.trim() ? completionValues(task, result, actor) : { result });
          }} />
          {completionTarget === 'review' && <span className="team-hint">Важная или неразобранная задача передаётся на проверку. Закрыть её после проверки может владелец.</span>}
        </label>}
      </>}
      {isCreate && <label className="team-checkbox" htmlFor={`${id}-classify-create`}>
        <input id={`${id}-classify-create`} type="checkbox" checked={values.classification_confirmed === true} onChange={(event) => {
          update(event.target.checked ? { classification_confirmed: true }
            : { classification_confirmed: undefined, important: undefined, urgent: undefined });
        }} />Подтвердить важность и срочность
      </label>}
      {hasAxes && <div className="team-form-row">
        <label className="team-field" htmlFor={`${id}-important`}>Важность
          <select id={`${id}-important`} required value={values.important === true ? 'true' : values.important === false ? 'false' : ''} onChange={(event) => {
            update({ important: event.target.value === '' ? undefined : event.target.value === 'true', classification_confirmed: true });
          }}><option value="">Выберите важность</option><option value="true">Важно</option><option value="false">Неважно</option></select>
        </label>
        <label className="team-field" htmlFor={`${id}-urgent`}>Срочность
          <select id={`${id}-urgent`} required value={values.urgent === true ? 'true' : values.urgent === false ? 'false' : ''} onChange={(event) => {
            update({ urgent: event.target.value === '' ? undefined : event.target.value === 'true', classification_confirmed: true });
          }}><option value="">Выберите срочность</option><option value="true">Срочно</option><option value="false">Несрочно</option></select>
        </label>
      </div>}
      {hasDue && <>
        <label className="team-checkbox" htmlFor={`${id}-no-due`}>
          <input id={`${id}-no-due`} type="checkbox" checked={clearingDue} onChange={(event) => {
            update(event.target.checked ? { due_at: null, due_confirmed: true, due_timezone: 'Europe/Moscow', due_phrase: 'Без срока' }
              : { due_at: undefined, due_confirmed: undefined, due_phrase: undefined, due_timezone: undefined });
          }} />Без срока
        </label>
        {!clearingDue && <label className="team-field" htmlFor={`${id}-due`}>Дата и время · Москва (UTC+03:00)
          <input id={`${id}-due`} type="datetime-local" required={!isCreate} step="60" value={moscowInput(values.due_at)} onChange={(event) => {
            const due = utcFromMoscow(event.target.value);
            update({ due_at: due, due_confirmed: due ? true : undefined, due_timezone: due ? 'Europe/Moscow' : undefined,
              ...(!due ? { due_phrase: undefined } : {}) });
          }} />
          <span className="team-hint">Укажите время явно. На устройстве в другом часовом поясе это поле также означает московское время.</span>
        </label>}
        {values.due_at && <label className="team-field" htmlFor={`${id}-due-phrase`}>Исходная фраза о сроке (необязательно)
          <input id={`${id}-due-phrase`} maxLength={4000} value={values.due_phrase ?? ''} onChange={(event) => update({ due_phrase: event.target.value })} />
        </label>}
        {!isCreate && <label className="team-field" htmlFor={`${id}-reason`}>Причина изменения срока
          <textarea id={`${id}-reason`} required maxLength={4000} rows={2} value={values.reason ?? ''} onChange={(event) => update({ reason: event.target.value })} />
          {draft.action === 'propose_due' && <span className="team-hint">Предложение сохранится для проверки владельцем. Действующий срок сразу не изменится.</span>}
        </label>}
      </>}
      {draft.action === 'comment' && <label className="team-field" htmlFor={`${id}-comment`}>Комментарий
        <textarea id={`${id}-comment`} required maxLength={4000} rows={3} value={values.comment ?? ''} onChange={(event) => update({ comment: event.target.value })} />
      </label>}
      <p className="team-hint">Сначала будет показан предпросмотр. Изменение выполняется после отдельного подтверждения и квитанции сервера.</p>
      {error && <p role="alert" className="team-error">{error}</p>}
      <button className="team-button team-button-primary" type="submit">Предпросмотр команды</button>
    </fieldset>
  </form>;
}
