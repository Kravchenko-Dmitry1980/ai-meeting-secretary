import { useMemo, useState } from 'react';
import { fetchMeetingDetails, fetchMeetingResults, uploadMeeting } from '../services/meetingApi';
import type { FrontendSettings, MeetingResults, MeetingStage } from '../types/meeting';

const FALLBACK_RESULTS: MeetingResults = {
  summary: {
    summary:
      'Demo mode: sprint sync covered onboarding blockers, release timeline, and integration risks.',
    key_decisions: ['Ship beta next Thursday', 'Assign API hardening to platform team'],
    action_recap: ['Prepare release notes', 'Confirm QA checklist by Wednesday'],
  },
  tasks: [
    {
      id: '1',
      title: 'Finalize release checklist',
      assignee: 'Anna',
      priority: 'high',
      due_date: '2026-05-01',
      confidence: 92,
      source_quote: 'Need release readiness checklist by Friday.',
    },
    {
      id: '2',
      title: 'Review speaker diarization edge cases',
      assignee: 'Dmitry',
      priority: 'medium',
      due_date: '2026-05-03',
      confidence: 81,
      source_quote: 'Speaker switch around 14:20 looked noisy.',
    },
  ],
  transcript: [
    { speaker: 'Speaker A', timestamp: '00:01', text: 'Quick status on AI secretary release.' },
    { speaker: 'Speaker B', timestamp: '00:34', text: 'Transcription quality improved by 18%.' },
  ],
  segments: [
    { speaker: 'Speaker A', start: 0, end: 95 },
    { speaker: 'Speaker B', start: 95, end: 210 },
    { speaker: 'Speaker C', start: 210, end: 360 },
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
            setError(details.error ?? 'Processing failed.');
            setIsLoading(false);
          }
        } catch (pollError) {
          window.clearInterval(timer);
          setError(pollError instanceof Error ? pollError.message : 'Polling error');
          setIsLoading(false);
        }
      }, 3000);
    } catch (uploadError) {
      setIsLoading(false);
      if (uploadError instanceof Error && uploadError.message === 'AUTH_ERROR') {
        setError('Auth error: проверьте API key/email.');
        return;
      }
      setError('Backend недоступен. Включен demo mode.');
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
