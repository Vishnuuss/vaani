import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';
import { applyVerticalFilter, verticalFromParam } from '@/lib/verticals';

export async function GET(request: Request) {
  try {
    const supabase = createServerClient();

    const vertical = verticalFromParam(new URL(request.url).searchParams.get('vertical'));
    const { data: leads, error } = await applyVerticalFilter(
      supabase.from('leads').select('*'),
      vertical,
    ).order('created_at', { ascending: false });

    if (error) {
      return NextResponse.json({ error: error.message }, { status: 500 });
    }

    if (!leads || leads.length === 0) {
      return NextResponse.json({ message: 'No data to export' }, { status: 404 });
    }

    // loan_type and budget are what makes an exported lead actionable for the
    // loan officer - a name and a score alone do not say what to call about.
    // house_ownership / solar_planning are the same thing for a solar lead: the
    // executive picking up the file needs to see "own house, planning solar".
    // currently_investing / investment_type / advisor_interest are the investing
    // agent's answers - an investing lead exported with only a loan_type column
    // is a name and a number the advisor cannot act on.
    // score_reason travels with the score so an exported list is auditable.
    const headers = [
      'id', 'name', 'phone', 'email', 'city', 'vertical', 'status', 'qualification', 'score',
      'loan_type', 'house_ownership', 'solar_planning', 'currently_investing', 'investment_type',
      'advisor_interest', 'budget', 'score_reason', 'created_at', 'call_outcome',
    ];
    const csvRows = [];
    csvRows.push(headers.join(','));

    for (const lead of leads) {
      const row = [
        lead.id,
        `"${(lead.name || '').replace(/"/g, '""')}"`,
        lead.phone,
        lead.email || '',
        `"${(lead.city || '').replace(/"/g, '""')}"`,
        // An export of "All" mixes four business lines, so each row has to say
        // which one it is or the file cannot be split up again afterwards.
        lead.vertical || '',
        lead.status,
        lead.qualification || '',
        lead.score || 0,
        `"${String(lead.property_type || lead.qual_data?.loan_type || '').replace(/"/g, '""')}"`,
        // Column first, qual_data second: leads scored before 007_solar_fields.sql
        // was run carry the answer only in the jsonb.
        (lead.house_ownership ?? lead.qual_data?.house_ownership) === 'own'
          ? 'own house'
          : (lead.house_ownership ?? lead.qual_data?.house_ownership) === 'rent'
            ? 'rented'
            : '',
        (lead.solar_planning ?? lead.qual_data?.solar_planning) === true
          ? 'yes'
          : (lead.solar_planning ?? lead.qual_data?.solar_planning) === false
            ? 'no'
            : '',
        // Investing has no columns on the leads table: the webhook writes both
        // answers into qual_data, so that is the only place to read them from.
        lead.qual_data?.currently_investing === true
          ? 'yes'
          : lead.qual_data?.currently_investing === false
            ? 'no'
            : '',
        `"${String(lead.qual_data?.investment_type || '').replace(/"/g, '""')}"`,
        lead.qual_data?.interested === true ? 'yes' : lead.qual_data?.interested === false ? 'no' : '',
        `"${String(lead.budget || lead.qual_data?.loan_amount || '').replace(/"/g, '""')}"`,
        `"${String(lead.qual_data?.scoring?.reason || '').replace(/"/g, '""')}"`,
        lead.created_at,
        lead.call_outcome || ''
      ];
      csvRows.push(row.join(','));
    }

    const csvContent = csvRows.join('\n');

    return new NextResponse(csvContent, {
      status: 200,
      headers: {
        'Content-Type': 'text/csv',
        'Content-Disposition': `attachment; filename="leads_export_${Date.now()}.csv"`
      }
    });
  } catch (error: any) {
    console.error('Error GET /api/reports/export:', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
