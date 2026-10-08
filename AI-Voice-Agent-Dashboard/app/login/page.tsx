'use client';

import { useState, type FormEvent } from 'react';
import { useRouter } from 'next/navigation';
import { Eye, EyeOff, Loader2 } from 'lucide-react';
import { createBrowserClient } from '@/lib/supabase-browser';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { BsWealthLockup } from '@/components/brand/bs-wealth-mark';

/**
 * Sign in.
 *
 * There is deliberately NO sign-up link. This dashboard exposes a client's
 * entire lead list and call recordings, so anyone able to register themselves
 * would be able to read all of it. Logins are created by us in the operator
 * console and handed over.
 *
 * Previously this page was hard-coded slate and blue with a generic title,
 * sharing none of the design language of the app behind it — the first screen
 * a client sees should not look like a different product.
 */
export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const handleLogin = async (e: FormEvent) => {
    e.preventDefault();
    setIsLoading(true);
    setError(null);

    try {
      // Built lazily, on submit. Creating it during render threw "Missing env
      // var NEXT_PUBLIC_SUPABASE_URL" while Next prerendered this page, which
      // failed the production build whenever env vars were absent at build time.
      const supabase = createBrowserClient();

      const { data, error: signInError } = await supabase.auth.signInWithPassword({
        email: email.trim(),
        password,
      });

      if (signInError) {
        // Supabase says "Invalid login credentials" for both a wrong password
        // and an unknown account — deliberately, so the form cannot be used to
        // discover which addresses have logins. Kept, with plainer wording.
        setError(
          /invalid login/i.test(signInError.message)
            ? 'That email and password do not match.'
            : signInError.message,
        );
        return;
      }

      if (data.session) {
        router.push('/');
        router.refresh(); // let the middleware pick up the new session cookie
      }
    } catch (err: any) {
      setError(err?.message || 'Something went wrong. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <main className="ambient-wash relative flex min-h-screen items-center justify-center overflow-hidden px-4 py-12">
      <div className="motion-rise relative z-10 w-full max-w-sm">
        <div className="mb-8 flex justify-center">
          <BsWealthLockup size="lg" />
        </div>

        <div className="rounded-2xl border border-border/70 bg-card p-6 shadow-paper sm:p-8">
          <h1 className="font-display text-2xl font-semibold tracking-tight">Sign in</h1>
          <p className="mt-1.5 text-sm text-muted-foreground">
            Lead operations for BS Wealth Finance.
          </p>

          <form onSubmit={handleLogin} className="mt-6 space-y-4">
            <div className="space-y-1.5">
              <label htmlFor="email" className="text-sm font-medium">
                Email
              </label>
              <Input
                id="email"
                type="email"
                autoComplete="username"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@company.com"
                disabled={isLoading}
              />
            </div>

            <div className="space-y-1.5">
              <label htmlFor="password" className="text-sm font-medium">
                Password
              </label>
              <div className="relative">
                <Input
                  id="password"
                  type={showPassword ? 'text' : 'password'}
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  disabled={isLoading}
                  className="pr-10"
                />
                {/* Passwords here are long generated strings that get typed by
                    hand at least once, so being able to see them matters. */}
                <button
                  type="button"
                  onClick={() => setShowPassword((v) => !v)}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground transition-colors hover:text-foreground"
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                  tabIndex={-1}
                >
                  {showPassword ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                </button>
              </div>
            </div>

            {error && (
              <p
                role="alert"
                className="rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
              >
                {error}
              </p>
            )}

            <Button type="submit" disabled={isLoading} className="w-full">
              {isLoading && <Loader2 data-icon="inline-start" className="animate-spin" />}
              {isLoading ? 'Signing in…' : 'Sign in'}
            </Button>
          </form>

          <p className="mt-6 border-t border-border/70 pt-4 text-xs leading-relaxed text-muted-foreground">
            Accounts are set up for you. If you cannot get in or need access for
            someone else, contact BS Financial Services.
          </p>
        </div>
      </div>
    </main>
  );
}
