import { NextResponse } from 'next/server';
import { DEFAULT_VERTICAL, parseVertical, VERTICALS } from '@/lib/verticals';

/**
 * Lead ingestion goes through n8n (not straight to Supabase from here) — n8n's
 * workflow does the real parsing: CSV/XLSX/XLS, fuzzy header detection, 500-row
 * batching for large files, and writes to upload_batches/leads/rejected_leads
 * itself. This route is a thin proxy so the browser only ever talks to our own
 * domain (n8n's webhook URL / any auth in front of it stays server-side).
 */
const FALLBACK_IMPORT_WEBHOOK_URL = 'https://agent.bswealthfinance.com/webhook/lead-uploaded';

export async function POST(request: Request) {
  try {
    const contentType = request.headers.get('content-type') || '';
    if (!contentType.includes('multipart/form-data')) {
      return NextResponse.json({ error: 'Expected multipart/form-data with a file field' }, { status: 400 });
    }

    // Forward the raw multipart body untouched (same bytes, same boundary) instead
    // of re-parsing into FormData and rebuilding it — reconstructing a File/Blob
    // from an already-parsed request body hit Node's "Body has already been read"
    // bug. Passing the raw bytes straight through sidesteps that entirely.
    const bodyBuffer = await request.arrayBuffer();

    // The business line travels as a QUERY PARAMETER, not a form field.
    //
    // On a multipart upload n8n moves the file into $binary and leaves
    // json.body as {} - confirmed on a real execution of the ingestion
    // workflow - so a `vertical` form field could not be relied on to arrive.
    // json.query is always populated, and it shows up in n8n's execution log,
    // which makes a mis-tagged upload diagnosable after the fact instead of
    // invisible. The raw body still passes through untouched.
    const requested = new URL(request.url).searchParams.get('vertical');
    const vertical = parseVertical(requested) ?? DEFAULT_VERTICAL;
    if (requested && !parseVertical(requested)) {
      // Guessing here would tag a whole file as the wrong business line and
      // hand it to the wrong agent. Refuse instead.
      return NextResponse.json(
        { error: `Unknown business line "${requested}". Expected one of: ${VERTICALS.join(', ')}.` },
        { status: 400 },
      );
    }

    const baseUrl = process.env.N8N_IMPORT_WEBHOOK_URL || FALLBACK_IMPORT_WEBHOOK_URL;
    const webhookUrl = `${baseUrl}${baseUrl.includes('?') ? '&' : '?'}vertical=${encodeURIComponent(vertical)}`;

    const response = await fetch(webhookUrl, {
      method: 'POST',
      headers: { 'content-type': contentType },
      body: bodyBuffer,
    });

    let data: unknown = null;
    try {
      data = await response.json();
    } catch {
      data = { message: await response.text() };
    }

    if (!response.ok) {
      console.error('n8n lead-upload webhook failed', response.status, data);
      return NextResponse.json(
        { error: 'Lead ingestion service rejected the file.', n8nStatus: response.status, data },
        { status: 502 },
      );
    }

    return NextResponse.json(data);
  } catch (error: any) {
    console.error('Unexpected error in POST /api/leads/upload:', error);
    return NextResponse.json(
      { error: error.message || 'Internal Server Error' },
      { status: 500 }
    );
  }
}
