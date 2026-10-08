import { NextResponse } from 'next/server';
import { isOperatorKey, mintOperatorToken, OPERATOR_COOKIE, operatorNotFound } from '@/lib/operator-auth';

export const dynamic = 'force-dynamic';

/**
 * Exchange the operator key for a short-lived signed cookie.
 *
 * The key itself is typed once and never stored in the browser — only the
 * signed token is, and it expires on its own.
 */
export async function POST(request: Request) {
  if (!process.env.OPERATOR_KEY) return operatorNotFound();

  const body = await request.json().catch(() => ({}));
  const key = typeof body?.key === 'string' ? body.key : '';

  if (!isOperatorKey(key)) {
    // Deliberately slow and vague. No hint about whether the route exists, and
    // a small delay makes guessing the key impractical.
    await new Promise((r) => setTimeout(r, 700));
    return operatorNotFound();
  }

  const token = mintOperatorToken();
  if (!token) return operatorNotFound();

  const response = NextResponse.json({ success: true });
  response.cookies.set(OPERATOR_COOKIE, token, {
    httpOnly: true,          // unreadable from JavaScript, so XSS cannot lift it
    secure: process.env.NODE_ENV === 'production',
    sameSite: 'strict',      // never sent on a cross-site request
    path: '/',
    maxAge: 12 * 3600,
  });
  return response;
}

/** Sign out of the operator console. */
export async function DELETE() {
  const response = NextResponse.json({ success: true });
  response.cookies.set(OPERATOR_COOKIE, '', { path: '/', maxAge: 0 });
  return response;
}
