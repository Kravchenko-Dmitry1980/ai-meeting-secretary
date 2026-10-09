import { CheckCircle2, ExternalLink, KeyRound, SlidersHorizontal, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { AppConfig, ModelCatalog, ModelInfo } from '../types/api';
import { Badge } from './ui/Badge';
import { Button } from './ui/Button';
import { Card } from './ui/Card';
import { CloudBudgetPanel } from './CloudBudgetPanel';

interface Props { config: AppConfig; catalog: ModelCatalog | null; busy: boolean; onClose: () => void; onSave: (config: Omit<AppConfig, 'key_configured'>) => Promise<boolean> }
const russianSupport = (value?: string | boolean) => value === true || value === 'language_ru_documented_not_live_verified' ? 'заявлен, не проверен' : value === false ? 'нет' : 'не проверен';
const timingSupport = (value?: string | boolean) => ({ provider_segment: 'сегменты; точность не проверена', provider_segments_if_present: 'сегменты при наличии', chunk_only: 'границы фрагмента', word: 'по словам', segment: 'по сегментам' })[String(value)] ?? (value === false ? 'нет' : value === true ? 'заявлены, не проверены' : 'не подтверждены');
const asPrice = (value: number | string | null | undefined): number | null => value == null || value === '' || !Number.isFinite(Number(value)) || Number(value) < 0 ? null : Number(value);
const priceLabel = (value?: number | string | null) => asPrice(value) == null ? 'неизвестна' : `${asPrice(value)!.toLocaleString('ru-RU', { maximumFractionDigits: 6 })} ₽`;
const isAudio = (model: ModelInfo) => /audio|speech|stt|transcri/i.test(model.type) || /whisper|transcribe/.test(model.id);
export function SettingsPanel({ config, catalog, busy, onClose, onSave }: Props) {
  const [draft, setDraft] = useState(config);
  const [saved, setSaved] = useState(false);
  const draftRevision = useRef(0);
  const saveInFlight = useRef(false);
  const modal = useRef<HTMLElement>(null);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    modal.current?.querySelector<HTMLButtonElement>('button')?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); onClose(); return; }
      if (event.key !== 'Tab') return;
      const items = Array.from(modal.current?.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled)') ?? []);
      const first = items[0]; const last = items.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener('keydown', onKey);
    return () => { document.removeEventListener('keydown', onKey); previous?.focus(); };
  }, [onClose]);
  const sttModels = catalog?.models.filter(isAudio) ?? [];
  const textModels = catalog?.models.filter((model) => !isAudio(model)) ?? [];
  const changeDraft = (patch: Partial<AppConfig>) => {
    ++draftRevision.current;
    setDraft((previous) => ({ ...previous, ...patch }));
    setSaved(false);
  };
  const update = <K extends keyof AppConfig>(key: K, value: AppConfig[K]) => changeDraft({ [key]: value });
  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy || saveInFlight.current) return;
    saveInFlight.current = true;
    const submittedRevision = draftRevision.current;
    const payload = { ...draft };
    const { key_configured: _keyConfigured, ...editable } = payload;
    void _keyConfigured;
    try {
      if (await onSave(editable) && submittedRevision === draftRevision.current) setSaved(true);
    } finally { saveInFlight.current = false; }
  };
  const chosenStt = sttModels.find((model) => model.id === draft.stt_model);
  const chosenSummary = textModels.find((model) => model.id === draft.summary_model);
  const localCostLimitsEnabled = draft.local_cost_limits_enabled !== false;
  const useCatalogEstimate = () => {
    changeDraft({
      stt_price_rub_per_minute: asPrice(chosenStt?.price_rub_per_minute),
      summary_input_rub_per_million: asPrice(chosenSummary?.input_rub_per_million),
      summary_output_rub_per_million: asPrice(chosenSummary?.output_rub_per_million),
    });
  };
  return (
    <div className="modal-backdrop">
      <section ref={modal} role="dialog" aria-modal="true" aria-labelledby="settings-title" className="settings-modal">
        <header className="flex items-center justify-between gap-3 border-b border-white/10 px-6 py-5">
          <div><p className="eyebrow mb-1">ПОДКЛЮЧЕНИЕ И ОБРАБОТКА</p><h2 id="settings-title" className="text-xl font-semibold">Настройки Secretary</h2></div>
          <Button variant="ghost" className="px-2.5" onClick={onClose} aria-label="Закрыть настройки"><X size={19} /></Button>
        </header>
        <form onSubmit={(event) => { void submit(event); }} className="space-y-6 p-6">
          <Card className={config.key_configured ? 'border-mint/20 bg-mint/5' : 'border-amber-300/20 bg-amber-300/5'}>
            <div className="flex items-center gap-3">
              {config.key_configured ? <CheckCircle2 size={20} className="text-mint" /> : <KeyRound size={20} className="text-amber-200" />}
              <div><p className="font-medium">{config.key_configured ? 'API-ключ настроен на сервере' : 'Для облачной обработки нужен API-ключ'}</p>
                <p className="mt-1 text-xs leading-5 text-slate-400">Запись и локальное сохранение доступны независимо от подключения.</p></div>
            </div>
            <p className="mt-4 text-sm leading-6 text-slate-300">Добавьте <code>POLZA_API_KEY</code> в локальный файл <code>Secretary\.env</code> и перезапустите Secretary. Значение ключа доступно только серверу.</p>
            <a href="https://polza.ai/dashboard/models" target="_blank" rel="noreferrer" className="mt-3 inline-flex items-center gap-2 text-sm text-violet-200 hover:text-white">Каталог Polza <ExternalLink size={13} /></a>
          </Card>
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="field-label">Модель расшифровки
              <select value={draft.stt_model} onChange={(event) => changeDraft({ stt_model: event.target.value, stt_price_rub_per_minute: null })} required>
                {!sttModels.some((model) => model.id === draft.stt_model) && <option value={draft.stt_model}>{draft.stt_model || 'Выберите модель'}</option>}
                {sttModels.map((model) => <option key={model.id} value={model.id}>{model.name ?? model.id}</option>)}
              </select>
              <span className="field-hint">{draft.stt_model || 'Модель пока не выбрана'}</span>
            </label>
            <label className="field-label">Модель итогов
              <select value={draft.summary_model} onChange={(event) => changeDraft({ summary_model: event.target.value, summary_input_rub_per_million: null, summary_output_rub_per_million: null })} required>
                {!textModels.some((model) => model.id === draft.summary_model) && <option value={draft.summary_model}>{draft.summary_model || 'Выберите модель'}</option>}
                {textModels.map((model) => <option key={model.id} value={model.id}>{model.name ?? model.id}</option>)}
              </select>
              <span className="field-hint">{draft.summary_model || 'Модель пока не выбрана'}</span>
            </label>
          </div>
          {chosenStt && <div className="flex flex-wrap gap-2 text-xs"><Badge>Русский: {russianSupport(chosenStt.russian_support)}</Badge><Badge>Таймкоды: {timingSupport(chosenStt.timestamps)}</Badge><Badge>Диаризация: {chosenStt.diarization === true ? 'заявлена, не проверена' : chosenStt.diarization === false ? 'нет' : 'не подтверждена'}</Badge><Badge>Публичный тариф: {priceLabel(chosenStt.price_rub_per_minute)} / мин</Badge></div>}
          <p className="text-xs leading-5 text-slate-500">{catalog ? 'Публичный каталог Polza' : 'Каталог не загружен'}{catalog?.updated_at ? ` · обновлён ${new Date(catalog.updated_at).toLocaleDateString('ru-RU')}` : ''}. Указанные возможности — сведения каталога. Качество на вашей записи и фактические расходы ещё требуют облачного теста. Живая запись обрабатывается сохранёнными фрагментами.</p>
          <div className="border-t border-white/10 pt-5">
            <h3 className="mb-4 flex items-center gap-2 font-medium"><SlidersHorizontal size={17} className="text-violet-300" />Обработка и расходы</h3>
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="field-label">Фрагмент, секунд<input type="number" min="15" max="240" required value={draft.chunk_seconds} onChange={(event) => update('chunk_seconds', Number(event.target.value))} /></label>
              <label className="field-label">Таймаут, секунд<input type="number" min="10" max="1800" required value={draft.request_timeout_seconds} onChange={(event) => update('request_timeout_seconds', Number(event.target.value))} /></label>
            </div>
            <label className="checkbox-label mt-5"><input type="checkbox" checked={draft.cloud_enabled} onChange={(event) => update('cloud_enabled', event.target.checked)} /><span>Разрешить облачную обработку через Polza</span></label>
            <label className="checkbox-label mt-5"><input type="checkbox" checked={localCostLimitsEnabled} onChange={(event) => update('local_cost_limits_enabled', event.target.checked)} /><span>Дополнительные лимиты Secretary</span></label>
            {!localCostLimitsEnabled && <p className="mt-2 text-sm leading-6 text-slate-300">Расходы контролируются лимитами аккаунта и API-ключа в Polza. Secretary не ограничивает тарифы и сумму на встречу; фактические расходы продолжают отображаться. Лимит Polza настраивается в её личном кабинете.</p>}
            {localCostLimitsEnabled && <div className="mt-4">
            <label className="field-label max-w-sm">Лимит на встречу, ₽<input type="number" min="0" max="100000" step="any" required value={draft.meeting_budget_rub} onChange={(event) => update('meeting_budget_rub', Number(event.target.value))} /></label>
            <div className="mt-4 grid gap-4 sm:grid-cols-3">
              <label className="field-label">Максимальный тариф STT, ₽/мин<input type="number" min="0" step="any" placeholder="Не задан" value={draft.stt_price_rub_per_minute ?? ''} onChange={(event) => update('stt_price_rub_per_minute', event.target.value === '' ? null : Number(event.target.value))} /></label>
              <label className="field-label">Максимальный тариф LLM вход, ₽/1M токенов<input type="number" min="0" step="any" placeholder="Не задан" value={draft.summary_input_rub_per_million ?? ''} onChange={(event) => update('summary_input_rub_per_million', event.target.value === '' ? null : Number(event.target.value))} /></label>
              <label className="field-label">Максимальный тариф LLM выход, ₽/1M токенов<input type="number" min="0" step="any" placeholder="Не задан" value={draft.summary_output_rub_per_million ?? ''} onChange={(event) => update('summary_output_rub_per_million', event.target.value === '' ? null : Number(event.target.value))} /></label>
            </div>
            <p className="mt-2 text-xs leading-5 text-slate-400">Эти значения ограничивают допустимый тариф запроса в Polza и используются для резерва расходов. Если доступный тариф выше указанного максимума, Polza отклонит запрос. Повышайте максимум только до согласованной суммы; общий лимит встречи сохраняется.</p>
            <Button variant="ghost" className="mt-3 text-xs" onClick={useCatalogEstimate} disabled={!chosenStt && !chosenSummary}>Взять публичные тарифы</Button>
            <p className="mt-2 text-xs leading-5 text-slate-500">Публичные тарифы служат ориентиром: доступный тариф аккаунта с наценкой может быть выше, и запрос не пройдёт установленный максимум. Кнопка заполняет поля для проверки; примените изменения через «Сохранить настройки». При смене модели прежний максимум сбрасывается. Пустое поле означает неизвестную цену.</p>
            <label className="checkbox-label mt-3"><input type="checkbox" checked={draft.allow_unknown_price} onChange={(event) => update('allow_unknown_price', event.target.checked)} /><span>Разрешить запросы с неизвестной ценой при заданном резерве расходов</span></label>
            <label className="field-label mt-3 max-w-sm">Резерв неизвестной цены за запрос, ₽<input type="number" min="0.000001" max="100000" step="any" placeholder="Не задан" disabled={!draft.allow_unknown_price} value={draft.unknown_request_reservation_rub ?? ''} onChange={(event) => update('unknown_request_reservation_rub', event.target.value === '' ? null : Number(event.target.value))} /></label>
            <p className="mt-2 text-xs leading-5 text-slate-500">Без резерва запросы с неизвестной ценой приостанавливаются. Выберите консервативную оценку: она учитывается до ответа провайдера и не является гарантированной ценой.</p>
            </div>}
          </div>
          <CloudBudgetPanel enabled={config.cloud_enabled && config.key_configured} />
          <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-white/10 pt-5">
            <span role="status" className="text-sm text-mint">{saved ? 'Настройки сохранены' : ''}</span>
            <div className="flex gap-2"><Button variant="ghost" onClick={onClose}>Закрыть</Button><Button type="submit" disabled={busy}>{busy ? 'Сохранение…' : 'Сохранить настройки'}</Button></div>
          </footer>
        </form>
      </section>
    </div>
  );
}
