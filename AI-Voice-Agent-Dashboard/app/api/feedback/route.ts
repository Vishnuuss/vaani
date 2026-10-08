import { NextResponse } from 'next/server';
import { createServerClient, isMissingTableError } from '@/lib/supabase-server';
import { currentUserEmail } from '@/lib/session';
import { FeedbackValidationError, validateFeedback } from '@/lib/feedback';

/**
 * Submit feedback. Write-only from the client's side.
 *
 * There is deliberately no GET here. Past submissions are read from
 * /api/operator/feedback, behind the operator key — the client should be able to
 * say the agent sounds robotic without wondering whether their last three
 * complaints are sitting on a screen they share with their own staff.
 */
export async function POST(request: Request) {
  try {
    const submission = validateFeedback(await request.json().catch(() => null));
    const supabase = createServerClient();

    const { error } = await supabase.from('feedback').insert({
      ...submission,
      submitted_by: await currentUserEmail(),
    });

    if (error) {
      if (isMissingTableError(error)) {
        return NextResponse.json(
          { error: 'The feedback table is missing. Run scripts/008_trash_and_feedback.sql in Supabase.' },
          { status: 503 },
        );
      }
      console.error('[feedback:POST] insert failed', error);
      return NextResponse.json({ error: 'Could not save your feedback. Please try again.' }, { status: 500 });
    }

    return NextResponse.json({ success: true });
  } catch (error: any) {
    if (error instanceof FeedbackValidationError) {
      return NextResponse.json({ error: error.message }, { status: 400 });
    }
    console.error('[feedback:POST] unexpected', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
