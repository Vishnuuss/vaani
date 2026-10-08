import { NextResponse } from 'next/server';
import { createServerClient, isMissingTableError } from '@/lib/supabase-server';
import { isAuthorisedOperator, operatorNotFound } from '@/lib/operator-auth';

/**
 * Every feedback submission, plus the averages, for the operator console.
 *
 * Behind the operator key like the rest of /api/operator: the client submits
 * feedback but never reads it back, so a candid answer stays candid.
 */

const MAX_ROWS = 200;

function average(values: (number | null)[]): number | null {
  const nums = values.filter((v): v is number => typeof v === 'number');
  if (!nums.length) return null;
  return Math.round((nums.reduce((a, b) => a + b, 0) / nums.length) * 10) / 10;
}

export async function GET(request: Request) {
  if (!isAuthorisedOperator(request)) return operatorNotFound();

  try {
    const supabase = createServerClient();
    const { data, error } = await supabase
      .from('feedback')
      .select('*')
      .order('created_at', { ascending: false })
      .limit(MAX_ROWS);

    if (error) {
      if (isMissingTableError(error)) {
        return NextResponse.json(
          { error: 'The feedback table is missing. Run scripts/008_trash_and_feedback.sql in Supabase.', submissions: [] },
          { status: 503 },
        );
      }
      console.error('[operator:feedback] query failed', error);
      return NextResponse.json({ error: 'Failed to load feedback.' }, { status: 500 });
    }

    const rows = data ?? [];

    // Net Promoter Score on the 0-10 question: promoters (9-10) minus
    // detractors (0-6), as a percentage. Only meaningful once a few people have
    // answered, so it is returned alongside the count rather than alone.
    const scores = rows.map((r: any) => r.recommend_score).filter((s: any): s is number => typeof s === 'number');
    const nps = scores.length
      ? Math.round(
          ((scores.filter((s) => s >= 9).length - scores.filter((s) => s <= 6).length) / scores.length) * 100,
        )
      : null;

    return NextResponse.json({
      submissions: rows,
      summary: {
        total: rows.length,
        dashboard: average(rows.map((r: any) => r.dashboard_rating)),
        voice: average(rows.map((r: any) => r.voice_rating)),
        understanding: average(rows.map((r: any) => r.understanding_rating)),
        nps,
        npsResponses: scores.length,
        improvedCount: rows.filter((r: any) => r.qualification_change === 'much_better' || r.qualification_change === 'better').length,
      },
    });
  } catch (error: any) {
    console.error('[operator:feedback] unexpected', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
