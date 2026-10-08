import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { verticalLabel } from '@/lib/verticals';

/**
 * Final counts for one import.
 *
 * n8n answers an upload with `202 accepted` and a batch id, because it parses and
 * inserts in the background — so the upload response itself can never carry the
 * real numbers. Nothing polled for them, which meant the operator was told
 * "File uploaded" and nothing else, whatever actually happened. A file where every
 * row was skipped looked exactly like a file that imported perfectly. That is how
 * a client spent a morning re-uploading the same three files believing they were
 * "not importing" (2026-08-11).
 *
 * Read-only.
 */
export async function GET(request: Request) {
  try {
    const batchId = new URL(request.url).searchParams.get('batchId');
    if (!batchId) {
      return NextResponse.json({ error: 'batchId is required' }, { status: 400 });
    }

    const supabase = createServerClient();
    const { data, error } = await supabase
      .from('upload_batches')
      .select('id, filename, vertical, total_rows, valid_rows, duplicate_rows, rejected_rows, uploaded_at')
      .eq('id', batchId)
      .single();

    if (error) {
      // PGRST116 = no row yet. n8n is still working; not an error, just not ready.
      if (error.code === 'PGRST116') {
        return NextResponse.json({ ready: false });
      }
      console.error('[leads:upload:status] lookup failed', error);
      return NextResponse.json({ error: 'Could not read the import status.' }, { status: 500 });
    }

    const total = data.total_rows ?? 0;
    const valid = data.valid_rows ?? 0;
    const duplicate = data.duplicate_rows ?? 0;
    const rejected = data.rejected_rows ?? 0;
    const line = verticalLabel(data.vertical);

    // Said in the operator's words, and it always names the business line. A
    // duplicate here means "already in THIS business line" — the same person in a
    // different business line is a separate lead and imports normally.
    const parts = [`${valid} added to ${line}`];
    if (duplicate > 0) parts.push(`${duplicate} already in ${line}`);
    if (rejected > 0) parts.push(`${rejected} with no usable phone number`);

    return NextResponse.json({
      ready: true,
      batch: { ...data, total, valid, duplicate, rejected, line },
      // True when the file did nothing at all — the case that used to be silent.
      nothingImported: total > 0 && valid === 0,
      message: parts.join(', '),
    });
  } catch (error: any) {
    console.error('[leads:upload:status] unexpected', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
