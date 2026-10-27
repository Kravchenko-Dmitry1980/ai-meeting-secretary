// Application sidecar. The unchanged provider DTO remains neurohr.local-asr.v2.
export interface LocalAsrSegment {
  ordinal: number;
  speaker_label: string;
  started_ms: number;
  ended_ms: number;
  text: string;
}
export interface LocalAsrTurn {
  ordinal: number;
  speaker_label: string;
  started_ms: number;
  ended_ms: number;
}
export interface LocalAsrV2 {
  schema_version: 'neurohr.local-asr.v2';
  provider: string;
  language: string;
  input_sha256: string;
  transcript_text: string;
  segments: number;
  segment_items: LocalAsrSegment[];
  speaker_turns?: LocalAsrTurn[];
  warnings: string[];
  metrics: Record<string, unknown>;
}
export interface LocalAsrRunSummary {
  run_id: string;
  meeting_id: string;
  transcript_version: number;
  revision: number;
  created_at: string;
  artifact_digest: string;
  review_revision: number;
}
export type LocalAsrSpeakerKind = 'single' | 'unknown' | 'composite';
export interface LocalAsrCluster {
  speaker_label: string;
  speaker_cluster_id: string;
  kind: LocalAsrSpeakerKind;
}
export interface LocalAsrManualReview {
  segment_ordinal: number;
  participant_id: string | null;
  person_id: string | null;
  status: 'confirmed' | 'excluded';
  source: 'manual_cluster' | 'manual_segment';
  speaker_cluster_id: string;
  review_revision: number;
  confirmed_at: string;
}
export interface LocalAsrRun extends LocalAsrRunSummary {
  result: LocalAsrV2;
  artifacts: {
    selected_text: string;
    alignment: { status: 'complete' | 'partial' | 'unavailable'; reason_codes: string[]; tokenizer_compatibility: 'verified' | 'unverified' | 'unsupported'; unsupported_token_indices: number[] };
    source_segments: Array<{ segment_id: string; channel?: string }>;
    segment_sources: Array<{ ordinal: number; source_segment_ids: string[] }>;
    [key: string]: unknown;
  };
  reviews: LocalAsrManualReview[];
  source_decisions: Array<{ source_segment_id: string; participant_id: string | null; status: string; method: string }>;
  clusters: LocalAsrCluster[];
}
export interface LocalAsrClusterPreview {
  run_id: string;
  speaker_label: string;
  speaker_cluster_id: string;
  review_revision: number;
  shown_segment_ordinals: number[];
  eligible_segment_ordinals: number[];
  excluded_segments: Array<{ segment_ordinal: number; reason: string }>;
  samples: Array<{
    segment_ordinal: number;
    started_ms: number;
    ended_ms: number;
    source_segment_ids: string[];
    channel?: string | null;
  }>;
  segments: Array<LocalAsrSegment & { source_segment_ids: string[]; channel?: string | null }>;
}
export interface LocalAsrClusterCommand {
  operation_id: string;
  expected_review_revision: number;
  speaker_label: string;
  shown_segment_ordinals: number[];
  excluded_segment_ordinals: number[];
  participant_id: string;
  listening_confirmed: true;
}
export interface LocalAsrSegmentCommand {
  operation_id: string;
  expected_review_revision: number;
  segment_ordinal: number;
  participant_id: string | null;
  listening_confirmed: true;
}
