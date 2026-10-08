'use client';

import { useEffect, useState } from 'react';
import { Check, Copy, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import {
  Dialog, DialogBody, DialogContent, DialogDescription,
  DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { useTopupInfo, useRequestTopup, type TopupPayload } from '@/hooks/use-credits';

/**
 * Buying credits.
 *
 * Two steps on purpose: choose an amount, then pay and tell us the reference.
 * Submitting does NOT add credits — it files a request that we confirm against
 * the money actually arriving. The copy says so plainly, because a client who
 * expects an instant balance and does not get one will assume it is broken.
 */
export function TopUpDialog({
  open,
  onOpenChange,
  onSubmitted,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmitted?: () => void;
}) {
  const { data, refresh } = useTopupInfo();
  const { requestTopup, isSubmitting } = useRequestTopup();

  const [credits, setCredits] = useState<number>(1000);
  const [custom, setCustom] = useState('');
  const [reference, setReference] = useState('');
  const [step, setStep] = useState<'amount' | 'pay'>('amount');
  const [copied, setCopied] = useState(false);
  const [qr, setQr] = useState<
    { qr_data_url: string; upi_uri: string; amount_inr: number } | null
  >(null);
  const [qrError, setQrError] = useState<string | null>(null);

  // Fetch a QR carrying THIS amount once the client reaches the pay step, so
  // their UPI app opens with the rupee value already filled in.
  useEffect(() => {
    if (step !== 'pay' || credits < 100) return;
    let cancelled = false;
    setQr(null);
    setQrError(null);
    fetch(`/api/credits/topup/qr?credits=${credits}`)
      .then(async (r) => {
        const payload = await r.json().catch(() => ({}));
        if (cancelled) return;
        if (!r.ok) setQrError(payload?.error || 'Could not load the payment code.');
        else setQr(payload);
      })
      .catch(() => { if (!cancelled) setQrError('Could not load the payment code.'); });
    return () => { cancelled = true; };
  }, [step, credits]);

  const presets = data?.presets ?? [500, 1000, 2500, 5000];
  const perMinute = data?.credits_per_minute ?? 4;
  const payment: TopupPayload['payment'] = data?.payment ?? {};
  const minutes = perMinute > 0 ? Math.floor(credits / perMinute) : 0;
  const pending = (data?.requests ?? []).filter((r) => r.status === 'pending');

  // GST is charged on top of the credits: 1000 credits = ₹1000 + 18% = ₹1180
  // payable, and 1000 credits of calling. Mirrors gstOn() on the server; the QR
  // is the authority for the figure actually collected, so once it has loaded
  // we show its amount rather than this one.
  const gstRate = data?.gst_rate_percent ?? 18;
  const gstAmount = Math.round(credits * gstRate) / 100;
  const totalPayable = qr?.amount_inr ?? credits + gstAmount;
  const rs = (n: number) =>
    `₹${n.toLocaleString('en-IN', { minimumFractionDigits: n % 1 === 0 ? 0 : 2, maximumFractionDigits: 2 })}`;

  function choose(value: number) {
    setCredits(value);
    setCustom('');
  }

  async function submit() {
    try {
      await requestTopup(credits, reference.trim() || undefined);
      toast.success('Request sent', {
        description: `We will add ${credits.toLocaleString('en-IN')} credits once the payment is confirmed.`,
      });
      setStep('amount');
      setReference('');
      refresh();
      onSubmitted?.();
      onOpenChange(false);
    } catch (err: any) {
      toast.error('Could not send the request', { description: err?.message });
    }
  }

  async function copyUpi() {
    if (!payment.upi_id) return;
    await navigator.clipboard.writeText(payment.upi_id);
    setCopied(true);
    setTimeout(() => setCopied(false), 1800);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="font-display">Add credits</DialogTitle>
          <DialogDescription>
            {step === 'amount'
              ? `Credits are used as your agent makes calls, at ${perMinute} credits a minute, charged in whole minutes.`
              : 'Pay using the details below, then enter the reference so we can match it.'}
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="space-y-4">
          {step === 'amount' ? (
            <>
              <div className="grid grid-cols-2 gap-2">
                {presets.map((value) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => choose(value)}
                    className={cn(
                      'rounded-lg border px-3 py-3 text-left transition-colors hover-lift',
                      credits === value && !custom
                        ? 'border-primary bg-primary/5'
                        : 'border-border hover:border-input hover:bg-muted/40',
                    )}
                  >
                    <div className="font-display text-lg tabular-nums">
                      {value.toLocaleString('en-IN')}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {rs(value + Math.round(value * gstRate) / 100)} incl GST · ~
                      {Math.floor(value / perMinute)} min
                    </div>
                  </button>
                ))}
              </div>

              <div className="space-y-1.5">
                <label htmlFor="custom-credits" className="text-sm text-muted-foreground">
                  Or enter an amount
                </label>
                <Input
                  id="custom-credits"
                  inputMode="numeric"
                  placeholder="e.g. 3000"
                  value={custom}
                  onChange={(e) => {
                    const raw = e.target.value.replace(/\D/g, '');
                    setCustom(raw);
                    if (raw) setCredits(Number(raw));
                  }}
                />
              </div>

              {/* The full bill, itemised. The client is asked for the gross
                  figure, so showing only the credit value would be quoting an
                  amount that does not match what their UPI app then demands. */}
              <div className="rounded-lg border border-border bg-muted/30 px-3 py-2.5 text-sm">
                <div className="flex items-center justify-between">
                  <span className="text-muted-foreground">
                    {credits.toLocaleString('en-IN')} credits
                  </span>
                  <span className="tabular-nums">{rs(credits)}</span>
                </div>
                <div className="mt-1 flex items-center justify-between">
                  <span className="text-muted-foreground">GST {gstRate}%</span>
                  <span className="tabular-nums">{rs(gstAmount)}</span>
                </div>
                <div className="mt-2 flex items-center justify-between border-t border-border pt-2 font-medium">
                  <span>Total payable</span>
                  <span className="tabular-nums">{rs(credits + gstAmount)}</span>
                </div>
                <p className="mt-2 text-xs text-muted-foreground">
                  You receive {credits.toLocaleString('en-IN')} credits — about{' '}
                  <span className="tabular-nums">{minutes.toLocaleString('en-IN')}</span> minutes of
                  calling. GST is a tax and is not added to your balance.
                </p>
              </div>

              {pending.length > 0 && (
                <p className="text-xs text-muted-foreground">
                  You have {pending.length} request{pending.length === 1 ? '' : 's'} waiting to be
                  confirmed.
                </p>
              )}
            </>
          ) : (
            <>
              {qr ? (
                <div className="flex flex-col items-center gap-2 rounded-lg border border-border bg-white p-4">
                  {/* White plate regardless of theme: a QR inverted by dark mode
                      will not scan. */}
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={qr.qr_data_url}
                    alt={`Scan to pay ${rs(totalPayable)}`}
                    className="size-48 object-contain"
                  />
                  <p className="text-xs font-medium text-neutral-700">
                    Scan to pay {rs(totalPayable)}
                  </p>
                  <p className="text-[11px] text-neutral-500">
                    {credits.toLocaleString('en-IN')} credits + {gstRate}% GST
                  </p>
                </div>
              ) : qrError ? (
                <div className="rounded-lg border border-dashed border-border px-3 py-6 text-center text-sm text-muted-foreground">
                  {qrError}
                </div>
              ) : (
                <div className="flex h-64 items-center justify-center rounded-lg border border-border">
                  <Loader2 className="size-5 animate-spin text-muted-foreground" />
                </div>
              )}

              {/* On a phone the client is already holding the device, so
                  scanning is impossible — this opens their UPI app directly. */}
              {qr && (
                <Button variant="outline" className="w-full sm:hidden" render={<a href={qr.upi_uri} />}>
                  Open UPI app to pay {rs(totalPayable)}
                </Button>
              )}

              {payment.upi_id && (
                <div className="flex items-center justify-between gap-2 rounded-lg border border-border px-3 py-2.5">
                  <div className="min-w-0">
                    <div className="text-xs text-muted-foreground">UPI ID</div>
                    <div className="truncate font-mono text-sm">{payment.upi_id}</div>
                  </div>
                  <Button variant="ghost" size="icon" onClick={copyUpi} aria-label="Copy UPI ID">
                    {copied ? <Check className="text-primary" /> : <Copy />}
                  </Button>
                </div>
              )}

              {payment.bank?.account_number && (
                <div className="rounded-lg border border-border px-3 py-2.5 text-sm">
                  <div className="text-xs text-muted-foreground">Bank transfer</div>
                  <div className="mt-1 space-y-0.5 font-mono text-xs">
                    <div>{payment.bank.account_name}</div>
                    <div>{payment.bank.account_number}</div>
                    <div>{payment.bank.ifsc} · {payment.bank.bank_name}</div>
                  </div>
                </div>
              )}

              <div className="space-y-1.5">
                <label htmlFor="upi-ref" className="text-sm">
                  UPI transaction ID <span className="text-destructive">*</span>
                </label>
                <Input
                  id="upi-ref"
                  placeholder="e.g. 412345678901"
                  value={reference}
                  onChange={(e) => setReference(e.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  {/* Required, not optional: this is the only thing tying the
                      request to a payment that actually arrived. Without it the
                      request cannot be verified and would just be rejected. */}
                  After paying, copy the transaction ID from your UPI app and paste it here. We
                  match it against the payment before adding your credits.
                </p>
              </div>

              <div className="rounded-lg border border-primary/30 bg-primary/5 px-3 py-2.5 text-xs">
                Credits are added once we confirm the payment — usually within a few hours.
              </div>
            </>
          )}
        </DialogBody>

        <DialogFooter className="gap-2">
          {step === 'pay' && (
            <Button variant="ghost" onClick={() => setStep('amount')} disabled={isSubmitting}>
              Back
            </Button>
          )}
          {step === 'amount' ? (
            <Button onClick={() => setStep('pay')} disabled={credits < 100}>
              Continue
            </Button>
          ) : (
            <Button onClick={submit} disabled={isSubmitting || reference.trim().length < 4}>
              {isSubmitting && <Loader2 data-icon="inline-start" className="animate-spin" />}
              {isSubmitting ? 'Sending…' : "I've paid"}
            </Button>
          )}
        </DialogFooter>

        {step === 'amount' && pending.length > 0 && (
          <div className="border-t border-border px-6 py-3">
            <div className="mb-1.5 text-xs font-medium text-muted-foreground">Recent requests</div>
            <div className="space-y-1">
              {(data?.requests ?? []).slice(0, 3).map((r) => (
                <div key={r.id} className="flex items-center justify-between text-xs">
                  <span className="tabular-nums">
                    {r.credits_requested.toLocaleString('en-IN')} credits
                  </span>
                  <Badge variant={r.status === 'approved' ? 'default' : 'outline'}>
                    {r.status}
                  </Badge>
                </div>
              ))}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
