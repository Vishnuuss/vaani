'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { Loader2, Lock } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

/**
 * The unlock screen.
 *
 * Deliberately anonymous — no branding, no mention of billing or credits, and
 * a wrong key gives the same flat "not found" message as a missing route. There
 * is nothing here to tell a curious visitor what they have found.
 */
export function OperatorUnlock() {
  const router = useRouter();
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!key.trim()) return;
    setBusy(true);
    setFailed(false);
    try {
      const response = await fetch('/api/operator/unlock', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: key.trim() }),
      });
      if (!response.ok) {
        setFailed(true);
        setKey('');
        return;
      }
      router.refresh();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4">
        <div className="flex items-center gap-2 text-muted-foreground">
          <Lock className="size-4" />
          <span className="text-sm">Restricted</span>
        </div>
        <Input
          type="password"
          autoComplete="off"
          placeholder="Access key"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          autoFocus
        />
        {failed && <p className="text-sm text-destructive">Not found.</p>}
        <Button type="submit" disabled={busy || !key.trim()} className="w-full">
          {busy && <Loader2 data-icon="inline-start" className="animate-spin" />}
          Continue
        </Button>
      </form>
    </div>
  );
}
