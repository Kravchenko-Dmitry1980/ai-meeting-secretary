import { AnimatePresence, motion } from 'framer-motion';
import { Copy, LoaderCircle, Settings, UploadCloud, X } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { useLocalStorage } from '../hooks/useLocalStorage';
import { useMeetingProcessor } from '../hooks/useMeetingProcessor';
import type { FrontendSettings, MeetingStage, TaskItem } from '../types/meeting';
import { LogoMark } from '../components/LogoMark';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { Card } from '../components/ui/Card';
import { Tabs } from '../components/ui/Tabs';

const DEFAULT_SETTINGS: FrontendSettings = {
  apiUrl: 'http://localhost:8000',
  apiKey: '',
  userEmail: '',
};

const steps: { key: MeetingStage; label: string }[] = [
  { key: 'uploaded', label: 'Файл получен' },
  { key: 'preparing', label: 'Подготовка аудио' },
  { key: 'transcribing', label: 'Распознавание речи' },
  { key: 'detecting_speakers', label: 'Определение спикеров' },
  { key: 'writing_summary', label: 'Формирование итогов' },
  { key: 'extracting_tasks', label: 'Извлечение задач' },
  { key: 'finalizing', label: 'Финальная проверка' },
  { key: 'completed', label: 'Анализ завершен' },
];
const tabs = ['Итоги встречи', 'Задачи', 'Расшифровка', 'Таймлайн спикеров'] as const;
const stageText: Record<MeetingStage, string> = {
  uploaded: 'В очереди',
  preparing: 'В процессе',
  transcribing: 'В процессе',
  detecting_speakers: 'В процессе',
  writing_summary: 'В процессе',
  extracting_tasks: 'В процессе',
  finalizing: 'В процессе',
  completed: 'Завершено',
  failed: 'Ошибка',
};
const priorityText: Record<'all' | 'high' | 'medium' | 'low', string> = {
  all: 'Все',
  high: 'Высокий',
  medium: 'Средний',
  low: 'Низкий',
};
const trustBadges = [
  'Экономит часы ручной работы',
  'Подходит для отделов продаж, HR, руководителей',
  'Запуск без сложного внедрения',
];
const howItWorks = ['Загрузите запись', 'ИИ анализирует', 'Получите итоги и задачи'];
const audiences = ['Продажи', 'HR', 'Руководители', 'Проектные команды', 'Агентства'];
const testimonials = [
  {
    quote:
      'Сократили время подготовки пост-митинговых отчетов почти в 6 раз. Команда фокусируется на клиентах, а не на рутине.',
    author: 'Коммерческий директор',
    company: 'B2B SaaS',
  },
  {
    quote:
      'Руководители получают структурированные итоги уже через несколько минут после встречи. Это ускоряет принятие решений.',
    author: 'Операционный руководитель',
    company: 'Консалтинг',
  },
];
const beforeItems = ['ручные заметки', 'потерянные задачи', 'хаос после встречи'];
const afterItems = ['структурные итоги', 'назначенные задачи', 'контроль исполнения'];
const kpiTargets = [
  { label: 'Совещаний обработано', value: 126, suffix: '' },
  { label: 'Среднее время', value: 3.42, suffix: ' мин' },
  { label: 'Задач извлечено', value: 482, suffix: '' },
  { label: 'Точность анализа', value: 97.2, suffix: '%' },
];

const formatSize = (bytes: number): string => `${(bytes / (1024 * 1024)).toFixed(2)} MB`;

export const MainPage = () => {
  const [settings, setSettings] = useLocalStorage<FrontendSettings>(
    'meeting-secretary-settings',
    DEFAULT_SETTINGS,
  );
  const { stage, results, meetingId, isLoading, error, isDemoMode, start, retry } =
    useMeetingProcessor(settings);
  const [file, setFile] = useState<File | null>(null);
  const [tab, setTab] = useState<(typeof tabs)[number]>('Итоги встречи');
  const [showSettings, setShowSettings] = useState(false);
  const [showLeadModal, setShowLeadModal] = useState(false);
  const [priorityFilter, setPriorityFilter] = useState<'all' | 'high' | 'medium' | 'low'>('all');
  const [search, setSearch] = useState('');
  const [lead, setLead] = useState({ name: '', company: '', email: '' });
  const [kpiValues, setKpiValues] = useState([0, 0, 0, 0]);
  const [processingStartedAt, setProcessingStartedAt] = useState(0);
  const [processingTick, setProcessingTick] = useState(0);

  const filteredTasks = useMemo(() => {
    const tasks = results?.tasks ?? [];
    return tasks
      .filter((task) => priorityFilter === 'all' || task.priority === priorityFilter)
      .sort((a, b) => (b.confidence ?? 0) - (a.confidence ?? 0));
  }, [priorityFilter, results?.tasks]);

  const transcript = useMemo(
    () =>
      (results?.transcript ?? []).filter((item) =>
        (item.text ?? '').toLowerCase().includes(search.toLowerCase()),
      ),
    [results?.transcript, search],
  );

  const activeStepIndex = steps.findIndex((item) => item.key === stage);
  const isCompleted = stage === 'completed' && Boolean(results);
  const summaryText = results?.summary?.summary ?? '';

  useEffect(() => {
    const startedAt = performance.now();
    const duration = 1400;
    let frame = 0;
    const tick = (time: number) => {
      const progress = Math.min(1, (time - startedAt) / duration);
      setKpiValues(kpiTargets.map((kpi) => Number((kpi.value * progress).toFixed(2))));
      if (progress < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, []);

  useEffect(() => {
    if (!isLoading) return;
    const timer = window.setInterval(() => {
      setProcessingTick(Date.now());
    }, 1000);
    return () => window.clearInterval(timer);
  }, [isLoading]);

  const onUpload = async () => {
    if (!file) return;
    setProcessingStartedAt(Date.now());
    setProcessingTick(Date.now());
    await start(file);
  };
  const scrollToUpload = () =>
    document.getElementById('upload-workspace')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  const openLeadModal = () => setShowLeadModal(true);
  const closeLeadModal = () => setShowLeadModal(false);

  const onDrop: React.DragEventHandler<HTMLDivElement> = (event) => {
    event.preventDefault();
    const dropped = event.dataTransfer.files?.[0];
    if (dropped) setFile(dropped);
  };

  const copyText = async (text: string) => navigator.clipboard.writeText(text);
  const copyTasks = async (tasks: TaskItem[]) => {
    const payload = tasks.map((task) => `${task.title} [${task.priority ?? 'n/a'}]`).join('\n');
    await navigator.clipboard.writeText(payload);
  };
  const exportResults = () => {
    if (!results) return;
    const taskLines =
      results.tasks.length > 0
        ? results.tasks.map(
            (task, index) =>
              `${index + 1}. ${task.title}\n   Ответственный: ${task.assignee ?? 'Не назначено'}\n   Приоритет: ${task.priority ?? 'n/a'}\n   Срок: ${task.due_date ?? 'n/a'}`,
          )
        : ['Нет задач'];
    const transcriptLines =
      results.transcript.length > 0
        ? results.transcript.map(
            (line) => `[${line.timestamp || '--:--'}] ${line.speaker || 'Неизвестный'}: ${line.text || ''}`,
          )
        : ['Нет расшифровки'];

    const content = [
      `Meeting ID: ${meetingId ?? 'unknown'}`,
      '',
      '=== SUMMARY ===',
      summaryText || 'Нет итогов',
      '',
      '=== TASKS ===',
      ...taskLines,
      '',
      '=== TRANSCRIPT ===',
      ...transcriptLines,
    ].join('\n');

    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `meeting-${meetingId ?? 'unknown'}.txt`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.URL.revokeObjectURL(url);
  };
  const showLongProcessingHint =
    isLoading &&
    stage !== 'completed' &&
    stage !== 'failed' &&
    processingTick - processingStartedAt > 30000;

  return (
    <div className="relative overflow-hidden px-4 py-8 text-slate-100 md:px-8 md:py-10">
      <div className="sticky top-0 z-30 mx-auto mb-6 flex w-full max-w-6xl items-center justify-between rounded-2xl border border-white/15 bg-[#0e1022]/80 px-4 py-3 backdrop-blur-md">
        <p className="text-sm text-slate-200">Подходит для малого бизнеса и корпоративных команд</p>
        <div className="flex items-center gap-2">
          <Button className="min-w-44" onClick={openLeadModal}>
            Попробовать бесплатно
          </Button>
          <Button className="min-w-40" onClick={openLeadModal} variant="ghost">
            Запросить демо
          </Button>
        </div>
      </div>
      <div className="pointer-events-none absolute inset-0 -z-10">
        <motion.div
          animate={{ x: [0, 50, 0], y: [0, -30, 0] }}
          className="absolute -left-28 top-10 h-72 w-72 rounded-full bg-violet-500/25 blur-3xl"
          transition={{ duration: 14, repeat: Infinity }}
        />
      </div>

      <header className="mx-auto mb-10 flex max-w-6xl items-center justify-between">
        <div className="flex items-center gap-3">
          <LogoMark />
          <div>
            <p className="text-xs uppercase tracking-[0.2em] text-violet-200/80">AI Product Suite</p>
            <h1 className="text-xl font-semibold md:text-2xl">AI Секретарь Совещаний</h1>
          </div>
        </div>
        <Button variant="ghost" onClick={() => setShowSettings(true)}>
          <Settings className="mr-2 inline-block h-4 w-4" />
          Настройки
        </Button>
      </header>

      <motion.section
        className="mx-auto mb-10 max-w-6xl rounded-3xl border border-white/10 bg-white/[0.03] px-6 py-12 shadow-[0_30px_80px_rgba(9,9,20,0.45)] md:px-10"
        initial={{ opacity: 0, y: 14 }}
        transition={{ duration: 0.45 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, amount: 0.3 }}
      >
        <motion.h2
          animate={{ y: [0, -2, 0] }}
          className="max-w-3xl text-4xl font-semibold leading-tight md:text-6xl"
          transition={{ duration: 6, repeat: Infinity }}
        >
          AI Секретарь Совещаний
        </motion.h2>
        <p className="mt-4 max-w-2xl text-base text-slate-300 md:text-lg">
          За 5 минут превращает встречу в итоги, задачи и контроль исполнения.
        </p>
        <div className="mt-6 flex flex-wrap gap-2">
          {trustBadges.map((item) => (
            <Badge className="px-3 py-1.5 text-slate-100" key={item}>
              {item}
            </Badge>
          ))}
        </div>
        <div className="mt-8 flex items-center gap-4">
          <Button className="min-w-56" onClick={scrollToUpload}>
            Попробовать сейчас
          </Button>
          <Button className="min-w-44" onClick={openLeadModal} variant="ghost">
            Запросить демо
          </Button>
          <Badge className="px-3 py-1.5">Готово к коммерческому демо</Badge>
        </div>
      </motion.section>

      <motion.section
        className="mx-auto mb-10 grid max-w-6xl gap-4 md:grid-cols-3"
        initial={{ opacity: 0, y: 14 }}
        transition={{ duration: 0.45 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, amount: 0.3 }}
      >
        {howItWorks.map((item, idx) => (
          <Card className="rounded-3xl border-white/15 bg-white/[0.04] p-6" key={item}>
            <p className="text-xs uppercase tracking-wider text-violet-200/90">Шаг {idx + 1}</p>
            <p className="mt-3 text-xl font-semibold">{item}</p>
          </Card>
        ))}
        <div className="md:col-span-3">
          <Button className="min-w-48" onClick={scrollToUpload}>
            Загрузить запись
          </Button>
        </div>
      </motion.section>

      <motion.section
        className="mx-auto grid max-w-6xl gap-4 md:grid-cols-4"
        initial={{ opacity: 0, y: 14 }}
        transition={{ duration: 0.45 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, amount: 0.2 }}
      >
        {kpiTargets.map((kpi, index) => (
          <Card className="rounded-3xl border-white/15 bg-white/[0.04] p-6 transition hover:-translate-y-1" key={kpi.label}>
            <p className="text-sm uppercase tracking-wider text-slate-400">{kpi.label}</p>
            <p className="mt-3 text-3xl font-semibold">
              {kpi.label === 'Среднее время'
                ? `${Math.max(1, Math.floor(kpiValues[index]))}:${String(
                    Math.round((kpiValues[index] % 1) * 60),
                  ).padStart(2, '0')}`
                : `${kpiValues[index].toFixed(kpi.suffix ? 1 : 0)}${kpi.suffix}`}
            </p>
          </Card>
        ))}
      </motion.section>

      <motion.section
        className="mx-auto mt-10 max-w-6xl"
        initial={{ opacity: 0, y: 14 }}
        transition={{ duration: 0.45 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, amount: 0.3 }}
      >
        <h2 className="mb-4 text-2xl font-semibold">До / После внедрения</h2>
        <div className="grid gap-4 md:grid-cols-2">
          <Card className="rounded-3xl border-rose-300/20 bg-rose-500/10 p-6">
            <p className="text-sm uppercase tracking-widest text-rose-200">До</p>
            <ul className="mt-3 space-y-2 text-slate-200">
              {beforeItems.map((item) => (
                <li key={item}>- {item}</li>
              ))}
            </ul>
          </Card>
          <Card className="rounded-3xl border-emerald-300/20 bg-emerald-500/10 p-6">
            <p className="text-sm uppercase tracking-widest text-emerald-200">После</p>
            <ul className="mt-3 space-y-2 text-slate-100">
              {afterItems.map((item) => (
                <li key={item}>- {item}</li>
              ))}
            </ul>
          </Card>
        </div>
      </motion.section>

      <motion.section
        className="mx-auto mt-10 max-w-6xl"
        initial={{ opacity: 0, y: 14 }}
        transition={{ duration: 0.45 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, amount: 0.3 }}
      >
        <Card className="rounded-3xl border-white/15 bg-gradient-to-r from-violet-500/15 via-fuchsia-500/10 to-emerald-500/15 p-7">
          <p className="text-xs uppercase tracking-[0.2em] text-violet-200/85">ROI</p>
          <p className="mt-3 text-3xl font-semibold md:text-4xl">1 час встречи = 10 минут результата</p>
          <p className="mt-2 text-sm text-slate-300">
            Команда быстрее фиксирует договоренности, задачи и контрольные точки без ручной
            расшифровки.
          </p>
          <Button className="mt-5 min-w-52" onClick={openLeadModal}>
            Запросить демо
          </Button>
        </Card>
      </motion.section>

      <motion.section
        className="mx-auto mt-10 max-w-6xl"
        initial={{ opacity: 0, y: 14 }}
        transition={{ duration: 0.45 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, amount: 0.3 }}
      >
        <Card className="rounded-3xl border-amber-300/20 bg-amber-500/10 p-7">
          <p className="text-xs uppercase tracking-[0.2em] text-amber-200/90">Срочность</p>
          <p className="mt-3 text-2xl font-semibold md:text-3xl">
            Каждая неделя без системы = потерянные часы команды
          </p>
          <p className="mt-2 text-sm text-slate-200">
            Пока задачи и итоги фиксируются вручную, компания теряет скорость решений и деньги.
          </p>
        </Card>
      </motion.section>

      <motion.section
        className="mx-auto mt-10 max-w-6xl"
        initial={{ opacity: 0, y: 14 }}
        transition={{ duration: 0.45 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, amount: 0.3 }}
      >
        <h2 className="mb-4 text-2xl font-semibold">Для кого подходит</h2>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
          {audiences.map((item) => (
            <Card className="rounded-3xl border-white/15 bg-white/[0.04] p-5 text-center" key={item}>
              <p className="text-base font-semibold">{item}</p>
            </Card>
          ))}
        </div>
        <Button className="mt-5 min-w-52" onClick={openLeadModal}>
          Запросить демо
        </Button>
      </motion.section>

      <motion.section
        className="mx-auto mt-10 max-w-6xl"
        initial={{ opacity: 0, y: 14 }}
        transition={{ duration: 0.45 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, amount: 0.3 }}
      >
        <h2 className="mb-4 text-2xl font-semibold">Отзывы клиентов</h2>
        <div className="grid gap-4 md:grid-cols-2">
          {testimonials.map((item) => (
            <Card className="rounded-3xl border-white/15 bg-white/[0.04] p-6" key={item.author}>
              <p className="text-sm leading-6 text-slate-200">{item.quote}</p>
              <p className="mt-4 text-sm font-semibold">{item.author}</p>
              <p className="text-xs text-slate-400">{item.company}</p>
            </Card>
          ))}
        </div>
        <Button className="mt-5 min-w-52" onClick={openLeadModal}>
          Получить консультацию
        </Button>
      </motion.section>

      <section className="mx-auto mt-10 grid max-w-6xl gap-5 lg:grid-cols-[1.2fr_1fr]">
        <Card id="upload-workspace" className="rounded-3xl p-7">
          <h2 className="mb-4 text-lg font-semibold">Загрузка файла</h2>
          <div
            className="rounded-2xl border border-dashed border-violet-300/40 bg-violet-500/5 p-8 text-center transition hover:bg-violet-500/10"
            onDragOver={(event) => event.preventDefault()}
            onDrop={onDrop}
          >
            <UploadCloud className="mx-auto h-8 w-8 text-violet-300" />
            <p className="mt-3 text-sm text-slate-300">
              Загрузите аудио или видео встречи (wav, mp3, mp4, mkv)
            </p>
            <input
              accept=".wav,.mp3,.mp4,.mkv"
              className="mt-4 w-full rounded-lg border border-white/10 bg-black/30 p-2 text-sm"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              type="file"
            />
            {file && (
              <div className="mt-4 flex items-center justify-between rounded-xl bg-black/30 p-3 text-left">
                <div>
                  <p className="text-sm font-medium">{file.name}</p>
                  <p className="text-xs text-slate-400">{formatSize(file.size)}</p>
                </div>
                <button onClick={() => setFile(null)} type="button">
                  <X className="h-4 w-4" />
                </button>
              </div>
            )}
            <div className="mt-4 flex items-center justify-center gap-3">
              <Button disabled={!file || isLoading} onClick={onUpload}>
                {isLoading ? 'Система анализирует запись...' : 'Загрузить запись'}
              </Button>
              <Button onClick={openLeadModal} variant="ghost">
                Получить демо
              </Button>
              {isDemoMode && <Badge>Демо-режим</Badge>}
            </div>
            {!file && (
              <p className="mt-4 text-sm text-slate-400">
                Файл не выбран. Добавьте запись встречи, чтобы получить итоги и список задач.
              </p>
            )}
          </div>
        </Card>

        <Card className="rounded-3xl p-7">
          <h2 className="mb-4 text-lg font-semibold">Обработка в реальном времени</h2>
          <div className="relative mt-1 space-y-4">
            <div className="absolute left-[7px] top-2 h-[92%] w-px bg-white/15" />
            {steps.map((step, idx) => (
              <motion.div
                animate={idx === activeStepIndex ? { scale: [1, 1.02, 1] } : { scale: 1 }}
                className="relative flex items-center gap-4"
                key={step.key}
                transition={{ duration: 1.6, repeat: idx === activeStepIndex ? Infinity : 0 }}
              >
                <div
                  className={`relative z-10 h-4 w-4 rounded-full border ${
                    idx <= activeStepIndex
                      ? 'border-emerald-300 bg-emerald-400 shadow-[0_0_30px_rgba(52,211,153,0.7)]'
                      : 'border-white/30 bg-[#1a1a2a]'
                  }`}
                />
                <span
                  className={`flex items-center gap-2 text-sm ${idx === activeStepIndex ? 'text-white' : 'text-slate-400'}`}
                >
                  {step.label}
                  {idx === activeStepIndex && stage !== 'completed' && stage !== 'failed' && (
                    <LoaderCircle className="h-3.5 w-3.5 animate-spin text-violet-300" />
                  )}
                </span>
              </motion.div>
            ))}
          </div>
          <div className="mt-4 h-2 overflow-hidden rounded-full bg-white/10">
            <motion.div
              animate={{ width: `${Math.max(8, ((activeStepIndex + 1) / steps.length) * 100)}%` }}
              className="h-full bg-gradient-to-r from-violet-400 to-emerald-400"
            />
          </div>
          <div className="mt-3 flex items-center gap-2 text-xs text-slate-300">
            <Badge>{stageText[stage]}</Badge>
            {meetingId && <span>Сессия: {meetingId}</span>}
          </div>
          {showLongProcessingHint && (
            <p className="mt-3 text-sm text-amber-200">
              Идет обработка... это может занять несколько минут.
            </p>
          )}
          {error && (
            <div className="mt-4 rounded-xl border border-rose-400/30 bg-rose-500/15 p-3 text-sm">
              {error} <Button onClick={retry}>Повторить</Button>
            </div>
          )}
        </Card>
      </section>

      <section className="mx-auto mt-10 max-w-6xl">
        <Card className="rounded-3xl p-7">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-lg font-semibold">Результаты</h2>
            {!isCompleted && <Badge>Ожидаем завершения анализа</Badge>}
          </div>
          <Tabs active={tab} onChange={(next) => setTab(next as (typeof tabs)[number])} tabs={[...tabs]} />

          <AnimatePresence mode="wait">
            {isCompleted && tab === 'Итоги встречи' && (
              <motion.div
                animate={{ opacity: 1, y: 0 }}
                className="mt-4 space-y-3"
                exit={{ opacity: 0, y: -8 }}
                initial={{ opacity: 0, y: 8 }}
              >
                <Card className="bg-black/20">
                  <p>{summaryText}</p>
                  <div className="mt-3 flex gap-2">
                    <Button onClick={() => copyText(summaryText)}>
                      <Copy className="mr-2 inline h-3 w-3" /> Скопировать
                    </Button>
                    <Button disabled={!isCompleted} onClick={exportResults} variant="ghost">
                      Экспорт
                    </Button>
                  </div>
                </Card>
              </motion.div>
            )}

            {isCompleted && tab === 'Задачи' && (
              <motion.div className="mt-4" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                <div className="mb-3 flex flex-wrap items-center gap-2">
                  {(['all', 'high', 'medium', 'low'] as const).map((priority) => (
                    <Button
                      key={priority}
                      onClick={() => setPriorityFilter(priority)}
                      variant={priorityFilter === priority ? 'primary' : 'ghost'}
                    >
                      {priorityText[priority]}
                    </Button>
                  ))}
                  <Button onClick={() => copyTasks(filteredTasks)}>Скопировать все</Button>
                </div>
                {filteredTasks.length === 0 ? (
                  <div className="rounded-2xl border border-white/10 bg-black/20 p-5 text-sm text-slate-300">
                    По текущему фильтру задач пока нет.
                  </div>
                ) : (
                  <div className="grid gap-3 md:grid-cols-2">
                  {filteredTasks.map((task) => (
                    <Card className="bg-black/20" key={task.id}>
                      <p className="font-semibold">{task.title}</p>
                      <p className="mt-2 text-sm text-slate-300">{task.assignee ?? 'Не назначено'}</p>
                      <div className="mt-2 flex gap-2">
                        <Badge>{priorityText[(task.priority ?? 'low') as 'high' | 'medium' | 'low']}</Badge>
                        <Badge>{task.confidence ?? 0}% достоверность</Badge>
                      </div>
                      <p className="mt-2 text-xs text-slate-400">{task.source_quote}</p>
                    </Card>
                  ))}
                  </div>
                )}
              </motion.div>
            )}

            {isCompleted && tab === 'Расшифровка' && (
              <motion.div className="mt-4" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                <input
                  className="mb-3 w-full rounded-xl border border-white/10 bg-black/25 p-2 text-sm"
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="Поиск по расшифровке..."
                  value={search}
                />
                {transcript.length === 0 ? (
                  <div className="rounded-2xl border border-white/10 bg-black/20 p-5 text-sm text-slate-300">
                    Ничего не найдено. Уточните формулировку для поиска.
                  </div>
                ) : (
                  <div className="space-y-2">
                  {transcript.map((line, index) => (
                    <div
                      className={`rounded-xl p-3 ${index % 2 === 0 ? 'bg-white/5' : 'bg-white/[0.03]'}`}
                      key={`${line.timestamp}-${line.speaker}`}
                    >
                      <div className="mb-1 flex items-center gap-2 text-xs">
                        <Badge>{line.speaker}</Badge>
                        <span className="text-slate-400">{line.timestamp}</span>
                      </div>
                      <p className="text-sm">{line.text}</p>
                    </div>
                  ))}
                  </div>
                )}
              </motion.div>
            )}

            {isCompleted && tab === 'Таймлайн спикеров' && (
              <motion.div className="mt-4 space-y-3" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                {(results?.segments ?? []).map((segment, index) => {
                  const width = Math.min(100, Math.max(14, (segment.end - segment.start) / 4));
                  return (
                    <div className="rounded-xl bg-black/20 p-3" key={`${segment.speaker}-${index}`}>
                      <p className="mb-2 text-sm">{segment.speaker}</p>
                      <div className="h-2 rounded-full bg-white/10">
                        <div className="h-full rounded-full bg-emerald-400" style={{ width: `${width}%` }} />
                      </div>
                      <p className="mt-2 text-xs text-slate-400">
                        {segment.start}s - {segment.end}s
                      </p>
                    </div>
                  );
                })}
              </motion.div>
            )}
            {!isCompleted && (
              <motion.div className="mt-4 rounded-2xl border border-white/10 bg-black/20 p-5 text-sm text-slate-300">
                Результаты пока недоступны. Система завершает анализ встречи и скоро покажет итоги,
                задачи и расшифровку для вашей команды.
              </motion.div>
            )}
          </AnimatePresence>
        </Card>
      </section>

      <AnimatePresence>
        {showLeadModal && (
          <motion.div
            animate={{ opacity: 1 }}
            className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4"
            exit={{ opacity: 0 }}
            initial={{ opacity: 0 }}
          >
            <motion.div
              animate={{ y: 0, opacity: 1 }}
              className="w-full max-w-xl rounded-3xl border border-white/15 bg-[#0b0d1c] p-6 shadow-[0_30px_80px_rgba(0,0,0,0.45)]"
              initial={{ y: 16, opacity: 0 }}
            >
              <div className="mb-4 flex items-center justify-between">
                <h3 className="text-xl font-semibold">Запрос демо</h3>
                <button onClick={closeLeadModal} type="button">
                  <X />
                </button>
              </div>
              <p className="mb-4 text-sm text-slate-300">
                Оставьте контакты, и мы покажем, как внедрить систему в вашу команду.
              </p>
              <div className="space-y-3">
                <input
                  className="w-full rounded-xl border border-white/15 bg-black/30 p-2 text-sm"
                  onChange={(event) => setLead((prev) => ({ ...prev, name: event.target.value }))}
                  placeholder="Имя"
                  value={lead.name}
                />
                <input
                  className="w-full rounded-xl border border-white/15 bg-black/30 p-2 text-sm"
                  onChange={(event) => setLead((prev) => ({ ...prev, company: event.target.value }))}
                  placeholder="Компания"
                  value={lead.company}
                />
                <input
                  className="w-full rounded-xl border border-white/15 bg-black/30 p-2 text-sm"
                  onChange={(event) => setLead((prev) => ({ ...prev, email: event.target.value }))}
                  placeholder="Email"
                  value={lead.email}
                />
                <Button className="w-full">Получить демо</Button>
              </div>
            </motion.div>
          </motion.div>
        )}
        {showSettings && (
          <motion.aside
            animate={{ x: 0 }}
            className="fixed right-0 top-0 z-20 h-full w-full max-w-md border-l border-white/10 bg-[#090911]/95 p-5 backdrop-blur-md"
            exit={{ x: 420 }}
            initial={{ x: 420 }}
          >
            <div className="mb-4 flex items-center justify-between">
              <h3 className="text-xl font-semibold">Настройки</h3>
              <button onClick={() => setShowSettings(false)} type="button">
                <X />
              </button>
            </div>
            <div className="space-y-3">
              <input
                className="w-full rounded-xl border border-white/15 bg-black/30 p-2 text-sm"
                onChange={(event) => setSettings((prev) => ({ ...prev, apiUrl: event.target.value }))}
                placeholder="URL API"
                value={settings.apiUrl}
              />
              <input
                className="w-full rounded-xl border border-white/15 bg-black/30 p-2 text-sm"
                onChange={(event) => setSettings((prev) => ({ ...prev, apiKey: event.target.value }))}
                placeholder="API-ключ"
                value={settings.apiKey}
              />
              <input
                className="w-full rounded-xl border border-white/15 bg-black/30 p-2 text-sm"
                onChange={(event) =>
                  setSettings((prev) => ({ ...prev, userEmail: event.target.value }))
                }
                placeholder="Рабочий email"
                value={settings.userEmail}
              />
            </div>
          </motion.aside>
        )}
      </AnimatePresence>
    </div>
  );
};
