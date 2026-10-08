import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';

/**
 * Returns the transcript for one call log.
 *
 * The browser cannot fetch Dograh's transcript URL directly (no CORS headers on
 * the storage bucket), which is why the lead panel could only ever offer a link
 * that opened in a new tab. This proxies it server-side.
 *
 * SSRF-safe by construction: the URL is never taken from the request. It is read
 * from the call_logs row identified by :id, so a caller can only ever reach a URL
 * we previously stored ourselves.
 */

const FETCH_TIMEOUT_MS = 10_000;
const MAX_BYTES = 1_000_000;

/**
 * Parses Dograh's plain-text transcript.
 *
 * The stored URL 302s to a file served as application/octet-stream, and the
 * body is NOT JSON — it looks like:
 *
 *   [2026-08-02T06:34:49.066+00:00] assistant: నమస్కారం, నేను ...
 *   [2026-08-02T06:34:57.046+00:00] user: ఆ లోన్ ఆ అవసరం ఉందండి.
 *   Hello?
 *   Lo?
 *
 * A line without a timestamp is a continuation of the turn above it — that is
 * how a caller's repeated "Hello?" arrives — so it is appended rather than
 * dropped or treated as a new turn.
 */
function parseTextTranscript(body: string): TranscriptMessage[] | null {
  const LINE = /^\[([^\]]+)\]\s*([A-Za-z_]+)\s*:\s*([\s\S]*)$/;
  const out: TranscriptMessage[] = [];

  for (const rawLine of body.split(/\r?\n/)) {
    const line = rawLine.trimEnd();
    if (!line.trim()) continue;

    const match = line.match(LINE);
    if (match) {
      const [, timestamp, rawSpeaker, text] = match;
      out.push({
        speaker: speakerOf(rawSpeaker),
        text: text.trim(),
        at: timestamp,
      });
    } else if (out.length > 0) {
      out[out.length - 1].text += `\n${line.trim()}`;
    }
  }

  return out.length > 0 ? out : null;
}

interface TranscriptMessage {
  speaker: 'Agent' | 'Customer';
  text: string;
  at?: string;
}

/** Dograh says "assistant"/"user"; everything else is normalised the same way. */
function speakerOf(raw: string): 'Agent' | 'Customer' {
  const s = String(raw).toLowerCase();
  return s.includes('user') || s.includes('customer') || s.includes('human') || s.includes('caller')
    ? 'Customer'
    : 'Agent';
}

/** Normalises the many transcript shapes into [{ speaker, text }]. */
function toMessages(payload: unknown): TranscriptMessage[] | null {
  const rows = Array.isArray(payload)
    ? payload
    : Array.isArray((payload as any)?.messages)
      ? (payload as any).messages
      : Array.isArray((payload as any)?.transcript)
        ? (payload as any).transcript
        : Array.isArray((payload as any)?.turns)
          ? (payload as any).turns
          : null;

  if (!rows) return null;

  return rows
    .map((row: any) => {
      if (typeof row === 'string') return { speaker: 'Agent', text: row };
      const rawSpeaker = String(row?.speaker ?? row?.role ?? row?.source ?? 'agent').toLowerCase();
      const text = row?.text ?? row?.content ?? row?.message ?? row?.transcript ?? '';
      if (!text) return null;
      return { speaker: speakerOf(rawSpeaker), text: String(text) };
    })
    .filter(Boolean) as TranscriptMessage[];
}

export async function GET(_request: Request, { params }: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await params;
    const supabase = createServerClient();

    const { data: call, error } = await supabase
      .from('call_logs')
      .select('id, transcript_url, gathered_context')
      .eq('id', id)
      .maybeSingle();

    if (error) {
      console.error('[calls/transcript] lookup failed', error);
      return NextResponse.json({ error: 'Failed to load the call.' }, { status: 500 });
    }
    if (!call) {
      return NextResponse.json({ error: 'Call not found' }, { status: 404 });
    }
    if (!call.transcript_url) {
      return NextResponse.json({ messages: null, text: null, reason: 'no_transcript' });
    }

    let url: URL;
    try {
      url = new URL(call.transcript_url);
    } catch {
      return NextResponse.json({ error: 'Stored transcript URL is not valid.' }, { status: 422 });
    }
    if (url.protocol !== 'https:' && url.protocol !== 'http:') {
      return NextResponse.json({ error: 'Unsupported transcript URL scheme.' }, { status: 422 });
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);

    try {
      const res = await fetch(url, { signal: controller.signal, cache: 'no-store' });
      if (!res.ok) {
        return NextResponse.json(
          { error: 'The transcript could not be downloaded.', providerStatus: res.status },
          { status: 502 },
        );
      }

      const body = (await res.text()).slice(0, MAX_BYTES);

      let messages: TranscriptMessage[] | null = null;
      try {
        messages = toMessages(JSON.parse(body));
      } catch {
        // Not JSON. Dograh's own transcripts are plain text, so this is the
        // normal path rather than the exception — parse it properly instead of
        // dumping the raw file at the reader.
        messages = parseTextTranscript(body);
      }

      return NextResponse.json({
        messages,
        // Only fall back to raw text when parsing genuinely produced nothing.
        text: messages && messages.length > 0 ? null : body,
      });
    } finally {
      clearTimeout(timer);
    }
  } catch (error: any) {
    if (error?.name === 'AbortError') {
      return NextResponse.json({ error: 'Timed out downloading the transcript.' }, { status: 504 });
    }
    console.error('[calls/transcript] unexpected', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
