import { NextResponse } from 'next/server';
import { createServerClient } from '@/lib/supabase-server';

export async function GET() {
  try {
    const supabase = createServerClient();
    
    const { data: settings, error } = await supabase
      .from('settings')
      .select('key, value');

    if (error) {
      return NextResponse.json({ error: error.message }, { status: 500 });
    }

    const settingsMap = (settings || []).reduce((acc: Record<string, any>, curr) => {
      acc[curr.key] = curr.value;
      return acc;
    }, {});

    return NextResponse.json({ settings: settingsMap });
  } catch (error: any) {
    console.error('Error GET /api/settings:', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}

/** Whitelisted so a caller cannot fill the table with arbitrary keys. */
// `ai_agent` (not `agent`) is the key already present in the settings table.
const ALLOWED_KEYS = new Set([
  'workspace',
  'ai_agent',
  'notifications',
  'appearance',
  'call_behavior',
]);

async function upsertSetting(request: Request) {
  try {
    const body = await request.json().catch(() => null);
    const { key, value } = (body ?? {}) as { key?: string; value?: unknown };

    if (!key || typeof key !== 'string') {
      return NextResponse.json({ error: 'Key is required' }, { status: 400 });
    }
    if (!ALLOWED_KEYS.has(key)) {
      return NextResponse.json(
        { error: `Unknown setting "${key}". Allowed: ${[...ALLOWED_KEYS].join(', ')}` },
        { status: 400 },
      );
    }

    const supabase = createServerClient();

    const { data, error } = await supabase
      .from('settings')
      .upsert({ key, value }, { onConflict: 'key' })
      .select()
      .single();

    if (error) {
      console.error('[settings] upsert failed', error);
      return NextResponse.json({ error: 'Failed to save the setting.' }, { status: 500 });
    }

    return NextResponse.json({ setting: data, success: true });
  } catch (error: any) {
    console.error('[settings] unexpected', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}

// The settings hook POSTs; the original route only exported PUT, so every "Save"
// button in the UI got a 405 and the toast lied about having saved.
export const PUT = upsertSetting;
export const POST = upsertSetting;
