import { createHmac, timingSafeEqual } from 'crypto';

/**
 * The gate on everything that can create credits or reveal our margin.
 *
 * The client has a login to this dashboard, so hiding the operator screens in
 * the UI proves nothing — he can call the API directly. This check is the real
 * control, and it runs on the server for every operator route.
 *
 * Unlocking sets a signed cookie rather than storing the key in the browser, so
 * the raw OPERATOR_KEY is typed once and never persisted client-side.
 */

export const OPERATOR_COOKIE = 'bsw_op';
const SESSION_HOURS = 12;

function operatorKey(): string | null {
  const key = process.env.OPERATOR_KEY;
  return key && key.length >= 16 ? key : null;
}

/** Constant-time compare, so a wrong key cannot be found a character at a time. */
function safeEqual(a: string, b: string): boolean {
  const bufA = Buffer.from(a);
  const bufB = Buffer.from(b);
  if (bufA.length !== bufB.length) return false;
  return timingSafeEqual(bufA, bufB);
}

/** Is this the operator key? */
export function isOperatorKey(candidate: string | null | undefined): boolean {
  const key = operatorKey();
  if (!key || !candidate) return false;
  return safeEqual(candidate, key);
}

/**
 * A session token: "<expiry-ms>.<signature>".
 *
 * Signed with OPERATOR_KEY, so it cannot be forged without the key, and it
 * carries its own expiry so a leaked cookie stops working on its own.
 */
export function mintOperatorToken(): string | null {
  const key = operatorKey();
  if (!key) return null;
  const expiry = Date.now() + SESSION_HOURS * 3600_000;
  const sig = createHmac('sha256', key).update(String(expiry)).digest('hex');
  return `${expiry}.${sig}`;
}

export function isValidOperatorToken(token: string | null | undefined): boolean {
  const key = operatorKey();
  if (!key || !token) return false;

  const [expiryRaw, sig] = token.split('.');
  if (!expiryRaw || !sig) return false;

  const expiry = Number(expiryRaw);
  if (!Number.isFinite(expiry) || expiry < Date.now()) return false;

  const expected = createHmac('sha256', key).update(expiryRaw).digest('hex');
  return safeEqual(sig, expected);
}

/**
 * Authorise an operator request, by cookie or by a raw key header.
 *
 * Fails CLOSED when OPERATOR_KEY is unset: an unconfigured deployment must not
 * silently expose the margin data, the same way the call webhook refuses to run
 * without its secret.
 */
export function isAuthorisedOperator(request: Request): boolean {
  if (!operatorKey()) return false;

  const header = request.headers.get('x-operator-key');
  if (header && isOperatorKey(header)) return true;

  const cookie = request.headers.get('cookie') ?? '';
  const match = cookie.match(new RegExp(`(?:^|;\\s*)${OPERATOR_COOKIE}=([^;]+)`));
  return match ? isValidOperatorToken(decodeURIComponent(match[1])) : false;
}

/**
 * 404, never 401.
 *
 * A 401 confirms the endpoint exists and is worth attacking. A 404 says there
 * is nothing here at all, which is what we want the client to conclude.
 */
export function operatorNotFound() {
  return new Response(JSON.stringify({ error: 'Not found' }), {
    status: 404,
    headers: { 'content-type': 'application/json' },
  });
}
