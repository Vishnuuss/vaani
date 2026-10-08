export type LeadStatus =
  | 'new'
  | 'queued'
  | 'called'
  | 'retry_pending'
  | 'no_answer'
  | 'unreachable';

export interface Lead {
  id: string; // uuid
  name: string | null;
  phone: string;
  email: string | null;
  city: string | null;
  source: string | null;
  /**
   * `retry_pending` / `no_answer` / `unreachable` keep unanswered leads out of the
   * "called" bucket so the retry sweep can still pick them up.
   */
  status: LeadStatus;
  score: number | null;
  qualification: string | null;
  qual_data: Record<string, any> | null;
  call_outcome: string | null;
  property_type: string | null;
  budget: string | null;
  /**
   * The solar agent's two answers (scripts/007_solar_fields.sql). Optional
   * because they are null on every non-solar lead, and absent entirely until
   * that migration has been run - the values also live in qual_data.
   */
  house_ownership?: 'own' | 'rent' | null;
  solar_planning?: boolean | null;
  notes: string | null;
  batch_id: string | null;
  campaign_run_id: string | null;
  retry_count: number;
  recording_url: string | null;
  transcript_url: string | null;
  created_at: string;
  /** Used by the stuck-lead sweep to detect launches that never finished. */
  updated_at?: string | null;
  last_attempt_at: string | null;
  follow_up_date: string | null;
}

export interface CallLog {
  id: string;
  lead_id: string;
  campaign_run_id: string;
  dograh_run_id: number | null;
  attempt_no: number;
  outcome: string | null;
  duration: number | null;
  recording_url: string | null;
  transcript_url: string | null;
  gathered_context: Record<string, any> | null;
  cost_info: Record<string, any> | null;
  called_at: string;
}

export interface CampaignRun {
  id: string;
  campaign_name: string;
  dograh_campaign_id: number | null;
  workflow_id: number;
  source_id: string | null;
  requested_count: number;
  actual_count: number;
  concurrency: number;
  status: string;
  target_segment: Record<string, any> | null;
  retry_config: Record<string, any> | null;
  schedule_config: Record<string, any> | null;
  started_at: string | null;
  completed_at: string | null;
  paused_at: string | null;
  created_at: string;
}

export interface UploadBatch {
  id: string;
  filename: string;
  total_rows: number;
  valid_rows: number;
  duplicate_rows: number;
  rejected_rows: number;
  uploaded_at: string;
}

export interface RejectedLead {
  id: string;
  raw_data: Record<string, any>;
  reason: string;
  batch_id: string | null;
  created_at: string;
}

export interface Setting {
  id: string;
  key: string;
  value: Record<string, any>;
  updated_at: string;
}

export interface DograhCampaignProgress {
  campaign_id: number;
  state: string;
  total_rows: number;
  processed_rows: number;
  failed_calls: number;
  progress_percentage: number;
  source_sync: Record<string, any> | null;
  rate_limit: number;
  started_at: string | null;
  completed_at: string | null;
}

export interface DograhCampaignResponse {
  id: number;
  name: string;
  workflow_id: number;
  workflow_name: string;
  state: string;
  source_type: string;
  source_id: string;
  total_rows: number;
  processed_rows: number;
  failed_rows: number;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  retry_config: Record<string, any> | null;
  max_concurrency: number;
  executed_count: number;
  total_queued_count: number;
  logs: any[];
}

export interface DograhPresignedResponse {
  upload_url: string;
  file_key: string;
  expires_in: number;
}

/**
 * A Dograh run record as the API ACTUALLY returns it, verified 2026-08-06 against
 * `GET /api/v1/campaign/{id}/runs` and `GET /api/v1/workflow/{id}/runs/{runId}`.
 *
 * `phone_number` and `status` were previously declared here as required top-level
 * strings. They are not present at the top level at all - the phone number lives
 * in `initial_context.phone_number`, and the outcome in
 * `gathered_context.call_disposition`. Because the type promised them,
 * lib/reconcile.ts read `run.phone_number` and got undefined on every record,
 * which is why the reconcile sweep matched no leads. Typed optional here so the
 * compiler stops vouching for fields the provider does not send.
 */
export interface DograhRunRecord {
  id: number;
  /** Not sent at the top level - see `initial_context.phone_number`. */
  phone_number?: string | null;
  /** Not sent at the top level - see `gathered_context.call_disposition`. */
  status?: string | null;
  duration?: number | null;
  /** The CSV row the call was dialled from: phone_number, lead_id, campaign_id, ... */
  initial_context: Record<string, any> | null;
  gathered_context: Record<string, any> | null;
  recording_url: string | null;
  transcript_url: string | null;
  created_at: string;
}

export interface WebhookPayload {
  run_id: number;
  campaign_id: number;
  phone: string;
  lead_id: string;
  customer_name: string | null;
  outcome: string;
  interested: boolean | null;
  budget: string | null;
  visit_date: string | null;
  notes: string | null;
  recording: string | null;
  transcript: string | null;
  call_time: string;
}

export interface ApiResponse<T> {
  data?: T;
  error?: string;
  status: number;
}

export interface LeadStats {
  total: number;
  qualified: number;
  not_qualified: number;
  new_leads: number;
  queued: number;
  retry_pending: number;
  called: number;
  site_visits: number;
  follow_ups: number;
  not_interested: number;
}

export interface CampaignStats {
  active_count: number;
  total_targeted: number;
  total_called: number;
  total_qualified: number;
}

/**
 * Mirrors what /api/calls/stats actually returns. It previously listed only
 * four of the ten fields, so every consumer fell back to `any` and lost type
 * safety on the rest.
 *
 * All durations are SECONDS. `total_talk_time` sums only connected calls and is
 * the basis for billed minutes.
 */
export interface CallStats {
  total: number;
  connected: number;
  missed: number;
  failed: number;
  today: number;
  avg_duration: number;
  /** Legacy camelCase alias of avg_duration, still read by older callers. */
  avgDuration: number;
  total_talk_time: number;
  connect_rate: number;
  by_outcome: Record<string, number>;
}
