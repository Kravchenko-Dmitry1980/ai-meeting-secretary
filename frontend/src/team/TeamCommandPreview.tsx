import { useEffect, useId, useRef } from 'react';
import { BUCKET_LABELS as bucketLabels } from './model';
import type { AssigneeBinding, Bucket, PublicAction, TaskCommand, TaskSnapshot } from './model';

export interface TeamCommandPreviewProps {
  command: TaskCommand | null;
  task?: TaskSnapshot | null;
  members: AssigneeBinding[];
  busy: boolean;
  error?: string | null;
  onConfirm: () => void;
  onClose: () => void;
}

const actionLabels: Record<PublicAction, string> = {
  create: 'Создать задачу', set_state: 'Изменить состояние', assign: 'Назначить ответственного',
  set_due: 'Изменить срок', classify: 'Изменить важность и срочность', comment: 'Добавить комментарий',
  propose_due: 'Предложить срок владельцу', rename: 'Изменить название',
};
const fieldLabels: Record<string, string> = {
  title: 'Название', description: 'Описание', assignee_id: 'Ответственный', bucket: 'Новое состояние',
  important: 'Важность', urgent: 'Срочность', classification_confirmed: 'Подтверждение классификации',
  due_at: 'Новый срок', due_phrase: 'Исходная фраза о сроке', due_timezone: 'Часовой пояс срока',
  due_confirmed: 'Подтверждение срока', reason: 'Причина', result: 'Результат выполнения', comment: 'Комментарий',
};

function fieldText(key: string, value: unknown, members: AssigneeBinding[]): string {
  if (key === 'due_at') {
    if (value === null) return 'Без срока';
    if (typeof value !== 'string') return 'Дата не определена';
    const date = new Date(value);
    return Number.isFinite(date.getTime())
      ? `${new Intl.DateTimeFormat('ru-RU', { timeZone: 'Europe/Moscow', dateStyle: 'long', timeStyle: 'short' }).format(date)} · Москва (UTC+03:00)`
      : 'Некорректная дата';
  }
  if (key === 'assignee_id') {
    const member = members.find((item) => item.id === value);
    return member?.display_name ? `${member.display_name} (${member.id})` : value === null ? 'Не выбран' : `Участник ${String(value)}`;
  }
  if (key === 'bucket') return typeof value === 'string' && value in bucketLabels ? bucketLabels[value as Bucket] : String(value);
  if (key === 'important') return value === true ? 'Важно' : value === false ? 'Неважно' : 'Не определено';
  if (key === 'urgent') return value === true ? 'Срочно' : value === false ? 'Несрочно' : 'Не определено';
  if (typeof value === 'boolean') return value ? 'Подтверждено' : 'Не подтверждено';
  if (value === null) return 'Не указано';
  return value === '' ? '(пусто)' : String(value);
}

export function TeamCommandPreview({ command, task, members, busy, error, onConfirm, onClose }: TeamCommandPreviewProps) {
  const dialog = useRef<HTMLDialogElement>(null);
  const id = useId();
  useEffect(() => {
    const element = dialog.current;
    if (!element || !command) return;
    const previousFocus = document.activeElement;
    if (!element.open) element.showModal();
    return () => {
      if (element.open) element.close();
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
    };
  }, [command]);
  if (!command) return null;
  const previewTask = task && task.task_id === command.task_id && task.project_id === command.project_id
    && task.revision === command.expected_revision && task.remote_fingerprint === command.expected_fingerprint ? task : null;
  const title = command.action === 'create' ? command.values.title : previewTask?.title;
  const entries = Object.entries(command.values).filter(([, value]) => value !== undefined);
  return <dialog ref={dialog} className="team-modal" aria-labelledby={`${id}-title`} aria-describedby={`${id}-notice`} onCancel={(event) => {
    event.preventDefault();
    if (!busy) onClose();
  }}>
    <div className="team-preview">
      <div className="team-modal-header">
        <h2 id={`${id}-title`}>Подтвердите команду</h2>
        <button className="team-button" type="button" disabled={busy} autoFocus onClick={onClose} aria-label="Закрыть предпросмотр">Закрыть</button>
      </div>
      <p className="team-preview-action">{command.action === 'link' ? 'Связать задачу' : command.action === 'resolve_due' ? 'Подтверждение внешнего срока' : actionLabels[command.action]}</p>
      <h3 className="team-literal">{title ?? `Задача ${command.task_id ?? 'без ID'}`}</h3>
      {command.task_id && task && !previewTask && <p className="team-notice">Предпросмотр относится к указанной ниже ревизии задачи. Текущая выбранная карточка или её версия уже отличается.</p>}
      <dl className="team-preview-values">{entries.map(([key, value]) => <div key={key}>
        <dt>{fieldLabels[key] ?? key}</dt><dd className="team-literal">{fieldText(key, value, members)}</dd>
      </div>)}</dl>
      {command.action === 'create' && command.values.due_at === undefined && <p className="team-hint">Срок не задан. Напоминания относительно срока отсутствуют.</p>}
      {command.action === 'create' && command.values.classification_confirmed !== true && <p className="team-hint">Важность и срочность не подтверждены: задача попадёт в «Не разобрано».</p>}
      {command.action === 'propose_due' && <p className="team-notice">Предложение требует решения владельца. Оно сразу не переносит действующий срок.</p>}
      <p id={`${id}-notice`} className="team-hint">В очередь уйдёт именно эта команда. Её принятие сервером ещё не означает исполнение: состояние задачи изменится после проверенной квитанции.</p>
      {error && <p role="alert" className="team-error">{error}</p>}
      <details className="team-technical"><summary>Технические сведения</summary>
        <dl><div><dt>Операция</dt><dd>{command.operation_id}</dd></div>
          <div><dt>Проект</dt><dd>{command.project_id}</dd></div>
          {command.task_id && <div><dt>Задача</dt><dd>{command.task_id}</dd></div>}
          {command.expected_revision !== undefined && command.expected_revision !== null && <div><dt>Ревизия задачи</dt><dd>{command.expected_revision}</dd></div>}
          {command.expected_fingerprint && <div><dt>Отпечаток версии</dt><dd>{command.expected_fingerprint}</dd></div>}
          {command.expected_assignee_revision !== undefined && command.expected_assignee_revision !== null && <div><dt>Ревизия связи участника</dt><dd>{command.expected_assignee_revision}</dd></div>}
          {typeof command.values.due_at === 'string' && <div><dt>Срок UTC</dt><dd>{command.values.due_at}</dd></div>}
          {command.origin && <div><dt>Источник</dt><dd>{command.origin.source_kind}</dd></div>}
        </dl>
      </details>
      <div className="team-modal-actions"><button className="team-button" type="button" disabled={busy} onClick={onClose}>Вернуться к черновику</button>
        <button className="team-button team-button-primary" type="button" disabled={busy} onClick={onConfirm}>{busy ? 'Отправка…' : 'Подтвердить команду'}</button>
      </div>
    </div>
  </dialog>;
}
