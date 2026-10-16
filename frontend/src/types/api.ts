import type { components } from '../generated/api';
// Domain and response entities generated from the backend OpenAPI document.
// npm run api:types regenerates these types; a backend change is checked by tsc.
export type Meeting = components['schemas']['Meeting'];
export type ProcessingJob = components['schemas']['ProcessingJob'];
export type JobStage = ProcessingJob['stage'];
export type JobStatus = ProcessingJob['status'];
export type AudioChunk = components['schemas']['AudioChunk'];
export type TranscriptSegment = components['schemas']['TranscriptSegment'];
export type SegmentPage = components['schemas']['SegmentsPage'];
export type EvidenceItem = components['schemas']['EvidenceItem'];
export type ActionItem = components['schemas']['ActionItem'];
export type Summary = components['schemas']['Summary'];
export type Speaker = components['schemas']['Speaker'];
export type PendingSummary = components['schemas']['StatusResponse'];
export type ProcessingMode = Meeting['processing_mode'];
export type MeetingParticipant = components['schemas']['MeetingParticipant'];
export type ParticipantRoster = components['schemas']['ParticipantRosterSnapshot'];
export type AttributionSnapshot = components['schemas']['AttributionSnapshot'];
export type SpeakerAttribution = components['schemas']['SpeakerAttribution'];
export type AttributionChange = components['schemas']['AttributionChange'];
export type IdentificationState = components['schemas']['IdentificationState'];
export type PersonProfile = components['schemas']['PersonProfile'];
export type AppConfig = components['schemas']['ConfigurationResponse'];
export interface ModelInfo {
  id: string; name?: string; type: string; russian_support?: string | boolean;
  timestamps?: string | boolean; diarization?: string | boolean; live_ingress?: string | boolean;
  status?: string; price_rub_per_minute?: number | string | null;
  input_rub_per_million?: number | string | null; output_rub_per_million?: number | string | null;
}
export interface ModelCatalog { models: ModelInfo[]; source: string; updated_at: string | null }
export interface AudioDevice { id: string; name: string; kind: 'microphone' | 'system'; default: boolean }
export interface DeviceList { available: boolean; devices: AudioDevice[]; error: string | null }
export interface RecordingState {
  status: string; recording?: boolean; active?: boolean; saved_chunks?: number;
  started_at?: string; duration_ms?: number; error?: string | null;
}
export type UsageRecord = components['schemas']['UsageRecord'];
export type Usage = components['schemas']['UsageResponse'];
export interface EventSnapshot { meeting: Meeting; jobs: ProcessingJob[]; segment_count: number }
export type CloudBudgetOperation = components['schemas']['CloudBudgetOperationView'];
export type CloudBudgetReconciliationEvent = components['schemas']['CloudBudgetReconciliationEventView'];
export type CloudBudgetOperationPage = components['schemas']['CloudBudgetOperationPage'];
export type CloudBudgetOperationDetails = components['schemas']['CloudBudgetOperationDetails'];
