import type { DograhCampaignProgress, DograhCampaignResponse, DograhPresignedResponse, DograhRunRecord } from '@/types';

/** Error thrown for any non-2xx response from Dograh, carrying the HTTP status. */
export class DograhApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly body: string,
    public readonly endpoint: string,
  ) {
    super(message);
    this.name = 'DograhApiError';
  }

  /** 4xx (except 408/429) means the request itself is wrong - retrying will not help. */
  get isClientError() {
    return this.status >= 400 && this.status < 500 && this.status !== 408 && this.status !== 429;
  }
}

/** Thrown when required Dograh env vars are missing, instead of silently sending an empty key. */
export class DograhConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'DograhConfigError';
  }
}

const RETRYABLE_STATUS = new Set([408, 429, 500, 502, 503, 504]);
const DEFAULT_TIMEOUT_MS = 30_000;
const UPLOAD_TIMEOUT_MS = 120_000;
const MAX_ATTEMPTS = 3;

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export class DograhClient {
  private baseUrl: string;

  constructor() {
    // Trailing slashes would produce `//api/v1/...` and 404 on some gateways.
    this.baseUrl = (process.env.DOGRAH_API_URL || 'https://app.dograh.com').replace(/\/+$/, '');
  }

  /**
   * Resolved lazily rather than in the constructor so that importing this module
   * never throws at build time - only actual API use requires credentials.
   */
  private get apiKey(): string {
    const key = process.env.DOGRAH_API_KEY;
    if (!key) {
      throw new DograhConfigError(
        'DOGRAH_API_KEY is not set. Add it to your environment variables to enable calling features.',
      );
    }
    return key;
  }

  private get headers() {
    return {
      'Content-Type': 'application/json',
      'X-API-Key': this.apiKey,
    };
  }

  /**
   * Single place for fetch: enforces a timeout (otherwise a hung Dograh request
   * would hang the serverless function until the platform kills it) and retries
   * transient failures with exponential backoff.
   */
  private async request<T>(
    path: string,
    init: RequestInit & { timeoutMs?: number; idempotencyKey?: string } = {},
  ): Promise<T> {
    const { timeoutMs = DEFAULT_TIMEOUT_MS, idempotencyKey, ...rest } = init;
    const url = `${this.baseUrl}${path}`;
    let lastError: unknown;

    for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);

      try {
        const res = await fetch(url, {
          ...rest,
          headers: {
            ...this.headers,
            // Lets Dograh de-duplicate if our retry lands after their side succeeded.
            ...(idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {}),
            ...(rest.headers as Record<string, string> | undefined),
          },
          signal: controller.signal,
          cache: 'no-store',
        });

        if (!res.ok) {
          const body = await res.text().catch(() => '');
          const err = new DograhApiError(
            `Dograh ${path} failed: ${res.status} ${body.slice(0, 300)}`,
            res.status,
            body,
            path,
          );
          // Bad request / auth / not found: fail immediately, do not burn retries.
          if (!RETRYABLE_STATUS.has(res.status)) throw err;
          lastError = err;
        } else {
          // 204 and empty bodies are valid for start/pause/resume.
          if (res.status === 204) return undefined as T;
          const text = await res.text();
          return (text ? JSON.parse(text) : undefined) as T;
        }
      } catch (error) {
        if (error instanceof DograhApiError && !RETRYABLE_STATUS.has(error.status)) throw error;
        if (error instanceof DograhConfigError) throw error;
        lastError =
          error instanceof Error && error.name === 'AbortError'
            ? new Error(`Dograh ${path} timed out after ${timeoutMs}ms`)
            : error;
      } finally {
        clearTimeout(timer);
      }

      if (attempt < MAX_ATTEMPTS) {
        await sleep(2 ** (attempt - 1) * 500 + Math.random() * 250);
      }
    }

    throw lastError instanceof Error ? lastError : new Error(`Dograh ${path} failed after ${MAX_ATTEMPTS} attempts`);
  }

  /** True when credentials are configured, so routes can return a clean 503 instead of crashing. */
  static isConfigured(): boolean {
    return Boolean(process.env.DOGRAH_API_KEY);
  }

  async getPresignedUploadUrl(fileName: string, fileSize: number): Promise<DograhPresignedResponse> {
    return this.request<DograhPresignedResponse>('/api/v1/s3/presigned-upload-url', {
      method: 'POST',
      body: JSON.stringify({ file_name: fileName, file_size: fileSize, content_type: 'text/csv' }),
    });
  }

  /** S3 presigned PUT: must NOT carry the Dograh API key or the signature check fails. */
  async uploadCsvToS3(uploadUrl: string, csvContent: string): Promise<void> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), UPLOAD_TIMEOUT_MS);
    try {
      const res = await fetch(uploadUrl, {
        method: 'PUT',
        headers: { 'Content-Type': 'text/csv' },
        body: csvContent,
        signal: controller.signal,
      });
      if (!res.ok) {
        const body = await res.text().catch(() => '');
        throw new DograhApiError(
          `Failed to upload CSV to S3: ${res.status} ${body.slice(0, 300)}`,
          res.status,
          body,
          's3-upload',
        );
      }
    } finally {
      clearTimeout(timer);
    }
  }

  async createCampaign(params: {
    name: string;
    workflow_id: number;
    source_id: string;
    max_concurrency?: number;
    retry_config?: Record<string, any>;
    schedule_config?: Record<string, any>;
    circuit_breaker?: Record<string, any>;
    idempotencyKey?: string;
  }): Promise<DograhCampaignResponse> {
    const { idempotencyKey, ...body } = params;
    return this.request<DograhCampaignResponse>('/api/v1/campaign/create', {
      method: 'POST',
      body: JSON.stringify({ ...body, source_type: 'csv' }),
      idempotencyKey,
    });
  }

  /** Starts a NOT_STARTED campaign. For a paused campaign use resumeCampaign(). */
  async startCampaign(campaignId: number): Promise<void> {
    await this.request<void>(`/api/v1/campaign/${campaignId}/start`, {
      method: 'POST',
      idempotencyKey: `start-${campaignId}`,
    });
  }

  async pauseCampaign(campaignId: number): Promise<void> {
    await this.request<void>(`/api/v1/campaign/${campaignId}/pause`, {
      method: 'POST',
      idempotencyKey: `pause-${campaignId}`,
    });
  }

  /**
   * Resumes a paused campaign via the dedicated endpoint.
   *
   * This previously delegated to startCampaign(), which restarts a campaign from
   * the beginning and re-dials leads that were already called. /resume continues
   * from where the campaign was paused.
   */
  async resumeCampaign(campaignId: number): Promise<void> {
    await this.request<void>(`/api/v1/campaign/${campaignId}/resume`, {
      method: 'POST',
      idempotencyKey: `resume-${campaignId}`,
    });
  }

  async getCampaignProgress(campaignId: number): Promise<DograhCampaignProgress> {
    return this.request<DograhCampaignProgress>(`/api/v1/campaign/${campaignId}/progress`);
  }

  async getCampaignRuns(
    campaignId: number,
    page = 1,
    limit = 50,
  ): Promise<{ runs: DograhRunRecord[]; total: number }> {
    const qs = new URLSearchParams({ page: String(page), limit: String(limit) });
    const data = await this.request<{ runs?: DograhRunRecord[]; total?: number }>(
      `/api/v1/campaign/${campaignId}/runs?${qs}`,
    );
    // Normalise: some responses omit these keys entirely, which broke `.runs.map(...)`.
    return { runs: data?.runs ?? [], total: data?.total ?? 0 };
  }

  async getCampaign(campaignId: number): Promise<DograhCampaignResponse> {
    return this.request<DograhCampaignResponse>(`/api/v1/campaign/${campaignId}`);
  }

  async listCampaigns(): Promise<DograhCampaignResponse[]> {
    // Trailing slash is required: Dograh serves the list at /api/v1/campaign/ and
    // the unslashed path 404s instead of redirecting.
    const data = await this.request<DograhCampaignResponse[] | { campaigns?: DograhCampaignResponse[] }>(
      '/api/v1/campaign/',
    );
    if (Array.isArray(data)) return data;
    return data?.campaigns ?? [];
  }

  /**
   * Adjusts a live campaign's concurrency/retry/circuit-breaker settings.
   * Works on any non-terminal campaign (queued, running, paused) - Dograh
   * rejects completed/failed ones with a 400.
   */
  async updateCampaignSettings(
    campaignId: number,
    settings: {
      max_concurrency?: number;
      retry_config?: Record<string, any>;
      circuit_breaker?: Record<string, any>;
    },
  ): Promise<DograhCampaignResponse> {
    return this.request<DograhCampaignResponse>(`/api/v1/campaign/${campaignId}`, {
      method: 'PATCH',
      body: JSON.stringify(settings),
    });
  }

  /**
   * `/fetch/{id}` (not the masked admin GET) - returns the workflow with real,
   * usable credentials in workflow_configurations, exactly what the calling
   * engine itself reads. Round-tripping this object through updateWorkflow()
   * unchanged (aside from the fields you intentionally edit) keeps every API
   * key intact; Dograh masks keys only on the separate admin-facing GET.
   */
  async getWorkflow(workflowId: number): Promise<any> {
    return this.request<any>(`/api/v1/workflow/fetch/${workflowId}`);
  }

  async updateWorkflow(workflowId: number, workflow_definition: any, workflow_configurations: any): Promise<any> {
    return this.request<any>(`/api/v1/workflow/${workflowId}`, {
      method: 'PUT',
      body: JSON.stringify({ workflow_definition, workflow_configurations }),
    });
  }

  /**
   * A single run, in full.
   *
   * The campaign-runs LIST returns `usage_info: null` even for finished calls;
   * only this per-run fetch populates it with the LLM token counts and TTS
   * character counts that provider cost is computed from. Verified on run 17,
   * fetched both ways a minute apart.
   *
   * Billing itself does not need this — duration is present in the list — so
   * it is used only to enrich the operator margin view, in the background.
   */
  async getWorkflowRun(workflowId: number, runId: number): Promise<any> {
    return this.request<any>(`/api/v1/workflow/${workflowId}/runs/${runId}`);
  }

  /** Promotes the current draft to the live version callers actually get. */
  async publishWorkflow(workflowId: number): Promise<any> {
    return this.request<any>(`/api/v1/workflow/${workflowId}/publish`, {
      method: 'POST',
      idempotencyKey: `publish-${workflowId}-${Date.now()}`,
    });
  }
}

export const dograh = new DograhClient();
