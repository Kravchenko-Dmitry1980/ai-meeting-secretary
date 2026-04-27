export type MeetingStage =
  | 'uploaded'
  | 'preparing'
  | 'transcribing'
  | 'detecting_speakers'
  | 'writing_summary'
  | 'extracting_tasks'
  | 'finalizing'
  | 'completed'
  | 'failed';

export interface MeetingDetails {
  id: string;
  status: MeetingStage;
  estimatedSeconds?: number;
  error?: string;
}

export interface MeetingSummaryResponse {
  summary: string;
  key_decisions?: string[];
  action_recap?: string[];
}

export interface TaskItem {
  id: string;
  title: string;
  assignee?: string;
  priority?: 'low' | 'medium' | 'high';
  due_date?: string;
  confidence?: number;
  source_quote?: string;
}

export interface TranscriptItem {
  speaker: string;
  timestamp: string;
  text: string;
}

export interface SegmentItem {
  speaker: string;
  start: number;
  end: number;
}

export interface MeetingResults {
  summary: MeetingSummaryResponse;
  tasks: TaskItem[];
  transcript: TranscriptItem[];
  segments: SegmentItem[];
}

export interface FrontendSettings {
  apiUrl: string;
  apiKey: string;
  userEmail: string;
}
