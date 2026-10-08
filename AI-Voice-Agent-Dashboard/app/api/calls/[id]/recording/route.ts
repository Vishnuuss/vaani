import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';

/**
 * Streams the recording for one call log.
 *
 * The player used to point straight at the provider's URL. Two problems with
 * that, both of which matter now the dashboard is going to a client:
 *
 *  1. Those provider links are PUBLIC. They carry a permanent access token and
 *     need no login, so putting one in the page handed anyone who could read the
 *     HTML — or a copied link, or a browser history export — unrestricted access
 *     to a real customer's phone call. Everything else in this dashboard is
 *     behind the session; the recordings were not.
 *
 *  2. The provider serves them as `application/octet-stream`. Chrome sniffs the
 *     RIFF/WAVE header and plays it anyway, but Safari and iOS are stricter
 *     about media Content-Type and can simply fail with no error shown. The
 *     client would report "recordings don't work" on an iPhone and be right.
 *
 * Proxying fixes both: the URL in the page is now a dashboard URL that the
 * middleware requires a session for, and the response is labelled as audio.
 *
 * SSRF-safe by construction, the same way the transcript route is: the upstream
 * URL is never taken from the request. It is read from the call_logs row named
 * by :id, so a caller can only ever reach a URL we stored ourselves.
 *
 * Range requests are passed through and the 206 is preserved — without that,
 * seeking in the player breaks and some browsers refuse to start at all.
 */

const FETCH_TIMEOUT_MS = 30_000;

export async function GET(request: Request, { params }: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await params;
    const supabase = createServerClient();

    const { data: call, error } = await supabase
      .from('call_logs')
      .select('id, recording_url')
      .eq('id', id)
      .maybeSingle();

    if (error) {
      console.error('[calls/recording] lookup failed', error);
      return NextResponse.json({ error: 'Failed to load the call.' }, { status: 500 });
    }
    if (!call) return NextResponse.json({ error: 'Call not found' }, { status: 404 });
    if (!call.recording_url) {
      return NextResponse.json({ error: 'This call has no recording.' }, { status: 404 });
    }

    let url: URL;
    try {
      url = new URL(call.recording_url);
    } catch {
      return NextResponse.json({ error: 'Stored recording URL is not valid.' }, { status: 422 });
    }
    if (url.protocol !== 'https:' && url.protocol !== 'http:') {
      return NextResponse.json({ error: 'Unsupported recording URL scheme.' }, { status: 422 });
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);

    try {
      // Forward the browser's Range header verbatim. A media element asks for
      // bytes 0- first and then seeks by range; dropping this turns a seekable
      // recording into one that can only be played from the start, if at all.
      const range = request.headers.get('range');
      const res = await fetch(url, {
        signal: controller.signal,
        cache: 'no-store',
        headers: range ? { Range: range } : undefined,
      });

      if (!res.ok && res.status !== 206) {
        return NextResponse.json(
          { error: 'The recording could not be downloaded.', providerStatus: res.status },
          { status: 502 },
        );
      }

      const headers = new Headers();
      // The provider says octet-stream; every recording it produces is a WAV.
      // Trusting its label is what stops the file playing on Safari.
      headers.set('Content-Type', 'audio/wav');
      headers.set('Accept-Ranges', 'bytes');
      for (const h of ['content-length', 'content-range']) {
        const v = res.headers.get(h);
        if (v) headers.set(h, v);
      }
      // A customer's phone call: never cached by a shared proxy, and never
      // stored by the browser beyond the session.
      headers.set('Cache-Control', 'private, no-store');
      headers.set('Content-Disposition', 'inline');

      return new Response(res.body, { status: res.status, headers });
    } finally {
      clearTimeout(timer);
    }
  } catch (err: any) {
    if (err?.name === 'AbortError') {
      return NextResponse.json({ error: 'The recording took too long to load.' }, { status: 504 });
    }
    console.error('[calls/recording] failed', err);
    return NextResponse.json({ error: 'Could not load the recording.' }, { status: 500 });
  }
}
