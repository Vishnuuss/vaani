import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';

export async function GET(request: Request, { params }: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await params;
    const supabase = createServerClient();
    
    const { data: lead, error: leadError } = await supabase
      .from('leads')
      .select('*')
      .eq('id', id)
      .single();

    if (leadError) {
      console.error('Error fetching lead:', leadError);
      return NextResponse.json({ error: leadError.message }, { status: leadError.code === 'PGRST116' ? 404 : 500 });
    }

    const { data: callHistory, error: callsError } = await supabase
      .from('call_logs')
      .select('*')
      .eq('lead_id', id)
      // called_at is when the call happened; created_at is when we recorded it,
      // which for reconciled calls can be hours later and sorts wrongly.
      .order('called_at', { ascending: false });

    if (callsError) {
      console.error('Error fetching call history:', callsError);
      return NextResponse.json({ error: callsError.message }, { status: 500 });
    }

    // The same person can be a lead in several business lines at once — that is
    // the whole point of the (phone, vertical) key. Each of those is a SEPARATE
    // lead with its own score, its own recordings and its own transcripts, and
    // that separation is correct and must not be merged. But this endpoint used
    // to return only the one row's calls, so opening a solar lead gave no hint
    // that the same person had also been called by the loan agent, and the client
    // could not see the two histories side by side.
    //
    // So: return the sibling lines as clearly separate blocks, never pooled.
    let otherLines: any[] = [];
    if (lead?.phone) {
      const { data: siblings, error: sibError } = await supabase
        .from('leads')
        .select('id, vertical, score, status, qualification, recording_url, transcript_url, created_at, last_attempt_at')
        .eq('phone', lead.phone)
        .neq('id', id);

      if (sibError) {
        // Never fail the whole panel over this — the lead itself must still open.
        console.error('[leads:GET] sibling business lines lookup failed', sibError);
      } else if (siblings?.length) {
        const { data: sibCalls, error: sibCallsError } = await supabase
          .from('call_logs')
          .select('*')
          .in('lead_id', siblings.map((s) => s.id))
          .order('called_at', { ascending: false });

        if (sibCallsError) console.error('[leads:GET] sibling call history failed', sibCallsError);

        otherLines = siblings.map((s) => {
          const calls = (sibCalls || []).filter((c) => c.lead_id === s.id);
          return {
            ...s,
            callCount: calls.length,
            // Its own recordings and its own transcripts, kept under its own
            // business line. Nothing here is copied onto the lead being viewed.
            calls,
          };
        });
      }
    }

    return NextResponse.json({
      lead,
      callHistory: callHistory || [],
      otherLines,
    });
  } catch (error: any) {
    console.error('Unexpected error in GET /api/leads/[id]:', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}

/** Only these may be edited from the UI - never score, retry_count or campaign links. */
const EDITABLE_FIELDS = new Set(['status', 'notes', 'follow_up_date', 'qualification', 'city', 'budget', 'name', 'email']);
const EDITABLE_STATUSES = new Set(['new', 'queued', 'called', 'retry_pending', 'no_answer', 'unreachable']);

/**
 * Lets the lead panel save a follow-up date, a note or a status correction.
 * Without this the panel's buttons could only ever fire a toast.
 */
export async function PATCH(request: Request, { params }: { params: Promise<{ id: string }> }) {
  try {
    const { id } = await params;
    const body = await request.json().catch(() => null);

    if (!body || typeof body !== 'object') {
      return NextResponse.json({ error: 'Invalid JSON body' }, { status: 400 });
    }

    const update: Record<string, any> = {};
    for (const [key, value] of Object.entries(body as Record<string, any>)) {
      if (!EDITABLE_FIELDS.has(key)) continue;
      if (key === 'status' && !EDITABLE_STATUSES.has(String(value))) {
        return NextResponse.json({ error: `Invalid status "${value}"` }, { status: 400 });
      }
      if (key === 'follow_up_date' && value) {
        const parsed = new Date(String(value));
        if (Number.isNaN(parsed.getTime())) {
          return NextResponse.json({ error: 'follow_up_date is not a valid date' }, { status: 400 });
        }
        update[key] = parsed.toISOString();
        continue;
      }
      if (key === 'notes') {
        update[key] = String(value ?? '').slice(0, 4000);
        continue;
      }
      update[key] = value === '' ? null : value;
    }

    if (Object.keys(update).length === 0) {
      return NextResponse.json({ error: 'No editable fields supplied' }, { status: 400 });
    }

    const supabase = createServerClient();
    const { data, error } = await supabase.from('leads').update(update).eq('id', id).select().single();

    if (error) {
      console.error('[leads:PATCH] update failed', error);
      return NextResponse.json({ error: 'Failed to update the lead.' }, { status: 500 });
    }

    return NextResponse.json({ lead: data, success: true });
  } catch (error: any) {
    console.error('[leads:PATCH] unexpected', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
