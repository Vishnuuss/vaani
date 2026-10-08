import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { parseVertical, VERTICAL_LABELS } from '@/lib/verticals';

/**
 * "Is this number already a lead?" for the quick-call form.
 *
 * Read-only. Answers for the CHOSEN business line, and separately lists the other
 * business lines the same number appears in — because those are different leads
 * with their own history, and calling one has nothing to do with the others.
 *
 * The point is that the operator sees "already a Solar lead, called twice, last
 * called 3 days ago" BEFORE dialling, and then decides. It never blocks.
 */

/** Must match the normalisation in /api/calls/single, or the check misses. */
function normalisePhone(raw: unknown): string | null {
  const s = String(raw ?? '').trim();
  if (!s) return null;
  const cleaned = s.replace(/[\s()\-.]/g, '');
  if (!/^\+?\d{7,15}$/.test(cleaned)) return null;
  if (/^\d{10}$/.test(cleaned)) return `+91${cleaned}`;
  return cleaned.startsWith('+') ? cleaned : `+${cleaned}`;
}

export async function GET(request: Request) {
  try {
    const params = new URL(request.url).searchParams;
    const phone = normalisePhone(params.get('phone'));
    const vertical = parseVertical(params.get('vertical'));

    if (!phone) {
      // Not an error — the form calls this while the operator is still typing.
      return NextResponse.json({ found: false, incomplete: true });
    }

    const supabase = createServerClient();
    const { data: rows, error } = await supabase
      .from('leads')
      .select('id, name, phone, vertical, score, status, retry_count, last_attempt_at, created_at')
      .eq('phone', phone);

    if (error) {
      console.error('[leads:lookup] failed', error);
      return NextResponse.json({ error: 'Could not check this number.' }, { status: 500 });
    }

    const all = rows ?? [];
    const match = vertical ? all.find((r) => r.vertical === vertical) : undefined;
    const others = all
      .filter((r) => r.id !== match?.id)
      .map((r) => ({ ...r, line: VERTICAL_LABELS[parseVertical(r.vertical) ?? 'loan'] }));

    return NextResponse.json({
      phone,
      found: !!match,
      lead: match
        ? {
            ...match,
            line: vertical ? VERTICAL_LABELS[vertical] : undefined,
            timesCalled: match.retry_count ?? 0,
          }
        : null,
      // Shown as information only. A person being a loan lead is not a reason to
      // refuse a solar call — they are separate leads by design.
      otherLines: others,
    });
  } catch (error: any) {
    console.error('[leads:lookup] unexpected', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
