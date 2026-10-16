import type { AppConfig } from '../types/api';

const editableKeys = [
  'stt_model', 'summary_model', 'chunk_seconds', 'request_timeout_seconds',
  'meeting_budget_rub', 'local_cost_limits_enabled', 'allow_unknown_price',
  'unknown_request_reservation_rub', 'cloud_enabled', 'stt_price_rub_per_minute',
  'summary_input_rub_per_million', 'summary_output_rub_per_million',
  'summary_max_output_tokens', 'summary_batch_chars',
] as const satisfies readonly (keyof AppConfig)[];

export type EditableConfig = Pick<AppConfig, typeof editableKeys[number]>;
export type ConfigChanges = Partial<EditableConfig>;

// Public responses include connection facts and upload limits. They never belong
// in PATCH, even when a caller passes a whole response or a later server adds data.
export function editableConfig(values: ConfigChanges): ConfigChanges {
  return Object.fromEntries(editableKeys.filter((key) => Object.hasOwn(values, key))
    .map((key) => [key, values[key]]));
}

export function configChanges(before: AppConfig, after: AppConfig): ConfigChanges {
  return Object.fromEntries(editableKeys.filter((key) => !Object.is(before[key], after[key]))
    .map((key) => [key, after[key]]));
}
