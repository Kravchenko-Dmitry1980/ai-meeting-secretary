import { useId, useState } from 'react';
import { Activity, Clock3, LoaderCircle, RefreshCw, ShieldCheck } from 'lucide-react';
import type { Actor, TeamApi } from './api';
import { useTeamDashboard } from './useTeamDashboard';

export interface TeamOwnerDashboardProps {
  api: TeamApi;
  actor: Actor | null;
  sessionEpoch: number;
  projectId: string | null;
}
const commandStates: Record<string,string>={queued:'В очереди',running:'Выполняется',reconciling:'Сверяется результат',uncertain:'Результат неизвестен'};
const actions: Record<string,string>={create:'Создание задачи',set_state:'Изменение состояния',assign:'Назначение исполнителя',
  set_due:'Изменение срока',resolve_due:'Подтверждение внешнего срока',classify:'Важность и срочность',comment:'Комментарий',propose_due:'Предложение срока',rename:'Название',link:'Связь задачи'};
const errors: Record<string,string>={
  team_dashboard_unavailable:'Диагностика сейчас недоступна.',team_dashboard_response_invalid:'Ответ диагностики не прошёл проверку.',
  team_dashboard_cursor_invalid:'Следующая страница изменилась. Обновите список сначала.',team_owner_required:'Доступно только владельцу.',
  team_session_expired:'Сеанс завершён. Войдите снова.',team_forbidden:'Доступ к проекту изменился.',
  task_revision_conflict:'Версия задачи изменилась.',member_mapping_changed:'Связь участника изменилась.',
  actor_binding_changed:'Права участника изменились.',remote_timeout:'Ответ внешнего сервиса не подтверждён.',
  monthly_budget_exhausted:'Месячный бюджет Polza исчерпан.',account_snapshot_stale:'Нужна свежая проверка расходов Polza.',
};
const errorText=(value:string|null|undefined)=>value?errors[value]??'Требуется проверка состояния.':'';
function date(value:string|null|undefined):string {
  if(!value)return'Время не подтверждено';
  const parsed=new Date(value);
  return Number.isFinite(parsed.getTime())?new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Moscow',dateStyle:'short',timeStyle:'short'}).format(parsed)+' МСК':'Время не подтверждено';
}
function rubles(value:string|null|undefined):string {
  if(typeof value!=='string'||!/^\d+\.\d{6}$/.test(value))return'Неизвестно';
  const [whole,fraction]=value.split('.');
  const decimal=fraction.replace(/0+$/,'').padEnd(2,'0');
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g,' ')+','+decimal+' ₽';
}
const count=(value:number|null|undefined)=>typeof value==='number'&&Number.isSafeInteger(value)&&value>=0?String(value):'Неизвестно';
function sourceState(value:string|undefined):string {
  return value==='not_configured'?'Источник не подключён':value==='unavailable'?'Данные источника недоступны'
    :value==='issues'?'Есть неподтверждённые или неуспешные отправки':value==='observed'?'Состояние прочитано из журнала':'Состояние неизвестно';
}

export function TeamOwnerDashboard({api,actor,sessionEpoch,projectId}:TeamOwnerDashboardProps) {
  const id=useId();
  const context=JSON.stringify([actor?.id,actor?.revision,sessionEpoch,projectId]);
  const [panel,setPanel]=useState({context:'',open:false});
  const open=panel.context===context&&panel.open;
  const {state,store}=useTeamDashboard(api,{actor,sessionEpoch,projectId,expanded:open});
  if(actor?.role!=='owner'||api.actor?.id!==actor.id||api.actor.role!=='owner'||api.sessionEpoch!==sessionEpoch)return null;
  const matching=state.scope?.actorId===actor.id&&state.scope.actorRevision===actor.revision
    && state.scope.sessionEpoch===sessionEpoch&&state.scope.projectId===projectId;
  const data=matching?state.data:null;
  const rows=matching?state.commands:[];
  const cloud=data?.status.cloud;
  const lastSuccessful=matching?state.lastSuccessfulAt:null;
  return <details className="team-owner-dashboard team-panel" open={open} onToggle={(event)=>{
    const next=event.currentTarget.open;
    if(next!==open)setPanel({context,open:next});
  }}>
    <summary><ShieldCheck size={18} aria-hidden="true" /><span>Диагностика владельца</span><span className="team-muted">Команды, доставка, webhook и бюджет</span></summary>
    <div className="team-owner-dashboard-body">
      {!projectId||!actor.project_ids.includes(projectId)?<p className="team-muted">Выберите доступный проект для диагностики.</p>:<>
        <div className="team-owner-dashboard-toolbar"><p className="team-muted">Последняя успешная проверка этой панели: {date(lastSuccessful)}</p>
          <button type="button" disabled={matching&&(state.loading||state.pageLoading)} onClick={()=>{void store.refresh();}}>
            {matching&&state.loading?<LoaderCircle size={15} className="team-spinner" aria-hidden="true"/>:<RefreshCw size={15} aria-hidden="true"/>} Проверить состояние
          </button></div>
        {matching&&state.errorCode&&<p className="team-notice error" role="alert">{errorText(state.errorCode)} <small>Код: {state.errorCode}</small></p>}
        {matching&&state.autoPaused&&<p className="team-muted">Автопроверка приостановлена после ошибки. Кнопка «Проверить состояние» возобновит её.</p>}
        {matching&&state.loading&&<p role="status" className="team-statusline"><LoaderCircle size={16} className="team-spinner" aria-hidden="true"/> Читаем текущие журналы…</p>}
        <div className="team-owner-health-grid">
          <section className="team-owner-health" aria-labelledby={`${id}-delivery`}><h3 id={`${id}-delivery`}><Activity size={16} aria-hidden="true"/> Доставка сообщений</h3>
            <p className="team-muted">{sourceState(data?.deliveries.state)}</p>
            <p className="team-muted">Учтённые источники: {data?.deliveries.sources.length?data.deliveries.sources.map(source=>source==='bot'?'Кнопки и команды бота':'Ответы на голосовые команды').join(', '):'Не подтверждены'}. Это счётчики этих источников, а не всей переписки MAX.</p>
            <dl className="team-owner-metrics"><div><dt>Ожидают отправки</dt><dd>{count(data?.deliveries.pending_count)}</dd></div>
              <div><dt>Отправляются</dt><dd>{count(data?.deliveries.sending_count)}</dd></div><div><dt>Можно повторить после известного отказа</dt><dd>{count(data?.deliveries.retryable_count)}</dd></div>
              <div><dt>Отправка не подтверждена</dt><dd>{count(data?.deliveries.uncertain_count)}</dd></div><div><dt>Отклонены</dt><dd>{count(data?.deliveries.rejected_count)}</dd></div>
              <div><dt>Принято MAX API</dt><dd>{count(data?.deliveries.max_api_accepted_count)}</dd></div></dl>
            <p className="team-muted">Последнее принятие MAX API: {date(data?.deliveries.last_max_api_accepted_at)}. Прочтение человеком не подтверждено.</p>
          </section>
          <section className="team-owner-health" aria-labelledby={`${id}-reminders`}><h3 id={`${id}-reminders`}><Clock3 size={16} aria-hidden="true"/> Напоминания</h3>
            <p className="team-muted">{sourceState(data?.notifications.state)}</p><dl className="team-owner-metrics">
              <div><dt>Ожидают отправки</dt><dd>{count(data?.notifications.pending_count)}</dd></div><div><dt>Отправляются</dt><dd>{count(data?.notifications.sending_count)}</dd></div>
              <div><dt>Отправка не подтверждена</dt><dd>{count(data?.notifications.uncertain_count)}</dd></div><div><dt>Не отправлены</dt><dd>{count(data?.notifications.failed_count)}</dd></div>
              <div><dt>Отменены</dt><dd>{count(data?.notifications.cancelled_count)}</dd></div></dl>
            <p className="team-muted">Последнее принятие MAX API: {date(data?.notifications.last_max_api_accepted_at)}. Прочтение человеком не подтверждено.</p>
          </section>
          <section className="team-owner-health" aria-labelledby={`${id}-webhook`}><h3 id={`${id}-webhook`}>Webhook MAX</h3>
            <p>{data?.webhook.state==='not_configured'?'Webhook не настроен':data?.webhook.state==='unavailable'?'Журнал приёма недоступен'
              :data?.webhook.state==='observed'?'Наблюдения подписки записаны':data?.webhook.state==='issues'?'Подписка требует проверки':'Внешняя доступность не подтверждена'}</p>
            <p className="team-muted">Адрес подписки: {data?.webhook.configured_subscription===true?'настроен':data?.webhook.configured_subscription===false?'не настроен':'не подтверждён'}.</p>
            <p className="team-muted">Подписка: {data?.webhook.subscription_state==='present'?'обнаружена в MAX API':data?.webhook.subscription_state==='verified'?'изменение подтверждено MAX API'
              :data?.webhook.subscription_state==='missing'?'не обнаружена':data?.webhook.subscription_state==='rejected'?'изменение отклонено'
              :data?.webhook.subscription_state==='uncertain'?'результат изменения неизвестен':data?.webhook.subscription_state==='unavailable'?'проверка недоступна':'не проверена'}.</p>
            <p className="team-muted">Последняя проверка подписки: {date(data?.webhook.last_subscription_observed_at)}. Последний доверенный callback: {date(data?.webhook.last_trusted_callback_at)}.</p>
            <p className="team-muted">Последний проверенный приём: {date(data?.webhook.last_authenticated_accept_at)}. Локальная запись не доказывает доступность HTTPS из MAX.</p>
            <p className="team-muted">Доставка на телефон не подтверждена; требуется проверка на реальном устройстве.</p>
          </section>
          <section className="team-owner-health" aria-labelledby={`${id}-budget`}><h3 id={`${id}-budget`}>Месячный бюджет Polza</h3>
            <p>{!cloud?'Состояние бюджета неизвестно':cloud.state==='not_configured'?'Бюджет не настроен':cloud.state==='paused'?'Расходы приостановлены':cloud.state==='unavailable'?'Данные бюджета недоступны':'Обработка доступна в пределах бюджета'}</p>
            <dl className="team-owner-metrics"><div><dt>Учтённый расход</dt><dd>{rubles(cloud?.spend_rub)}</dd></div><div><dt>Зарезервировано</dt><dd>{rubles(cloud?.reserved_rub)}</dd></div>
              <div><dt>Остаток</dt><dd>{rubles(cloud?.remaining_rub)}</dd></div><div><dt>Действующий лимит</dt><dd>{rubles(cloud?.effective_budget_rub)}</dd></div></dl>
            {cloud?.paused_reason&&<p className="team-muted">{errorText(cloud.paused_reason)} <small>Код: {cloud.paused_reason}</small></p>}
            <p className="team-muted">Просмотр этой панели не вызывает платные запросы Polza.</p>
          </section>
        </div>
        <section className="team-owner-commands" aria-labelledby={`${id}-commands`}><h3 id={`${id}-commands`}>Ожидающие команды</h3>
          <p className="team-muted">Очередь, выполнение, сверка и неизвестный результат. Нахождение в очереди само по себе не означает зависание.</p>
          {!data&&!state.loading&&<p className="team-muted">Количество и список команд не подтверждены.</p>}
          {data&&!rows.length&&<p className="team-muted">На момент проверки ожидающих команд не обнаружено.</p>}
          <ol className="team-owner-command-list">{rows.map(row=><li key={row.operation_id}><div className="team-owner-command-heading"><strong>{actions[row.action]??'Команда задачи'}</strong><span className={`team-pill ${row.state==='uncertain'?'overdue':''}`}>{commandStates[row.state]??'Состояние неизвестно'}</span></div>
            <p className="team-muted">Принята в очередь: {date(row.accepted_at)}<br/>Последнее изменение состояния: {date(row.last_state_at)}</p>
            {row.error_code&&<p className="team-error">{errorText(row.error_code)} <small>Код: {row.error_code}</small></p>}
            <details className="team-technical"><summary>Сведения о команде</summary><dl className="team-owner-metrics"><div><dt>ID операции</dt><dd>{row.operation_id}</dd></div>
              <div><dt>ID задачи</dt><dd>{row.task_id??'Ещё не подтверждён'}</dd></div><div><dt>Срок служебной аренды</dt><dd>{date(row.lease_until)}</dd></div></dl></details>
          </li>)}</ol>
          {matching&&state.pageErrorCode&&<p className="team-notice error" role="alert">{errorText(state.pageErrorCode)} <small>Код: {state.pageErrorCode}</small></p>}
          {matching&&state.nextCursor&&<button type="button" disabled={state.loading||state.pageLoading} onClick={()=>{void store.loadMore();}}>{state.pageLoading?'Загружаем…':'Показать следующие команды'}</button>}
          {matching&&state.paginationLimited&&<p className="team-muted">Показан ограниченный набор команд. Обновите список для новой проверки; полнота списка не подтверждена.</p>}
        </section>
      </>}
    </div>
  </details>;
}
