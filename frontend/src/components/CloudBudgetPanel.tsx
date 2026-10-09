import { useEffect, useState } from 'react';
import { api, errorMessage } from '../services/api';
import type { CloudBudgetOperation, CloudBudgetOperationDetails, CloudBudgetOperationPage } from '../types/api';
import { Button } from './ui/Button';
import { Card } from './ui/Card';

interface Props { enabled: boolean }

const statusText: Record<CloudBudgetOperation['status'], string> = {
  reserved: 'Резерв', submitted: 'Отправлен провайдеру', uncertain: 'Исход не подтверждён',
  confirmed: 'Расход подтверждён', released: 'Резерв снят после отказа',
};
const reasonText: Record<string, string> = {
  runtime_outbound_disabled: 'Внешние обращения выключены в этом запуске.',
  monthly_budget_key_changed: 'Активный API-ключ отличается от ключа этой операции.',
  monthly_budget_receipt_identity_unavailable: 'Для запроса нет сохранённого ID квитанции или задания.',
  monthly_budget_receipt_unavailable: 'Провайдер пока не вернул проверяемую квитанцию.',
  budget_receipt_identity_conflict: 'ID в ответе не совпал с сохранённым ID операции.',
  provider_pending: 'Провайдер сообщает, что запрос ещё обрабатывается.',
  provider_period_unavailable: 'Стоимость получена, но период списания не установлен; резерв сохранён.',
  receipt_cost_unavailable: 'Провайдер не подтвердил стоимость; резерв сохранён.',
};
const rub = (value: number | null) => value == null ? 'не подтверждено' : `${value.toLocaleString('ru-RU', { maximumFractionDigits: 6 })} ₽`;
const when = (value: number) => Number.isFinite(value) ? new Date(value).toLocaleString('ru-RU') : 'время неизвестно';

export function CloudBudgetPanel({ enabled }: Props) {
  const [status, setStatus] = useState('uncertain');
  const [page, setPage] = useState<CloudBudgetOperationPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [details, setDetails] = useState<CloudBudgetOperationDetails | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = async (nextOffset = 0, nextStatus = status) => {
    setLoading(true); setError('');
    try { setPage(await api.cloudBudgetOperations(nextStatus, 50, nextOffset)); setOffset(nextOffset); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setLoading(false); }
  };
  useEffect(() => {
    let active = true;
    api.cloudBudgetOperations(status, 50, 0).then((nextPage) => {
      if (active) { setPage(nextPage); setOffset(0); setError(''); }
    }).catch((failure: unknown) => {
      if (active) setError(errorMessage(failure));
    });
    return () => { active = false; };
  }, [status]);

  const showDetails = async (operation: CloudBudgetOperation) => {
    if (details?.operation.operation_id === operation.operation_id) { setDetails(null); return; }
    setError(''); setNotice('');
    try { setDetails(await api.cloudBudgetOperation(operation.operation_id)); }
    catch (failure) { setError(errorMessage(failure)); }
  };
  const reconcile = async (operation: CloudBudgetOperation) => {
    if (!enabled || busyId || !operation.provider_request_id && !operation.provider_job_id) return;
    setBusyId(operation.operation_id); setError(''); setNotice('');
    try {
      const result = await api.reconcileCloudBudgetOperation(operation.operation_id);
      setNotice(result.status === 'confirmed' ? 'Квитанция подтверждена; расход учтён один раз.' : 'Операция остаётся неопределённой. Резерв сохранён, повторная оплачиваемая отправка не выполнялась.');
      const [nextDetails] = await Promise.all([
        api.cloudBudgetOperation(operation.operation_id), load(offset, status),
      ]);
      setDetails(nextDetails);
      if (result.status === 'confirmed' || result.status === 'released') setStatus(result.status);
    } catch (failure) {
      setError(errorMessage(failure));
      try { setDetails(await api.cloudBudgetOperation(operation.operation_id)); } catch { /* The original safe error remains visible. */ }
      await load(offset, status);
    } finally { setBusyId(null); }
  };

  return <section aria-labelledby="cloud-budget-operations-title" className="space-y-3 border-t border-white/10 pt-5">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h3 id="cloud-budget-operations-title" className="font-medium">Сверка расходов Polza</h3>
        <p className="mt-1 max-w-2xl text-xs leading-5 text-slate-400">Сверка запрашивает только сохранённый ID операции. Она не создаёт новый платный запрос и не сбрасывает резерв или историю расходов.</p></div>
      <div className="flex items-center gap-2">
        <label className="sr-only" htmlFor="cloud-operation-status">Статус операций</label>
        <select id="cloud-operation-status" value={status} onChange={(event) => setStatus(event.target.value)}>
          <option value="uncertain">Неопределённые</option><option value="submitted">Отправленные</option>
          <option value="reserved">Зарезервированные</option><option value="confirmed">Подтверждённые</option><option value="released">Снятые резервы</option>
        </select>
        <Button variant="ghost" onClick={() => { void load(0); }} disabled={loading}>{loading ? 'Загрузка…' : 'Обновить'}</Button>
      </div>
    </div>
    {!enabled && <p role="status" className="text-xs leading-5 text-slate-500">Сверка отключена: включите облачную обработку и настройте ключ Polza. Внешний вызов будет возможен только в разрешённом запуске приложения.</p>}
    {error && <p role="alert" className="text-sm text-rose-300">{error}</p>}
    {notice && <p role="status" className="text-sm text-mint">{notice}</p>}
    {page?.items.length === 0 && !loading && <Card className="text-sm text-slate-400">Для выбранного статуса операций нет.</Card>}
    <div className="space-y-2">
      {page?.items.map((operation) => {
        const exactId = operation.provider_request_id ?? operation.provider_job_id;
        const mayReconcile = enabled && ['submitted', 'uncertain'].includes(operation.status) && Boolean(exactId);
        const open = details?.operation.operation_id === operation.operation_id;
        return <Card key={operation.operation_id} className="space-y-3">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0 space-y-1">
              <p className="font-medium">{statusText[operation.status] ?? operation.status} · {operation.category}</p>
              <p className="break-all text-xs text-slate-400">Операция: <code>{operation.operation_id}</code></p>
              <p className="break-all text-xs text-slate-400">ID запроса: <code>{operation.provider_request_id ?? 'нет'}</code> · ID задания: <code>{operation.provider_job_id ?? 'нет'}</code></p>
              <p className="text-xs text-slate-400">Создана {when(operation.created_ms)} · период {operation.period}</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button variant="ghost" onClick={() => { void showDetails(operation); }}>{open ? 'Скрыть журнал' : 'Открыть журнал'}</Button>
              {mayReconcile && <Button onClick={() => { void reconcile(operation); }} disabled={busyId !== null}>{busyId === operation.operation_id ? 'Проверка ID…' : 'Сверить точный ID'}</Button>}
            </div>
          </div>
          <div className="grid gap-2 text-xs text-slate-400 sm:grid-cols-4">
            <p>Оценка: <strong className="text-slate-200">{rub(operation.estimated_rub)}</strong></p>
            <p>Резерв: <strong className="text-slate-200">{rub(operation.reserved_rub)}</strong></p>
            <p>Наблюдалось: <strong className="text-slate-200">{rub(operation.observed_rub)}</strong></p>
            <p>Подтверждено: <strong className="text-slate-200">{rub(operation.confirmed_rub)}</strong></p>
          </div>
          {open && <div className="border-t border-white/10 pt-3">
            <h4 className="mb-2 text-sm font-medium">Журнал сверки</h4>
            {details.events.length === 0 ? <p className="text-xs text-slate-500">Сверка ещё не выполнялась.</p> : <ol className="space-y-2">
              {details.events.map((event) => <li key={event.event_id} className="text-xs leading-5 text-slate-400">
                <span className="text-slate-200">{when(event.occurred_ms)} · {event.outcome}</span>
                {event.reason_code && <span> · {reasonText[event.reason_code] ?? event.reason_code}</span>}
                {event.provider_request_id && <span> · ID запроса: <code>{event.provider_request_id}</code></span>}
                {event.provider_job_id && <span> · ID задания: <code>{event.provider_job_id}</code></span>}
                {event.provider_status && <span> · статус Polza: {event.provider_status}</span>}
                {event.provider_cost_rub != null && <span> · стоимость по квитанции: {rub(event.provider_cost_rub)}</span>}
                {event.provider_period && <span> · период: {event.provider_period}</span>}
              </li>)}
            </ol>}
          </div>}
        </Card>;
      })}
    </div>
    {page?.next_offset != null && <Button variant="ghost" onClick={() => { void load(page.next_offset!); }} disabled={loading}>Показать следующие 50</Button>}
  </section>;
}
