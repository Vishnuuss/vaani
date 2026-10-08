import { NextResponse } from 'next/server';
import { randomInt } from 'crypto';
import { createServerClient } from '@/lib/supabase-server';
import { isAuthorisedOperator, operatorNotFound } from '@/lib/operator-auth';

export const dynamic = 'force-dynamic';

/**
 * Dashboard logins. OPERATOR ONLY.
 *
 * A private client dashboard must NOT have public sign-up: anyone who found the
 * URL could register and read the client's entire lead list. Accounts are
 * created here instead, by us, and handed over.
 *
 * Accounts live in Supabase Auth on the main project — no custom user table.
 * Supabase stores a bcrypt hash, so a password cannot be read back by anyone,
 * including from this endpoint. Lost passwords are reset, never retrieved.
 */

/** Unambiguous alphabet — no O/0, l/1/I — because these get typed by hand. */
function generatePassword(length = 16): string {
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
  let out = '';
  for (let i = 0; i < length; i += 1) out += alphabet[randomInt(alphabet.length)];
  return out;
}

function isEmail(value: unknown): value is string {
  return typeof value === 'string' && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim());
}

export async function GET(request: Request) {
  if (!isAuthorisedOperator(request)) return operatorNotFound();

  try {
    const supabase = createServerClient();
    const { data, error } = await supabase.auth.admin.listUsers({ page: 1, perPage: 100 });
    if (error) throw new Error(error.message);

    return NextResponse.json({
      users: (data?.users ?? []).map((u) => ({
        id: u.id,
        email: u.email,
        name: (u.user_metadata as any)?.name ?? null,
        role: (u.user_metadata as any)?.role ?? 'user',
        confirmed: Boolean(u.email_confirmed_at),
        created_at: u.created_at,
        last_sign_in_at: u.last_sign_in_at,
      })),
    });
  } catch (err: any) {
    console.error('[operator/users] GET failed', err?.message);
    return NextResponse.json({ error: 'Could not load users.' }, { status: 500 });
  }
}

/** Create a login. Returns the password ONCE — it can never be read again. */
export async function POST(request: Request) {
  if (!isAuthorisedOperator(request)) return operatorNotFound();

  try {
    const body = await request.json().catch(() => ({}));
    const email = String(body?.email ?? '').trim().toLowerCase();
    const name = typeof body?.name === 'string' ? body.name.trim().slice(0, 80) : null;

    if (!isEmail(email)) {
      return NextResponse.json({ error: 'Enter a valid email address.' }, { status: 400 });
    }

    const password = generatePassword();
    const supabase = createServerClient();

    const { data, error } = await supabase.auth.admin.createUser({
      email,
      password,
      // Confirmed on creation. Supabase's built-in mailer allows only a handful
      // of messages an hour and is not for production, so relying on a
      // verification email to arrive would strand the account.
      email_confirm: true,
      user_metadata: { role: 'user', ...(name ? { name } : {}) },
    });

    if (error) {
      const already = /already|registered|exists/i.test(error.message);
      return NextResponse.json(
        { error: already ? 'That email already has a login.' : error.message },
        { status: already ? 409 : 500 },
      );
    }

    return NextResponse.json({
      success: true,
      user: { id: data.user?.id, email: data.user?.email },
      // Shown once. We cannot retrieve it later — only overwrite it.
      password,
    }, { status: 201 });
  } catch (err: any) {
    console.error('[operator/users] POST failed', err?.message);
    return NextResponse.json({ error: 'Could not create the login.' }, { status: 500 });
  }
}

/** Set a new password for an existing login. */
export async function PATCH(request: Request) {
  if (!isAuthorisedOperator(request)) return operatorNotFound();

  try {
    const body = await request.json().catch(() => ({}));
    const id = String(body?.id ?? '');
    if (!id) return NextResponse.json({ error: 'Missing user id.' }, { status: 400 });

    const password = generatePassword();
    const supabase = createServerClient();
    const { error } = await supabase.auth.admin.updateUserById(id, { password });
    if (error) throw new Error(error.message);

    return NextResponse.json({ success: true, password });
  } catch (err: any) {
    console.error('[operator/users] PATCH failed', err?.message);
    return NextResponse.json({ error: 'Could not reset the password.' }, { status: 500 });
  }
}

/** Remove a login entirely. */
export async function DELETE(request: Request) {
  if (!isAuthorisedOperator(request)) return operatorNotFound();

  try {
    const id = new URL(request.url).searchParams.get('id');
    if (!id) return NextResponse.json({ error: 'Missing user id.' }, { status: 400 });

    const supabase = createServerClient();
    const { error } = await supabase.auth.admin.deleteUser(id);
    if (error) throw new Error(error.message);

    return NextResponse.json({ success: true });
  } catch (err: any) {
    console.error('[operator/users] DELETE failed', err?.message);
    return NextResponse.json({ error: 'Could not remove the login.' }, { status: 500 });
  }
}
