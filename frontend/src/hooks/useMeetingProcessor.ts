import { useMemo, useState } from 'react';
import { fetchMeetingDetails, fetchMeetingResults, uploadMeeting } from '../services/meetingApi';
import type { FrontendSettings, MeetingResults, MeetingStage } from '../types/meeting';

const FALLBACK_RESULTS: MeetingResults = {
  summary: {
    summary:
      'Демо-режим: команда обсудила риски релиза, точки роста и приоритеты по внедрению.',
    key_decisions: ['Запуск пилота на следующей неделе', 'Усилить контроль качества интеграции API'],
    action_recap: ['Подготовить релизные материалы', 'Согласовать чек-лист с QA до среды'],
  },
  tasks: [
    {
      id: '1',
      title: 'Финализировать чек-лист релиза',
      assignee: 'Анна',
      priority: 'high',
      due_date: '2026-05-01',
      confidence: 92,
      source_quote: 'Нужен финальный чек-лист готовности к релизу до пятницы.',
    },
    {
      id: '2',
      title: 'Проверить сложные кейсы разделения спикеров',
      assignee: 'Дмитрий',
      priority: 'medium',
      due_date: '2026-05-03',
      confidence: 81,
      source_quote: 'Переключение спикеров около 14:20 выглядит нестабильно.',
    },
  ],
  transcript: [
    { speaker: 'Спикер A', timestamp: '00:01', text: 'Коротко сверяем статус релиза AI-секретаря.' },
    { speaker: 'Спикер B', timestamp: '00:34', text: 'Качество расшифровки выросло на 18%.' },
  ],
  segments: [
    { speaker: 'Спикер A', start: 0, end: 95 },
    { speaker: 'Спикер B', start: 95, end: 210 },
    { speaker: 'Спикер C', start: 210, end: 360 },
  ],
};

export const useMeetingProcessor = (settings: FrontendSettings) => {
  const [meetingId, setMeetingId] = useState<string | null>(null);
  const [stage, setStage] = useState<MeetingStage>('uploaded');
  const [results, setResults] = useState<MeetingResults | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isDemoMode, setIsDemoMode] = useState(false);

  const canRequest = useMemo(
    () => Boolean(settings.apiUrl && settings.apiKey && settings.userEmail),
    [settings],
  );

  const runDemoFlow = () => {
    setIsDemoMode(true);
    setMeetingId(`demo-${Date.now()}`);
    const steps: MeetingStage[] = [
      'uploaded',
      'preparing',
      'transcribing',
      'detecting_speakers',
      'writing_summary',
      'extracting_tasks',
      'finalizing',
      'completed',
    ];
    steps.forEach((nextStage, index) => {
      window.setTimeout(() => {
        setStage(nextStage);
        if (nextStage === 'completed') {
          setResults(FALLBACK_RESULTS);
          setIsLoading(false);
        }
      }, index * 900);
    });
  };

  const start = async (file: File) => {
    setError(null);
    setResults(null);
    setIsLoading(true);
    setStage('uploaded');
    if (!canRequest) {
      runDemoFlow();
      return;
    }

    try {
      const uploaded = await uploadMeeting(file, settings);
      setMeetingId(uploaded.meeting_id);
      setIsDemoMode(false);

      const timer = window.setInterval(async () => {
        try {
          const details = await fetchMeetingDetails(uploaded.meeting_id, settings);
          setStage(details.status);
          if (details.status === 'completed') {
            window.clearInterval(timer);
            const payload = await fetchMeetingResults(uploaded.meeting_id, settings);
            setResults(payload);
            setIsLoading(false);
          }
          if (details.status === 'failed') {
            window.clearInterval(timer);
            setError(details.error ?? 'Во время обработки произошла ошибка.');
            setIsLoading(false);
          }
        } catch (pollError) {
          window.clearInterval(timer);
          setError(
            pollError instanceof Error ? pollError.message : 'Не удалось получить статус обработки.',
          );
          setIsLoading(false);
        }
      }, 3000);
    } catch (uploadError) {
      setIsLoading(false);
      if (uploadError instanceof Error && uploadError.message === 'AUTH_ERROR') {
        setError('Ошибка авторизации: проверьте API-ключ и email.');
        return;
      }
      setError('Сервер недоступен. Включен демо-режим.');
      runDemoFlow();
    }
  };

  const retry = () => {
    setError(null);
    setStage('uploaded');
    setResults(null);
  };

  return { meetingId, stage, results, isLoading, error, isDemoMode, start, retry };
};
