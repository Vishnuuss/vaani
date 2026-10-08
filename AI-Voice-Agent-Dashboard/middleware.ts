import { NextResponse } from 'next/server'
import type { NextRequest } from 'next/server'
import { createServerClient, type CookieOptions } from '@supabase/ssr'

export async function middleware(request: NextRequest) {
  // SKIP_AUTH disables authentication entirely: every lead record and every
  // campaign control becomes readable and operable by anyone with the URL.
  //
  // It is deliberately still honoured in production while the dashboard is being
  // brought up, at the owner's explicit request. Each request logs a warning and
  // /api/health reports it, so this cannot be forgotten about quietly.
  // TODO: remove SKIP_AUTH from the deployment once a login user exists.
  if (process.env.SKIP_AUTH === 'true') {
    if (process.env.NODE_ENV === 'production') {
      console.warn('[middleware] SKIP_AUTH=true — this deployment is PUBLIC. Remove it to require login.')
    }
    return NextResponse.next()
  }

  const { pathname } = request.nextUrl

  // Public paths that don't require authentication.
  // These are exact/prefix matches: `pathname.includes('/favicon.ico')` used to
  // make any URL containing that substring public.
  const isPublicPath =
    pathname === '/login' ||
    // Authenticated by its own shared secret, and the provider cannot log in.
    pathname.startsWith('/api/webhook/') ||
    // Authenticated by CRON_SECRET (Bearer or x-cron-secret).
    pathname.startsWith('/api/cron/') ||
    // The operator console authenticates itself with OPERATOR_KEY, checked in
    // the page and in every /api/operator route via isAuthorisedOperator().
    //
    // It is handled there rather than here on purpose: SKIP_AUTH returns early
    // at the top of this file, so a middleware-only gate would leave the
    // console wide open for as long as that flag is set. It is also the wrong
    // runtime — middleware runs on the Edge, where node:crypto HMAC is not
    // available. Unauthorised requests get 404, never 401.
    pathname.startsWith('/operator') ||
    pathname.startsWith('/api/operator/') ||
    pathname.startsWith('/_next/') ||
    pathname === '/favicon.ico' ||
    /\.(png|jpg|jpeg|gif|svg|webp|ico)$/.test(pathname)

  if (isPublicPath) {
    return NextResponse.next()
  }

  // If Supabase is not configured, creating the client throws and Next renders a
  // 500 with a stack trace on every single page. Fail closed with a clean answer
  // instead: nobody gets in, and nothing internal leaks.
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL
  const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY
  if (!supabaseUrl || !supabaseAnonKey) {
    console.error('[middleware] Supabase env vars are missing; denying all authenticated traffic.')
    if (pathname.startsWith('/api/')) {
      return NextResponse.json({ error: 'Authentication is not configured' }, { status: 503 })
    }
    return NextResponse.redirect(new URL('/login', request.url))
  }

  let response = NextResponse.next({
    request: {
      headers: request.headers,
    },
  })

  // Create an unauthenticated supabase client to check session
  const supabase = createServerClient(
    supabaseUrl,
    supabaseAnonKey,
    {
      cookies: {
        get(name: string) {
          return request.cookies.get(name)?.value
        },
        set(name: string, value: string, options: CookieOptions) {
          request.cookies.set({
            name,
            value,
            ...options,
          })
          response = NextResponse.next({
            request: {
              headers: request.headers,
            },
          })
          response.cookies.set({
            name,
            value,
            ...options,
          })
        },
        remove(name: string, options: CookieOptions) {
          request.cookies.set({
            name,
            value: '',
            ...options,
          })
          response = NextResponse.next({
            request: {
              headers: request.headers,
            },
          })
          response.cookies.set({
            name,
            value: '',
            ...options,
          })
        },
      },
    }
  )

  // getUser() revalidates the token against Supabase. getSession() only decodes
  // whatever cookie the browser sent, which a forged cookie can satisfy — so the
  // previous check could be bypassed without a real session.
  const { data: { user } } = await supabase.auth.getUser()

  // Redirect to login if not authenticated
  if (!user) {
    // If it's an API request, return 401
    if (pathname.startsWith('/api/')) {
      return NextResponse.json({ error: 'Unauthorized' }, { status: 401 })
    }
    
    // Otherwise redirect to login
    return NextResponse.redirect(new URL('/login', request.url))
  }

  // Baseline hardening for authenticated pages: no framing (clickjacking), no MIME
  // sniffing, and no full URL leaked to third parties via the referrer.
  response.headers.set('X-Frame-Options', 'DENY')
  response.headers.set('X-Content-Type-Options', 'nosniff')
  response.headers.set('Referrer-Policy', 'strict-origin-when-cross-origin')
  response.headers.set('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')

  return response
}

export const config = {
  matcher: [
    /*
     * Match all request paths except for the ones starting with:
     * - _next/static (static files)
     * - _next/image (image optimization files)
     * - favicon.ico (favicon file)
     * Feel free to modify this pattern to include more paths.
     */
    '/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)',
  ],
}
