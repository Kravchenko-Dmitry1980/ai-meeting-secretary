import { AnimatePresence, motion } from 'framer-motion';
import { Copy, Settings, UploadCloud, X } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useLocalStorage } from '../hooks/useLocalStorage';
import { useMeetingProcessor } from '../hooks/useMeetingProcessor';
import type { FrontendSettings, MeetingStage, TaskItem } from '../types/meeting';
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
  { key: 'uploaded', label: 'Uploaded' },
  { key: 'preparing', label: 'Preparing audio' },
  { key: 'transcribing', label: 'Transcribing speech' },
  { key: 'detecting_speakers', label: 'Detecting speakers' },
  { key: 'writing_summary', label: 'Writing summary' },
  { key: 'extracting_tasks', label: 'Extracting tasks' },
  { key: 'finalizing', label: 'Finalizing' },
  { key: 'completed', label: 'Completed' },
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
  const [tab, setTab] = useState('Executive Summary');
  const [showSettings, setShowSettings] = useState(false);
  const [priorityFilter, setPriorityFilter] = useState<'all' | 'high' | 'medium' | 'low'>('all');
  const [search, setSearch] = useState('');

  const filteredTasks = useMemo(() => {
    const tasks = results?.tasks ?? [];
    return tasks
      .filter((task) => priorityFilter === 'all' || task.priority === priorityFilter)
      .sort((a, b) => (b.confidence ?? 0) - (a.confidence ?? 0));
  }, [priorityFilter, results?.tasks]);

  const transcript = useMemo(
    () =>
      (results?.transcript ?? []).filter((item) =>
        item.text.toLowerCase().includes(search.toLowerCase()),
      ),
    [results?.transcript, search],
  );

  const activeStepIndex = steps.findIndex((item) => item.key === stage);
  const isCompleted = stage === 'completed' && Boolean(results);

  const onUpload = async () => {
    if (!file) return;
    await start(file);
  };

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

  return (
    <div className="relative overflow-hidden px-4 py-8 text-slate-100 md:px-8">
      <div className="pointer-events-none absolute inset-0 -z-10">
        <motion.div
          animate={{ x: [0, 50, 0], y: [0, -30, 0] }}
          className="absolute -left-28 top-10 h-72 w-72 rounded-full bg-violet-500/25 blur-3xl"
          transition={{ duration: 14, repeat: Infinity }}
        />
      </div>

      <header className="mx-auto mb-6 flex max-w-6xl items-center justify-between">
        <div>
          <h1 className="text-3xl font-semibold md:text-5xl">AI Meeting Secretary</h1>
          <p className="mt-2 text-slate-300">
            Turn meetings into summaries, tasks, and clarity.
          </p>
        </div>
        <Button variant="ghost" onClick={() => setShowSettings(true)}>
          <Settings className="mr-2 inline-block h-4 w-4" />
          Settings
        </Button>
      </header>

      <section className="mx-auto grid max-w-6xl gap-4 md:grid-cols-4">
        {[
          ['Meetings processed', '126'],
          ['Avg processing time', '03:42'],
          ['Tasks extracted', '482'],
          ['Speakers detected', '319'],
        ].map(([label, value]) => (
          <Card key={label}>
            <p className="text-sm text-slate-400">{label}</p>
            <p className="mt-2 text-2xl font-semibold">{value}</p>
          </Card>
        ))}
      </section>

      <section className="mx-auto mt-6 grid max-w-6xl gap-4 lg:grid-cols-[1.2fr_1fr]">
        <Card>
          <h2 className="mb-4 text-lg font-semibold">Upload Workspace</h2>
          <div
            className="rounded-2xl border border-dashed border-violet-300/40 bg-violet-500/5 p-8 text-center transition hover:bg-violet-500/10"
            onDragOver={(event) => event.preventDefault()}
            onDrop={onDrop}
          >
            <UploadCloud className="mx-auto h-8 w-8 text-violet-300" />
            <p className="mt-3 text-sm text-slate-300">Drop wav / mp3 / mp4 / mkv</p>
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
                Upload & Process
              </Button>
              {isDemoMode && <Badge>Demo mode</Badge>}
            </div>
          </div>
        </Card>

        <Card>
          <h2 className="mb-4 text-lg font-semibold">Live Processing</h2>
          {steps.map((step, idx) => (
            <div className="mb-3 flex items-center gap-3" key={step.key}>
              <div
                className={`h-3 w-3 rounded-full ${
                  idx <= activeStepIndex ? 'bg-emerald-400 shadow-glow' : 'bg-white/20'
                }`}
              />
              <span className={idx === activeStepIndex ? 'text-white' : 'text-slate-400'}>
                {step.label}
              </span>
            </div>
          ))}
          <div className="mt-4 h-2 overflow-hidden rounded-full bg-white/10">
            <motion.div
              animate={{ width: `${Math.max(8, ((activeStepIndex + 1) / steps.length) * 100)}%` }}
              className="h-full bg-violet-400"
            />
          </div>
          <div className="mt-3 flex items-center gap-2 text-xs text-slate-300">
            <Badge>{stage}</Badge>
            {meetingId && <span>ID: {meetingId}</span>}
          </div>
          {error && (
            <div className="mt-4 rounded-xl border border-rose-400/30 bg-rose-500/15 p-3 text-sm">
              {error} <Button onClick={retry}>Retry</Button>
            </div>
          )}
        </Card>
      </section>

      <section className="mx-auto mt-6 max-w-6xl">
        <Card>
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-lg font-semibold">Results Workspace</h2>
            {!isCompleted && <Badge>Waiting for completion</Badge>}
          </div>
          <Tabs
            active={tab}
            onChange={setTab}
            tabs={['Executive Summary', 'Action Tasks', 'Full Transcript', 'Speaker Timeline']}
          />

          <AnimatePresence mode="wait">
            {isCompleted && tab === 'Executive Summary' && (
              <motion.div
                animate={{ opacity: 1, y: 0 }}
                className="mt-4 space-y-3"
                exit={{ opacity: 0, y: -8 }}
                initial={{ opacity: 0, y: 8 }}
              >
                <Card className="bg-black/20">
                  <p>{results?.summary.summary ?? ''}</p>
                  <div className="mt-3 flex gap-2">
                    <Button onClick={() => copyText(results?.summary.summary ?? '')}>
                      <Copy className="mr-2 inline h-3 w-3" /> Copy
                    </Button>
                    <Button variant="ghost">Export</Button>
                  </div>
                </Card>
              </motion.div>
            )}

            {isCompleted && tab === 'Action Tasks' && (
              <motion.div className="mt-4" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                <div className="mb-3 flex flex-wrap items-center gap-2">
                  {(['all', 'high', 'medium', 'low'] as const).map((priority) => (
                    <Button
                      key={priority}
                      onClick={() => setPriorityFilter(priority)}
                      variant={priorityFilter === priority ? 'primary' : 'ghost'}
                    >
                      {priority}
                    </Button>
                  ))}
                  <Button onClick={() => copyTasks(filteredTasks)}>Copy all</Button>
                </div>
                <div className="grid gap-3 md:grid-cols-2">
                  {filteredTasks.map((task) => (
                    <Card className="bg-black/20" key={task.id}>
                      <p className="font-semibold">{task.title}</p>
                      <p className="mt-2 text-sm text-slate-300">{task.assignee ?? 'Unassigned'}</p>
                      <div className="mt-2 flex gap-2">
                        <Badge>{task.priority ?? 'n/a'}</Badge>
                        <Badge>{task.confidence ?? 0}% confidence</Badge>
                      </div>
                      <p className="mt-2 text-xs text-slate-400">{task.source_quote}</p>
                    </Card>
                  ))}
                </div>
              </motion.div>
            )}

            {isCompleted && tab === 'Full Transcript' && (
              <motion.div className="mt-4" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                <input
                  className="mb-3 w-full rounded-xl border border-white/10 bg-black/25 p-2 text-sm"
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="Search transcript..."
                  value={search}
                />
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
              </motion.div>
            )}

            {isCompleted && tab === 'Speaker Timeline' && (
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
          </AnimatePresence>
        </Card>
      </section>

      <AnimatePresence>
        {showSettings && (
          <motion.aside
            animate={{ x: 0 }}
            className="fixed right-0 top-0 z-20 h-full w-full max-w-md border-l border-white/10 bg-[#090911]/95 p-5 backdrop-blur-md"
            exit={{ x: 420 }}
            initial={{ x: 420 }}
          >
            <div className="mb-4 flex items-center justify-between">
              <h3 className="text-xl font-semibold">Settings</h3>
              <button onClick={() => setShowSettings(false)} type="button">
                <X />
              </button>
            </div>
            <div className="space-y-3">
              <input
                className="w-full rounded-xl border border-white/15 bg-black/30 p-2 text-sm"
                onChange={(event) => setSettings((prev) => ({ ...prev, apiUrl: event.target.value }))}
                placeholder="API URL"
                value={settings.apiUrl}
              />
              <input
                className="w-full rounded-xl border border-white/15 bg-black/30 p-2 text-sm"
                onChange={(event) => setSettings((prev) => ({ ...prev, apiKey: event.target.value }))}
                placeholder="API Key"
                value={settings.apiKey}
              />
              <input
                className="w-full rounded-xl border border-white/15 bg-black/30 p-2 text-sm"
                onChange={(event) =>
                  setSettings((prev) => ({ ...prev, userEmail: event.target.value }))
                }
                placeholder="Email"
                value={settings.userEmail}
              />
            </div>
          </motion.aside>
        )}
      </AnimatePresence>
    </div>
  );
};
