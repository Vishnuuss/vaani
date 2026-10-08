'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle, CheckCircle2, Loader2, RefreshCw, ShieldCheck, TrendingDown, TrendingUp,
} from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { cn } from '@/lib/utils';

/** Every operator fetch goes through here so a 404 always means "locked out". */
async function opFetch(path: string, init?: RequestInit) {
  const response = await fetch(path, init);
  if (response.status === 404) {
    window.location.reload();   // session expired — bounce back to the unlock screen
    throw new Error('Session expired');
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload?.error || 'Request failed');
  return payload;
}

const rs = (n: number) => `₹${n.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;

export function OperatorConsole() {
  const [tab, setTab] = useState('money');
  const [topups, setTopups] = useState<any[]>([]);
  const [account, setAccount] = useState<any>(null);
  const [economics, setEconomics] = useState<any>(null);
  const [integrity, setIntegrity] = useState<any>(null);
  const [users, setUsers] = useState<any[]>([]);
  const [feedback, setFeedback] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [t, s, e, i, u] = await Promise.all([
        opFetch('/api/operator/topups?status=all'),
        opFetch('/api/operator/settings'),
        opFetch('/api/operator/economics?days=90'),
        opFetch('/api/operator/integrity'),
        opFetch('/api/operator/users'),
      ]);
      setTopups(t.requests ?? []);
      setAccount(s.account ?? null);
      setEconomics(e);
      setIntegrity(i);
      setUsers(u.users ?? []);

      // Deliberately outside the Promise.all above: feedback is the newest table
      // and the only one that can be missing on a deployment where migration 008
      // has not been run yet. Failing it inside the batch would take the whole
      // console — money, margin, logins — down with it.
      try {
        setFeedback(await opFetch('/api/operator/feedback'));
      } catch {
        setFeedback(null);
      }
    } catch (err: any) {
      toast.error('Could not load', { description: err?.message });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function decide(id: string, action: 'approve' | 'reject') {
    setBusyId(id);
    try {
      const result = await opFetch('/api/operator/topups', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, action }),
      });
      toast.success(
        action === 'approve'
          ? `Added ${result.credited?.toLocaleString('en-IN')} credits`
          : 'Request rejected',
      );
      await load();
    } catch (err: any) {
      toast.error('Failed', { description: err?.message });
    } finally {
      setBusyId(null);
    }
  }

  const pending = topups.filter((t) => t.status === 'pending');
  const balance = account ? (account.balance_milli_credits ?? 0) / 1000 : 0;

  return (
    <div className="mx-auto max-w-6xl px-4 py-8 md:px-8">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-3xl font-semibold tracking-tight">Operations</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {account?.display_name ?? 'Account'} · balance{' '}
            <span className="font-medium tabular-nums text-foreground">
              {balance.toLocaleString('en-IN', { maximumFractionDigits: 0 })} credits
            </span>
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="icon" onClick={load} aria-label="Refresh">
            <RefreshCw className={cn(loading && 'animate-spin')} />
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={async () => {
              await fetch('/api/operator/unlock', { method: 'DELETE' });
              window.location.reload();
            }}
          >
            Lock
          </Button>
        </div>
      </header>

      {integrity && (
        <div
          className={cn(
            'mb-6 flex items-center gap-2 rounded-lg border px-3 py-2.5 text-sm',
            integrity.healthy
              ? 'border-border bg-muted/30 text-muted-foreground'
              : 'border-destructive bg-destructive/10 text-destructive',
          )}
        >
          {integrity.healthy ? <ShieldCheck className="size-4" /> : <AlertTriangle className="size-4" />}
          {integrity.healthy ? (
            <span>
              Ledger intact · {integrity.chain.entries_checked} entries verified · balance matches
              {integrity.metering.calls_awaiting_billing > 0 &&
                ` · ${integrity.metering.calls_awaiting_billing} calls awaiting billing`}
            </span>
          ) : (
            <span>
              {!integrity.balance.drift_ok && `Balance drift of ${integrity.balance.drift_credits} credits. `}
              {!integrity.chain.intact && 'Ledger hash chain is broken — someone edited the database directly.'}
            </span>
          )}
        </div>
      )}

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="money">
            Money in{pending.length > 0 ? ` (${pending.length})` : ''}
          </TabsTrigger>
          <TabsTrigger value="margin">Margin</TabsTrigger>
          <TabsTrigger value="settings">Pricing</TabsTrigger>
          <TabsTrigger value="users">Logins</TabsTrigger>
          <TabsTrigger value="feedback">
            Feedback{feedback?.summary?.total ? ` (${feedback.summary.total})` : ''}
          </TabsTrigger>
        </TabsList>
      </Tabs>

      <div className="mt-6 space-y-6">
        {tab === 'money' && (
          <>
            <Card>
              <CardHeader>
                <CardTitle>Top-up requests</CardTitle>
                <CardDescription>
                  Approving is the only way credits are created. Confirm the money arrived first.
                </CardDescription>
              </CardHeader>
              <CardContent>
                {topups.length === 0 ? (
                  <p className="py-8 text-center text-sm text-muted-foreground">No requests yet.</p>
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Requested</TableHead>
                        <TableHead>Credits</TableHead>
                        <TableHead>Reference</TableHead>
                        <TableHead>Status</TableHead>
                        <TableHead className="text-right">Action</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {topups.map((t) => (
                        <TableRow key={t.id}>
                          <TableCell className="whitespace-nowrap text-muted-foreground">
                            {new Date(t.requested_at).toLocaleString('en-IN', {
                              day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit',
                            })}
                          </TableCell>
                          <TableCell className="font-medium tabular-nums">
                            {t.credits_requested.toLocaleString('en-IN')}
                            {/* amount_inr is the GROSS the client was asked for.
                                Match the payment against THIS, not the credit
                                value — GST makes the two differ. */}
                            <span className="ml-1 text-xs text-muted-foreground">
                              ({rs(Number(t.amount_inr))})
                            </span>
                            {Number(t.gst_amount_inr) > 0 && (
                              <div className="text-[11px] font-normal text-muted-foreground">
                                {rs(Number(t.base_amount_inr))} + {Number(t.gst_rate_percent)}% GST{' '}
                                {rs(Number(t.gst_amount_inr))}
                              </div>
                            )}
                          </TableCell>
                          <TableCell className="max-w-40 truncate font-mono text-xs">
                            {t.reference_note || '—'}
                          </TableCell>
                          <TableCell>
                            <Badge variant={t.status === 'approved' ? 'default' : 'outline'}>
                              {t.status}
                            </Badge>
                          </TableCell>
                          <TableCell className="text-right">
                            {t.status === 'pending' ? (
                              <div className="flex justify-end gap-2">
                                <Button
                                  size="sm"
                                  variant="outline"
                                  disabled={busyId === t.id}
                                  onClick={() => decide(t.id, 'reject')}
                                >
                                  Reject
                                </Button>
                                <Button
                                  size="sm"
                                  disabled={busyId === t.id}
                                  onClick={() => decide(t.id, 'approve')}
                                >
                                  {busyId === t.id && <Loader2 data-icon="inline-start" className="animate-spin" />}
                                  Approve
                                </Button>
                              </div>
                            ) : (
                              <span className="text-xs text-muted-foreground">
                                {t.decided_at
                                  ? new Date(t.decided_at).toLocaleDateString('en-IN')
                                  : '—'}
                              </span>
                            )}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>

            <ManualAdjust onDone={load} />
          </>
        )}

        {tab === 'margin' && economics && <MarginPanel economics={economics} />}

        {tab === 'settings' && account && <PricingPanel account={account} onSaved={load} />}

        {tab === 'users' && <UsersPanel users={users} onChanged={load} />}
        {tab === 'feedback' && <FeedbackPanel data={feedback} />}
      </div>
    </div>
  );
}

/** Add or remove credits by hand. Always on the books, always with a reason. */
function ManualAdjust({ onDone }: { onDone: () => void }) {
  const [credits, setCredits] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    try {
      const result = await opFetch('/api/operator/adjust', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ credits: Number(credits), reason }),
      });
      toast.success(`Balance is now ${result.balance_credits.toLocaleString('en-IN')} credits`);
      setCredits('');
      setReason('');
      onDone();
    } catch (err: any) {
      toast.error('Failed', { description: err?.message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Manual adjustment</CardTitle>
        <CardDescription>
          Use a negative number to remove credits. This appears on the client&apos;s statement.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-wrap items-end gap-3">
        <div className="w-32 space-y-1.5">
          <label htmlFor="adj-credits" className="text-xs text-muted-foreground">Credits</label>
          <Input
            id="adj-credits"
            value={credits}
            onChange={(e) => setCredits(e.target.value.replace(/[^\d-]/g, ''))}
            placeholder="1000"
          />
        </div>
        <div className="min-w-52 flex-1 space-y-1.5">
          <label htmlFor="adj-reason" className="text-xs text-muted-foreground">Reason (required)</label>
          <Input
            id="adj-reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Opening balance / goodwill / correction"
          />
        </div>
        <Button onClick={submit} disabled={busy || !credits || reason.trim().length < 3}>
          {busy && <Loader2 data-icon="inline-start" className="animate-spin" />}
          Post
        </Button>
      </CardContent>
    </Card>
  );
}

/**
 * Where our money actually goes. OPERATOR ONLY.
 *
 * Written to answer one question in plain language: of what the client paid,
 * how much left our pocket and to whom. The four providers are named for what
 * they DO, not their technical role — "llm / tts / stt / telephony" means
 * nothing at a glance, and this screen gets read in a hurry.
 */
function MarginPanel({ economics }: { economics: any }) {
  const talkedMin = economics.revenue.actual_minutes || 0;
  const calls = economics.coverage.billable_calls || 0;
  const avgSeconds = calls > 0 ? Math.round((talkedMin * 60) / calls) : 0;
  const earned = economics.revenue.credits;
  const paid = economics.cost.credits;
  const kept = economics.margin.credits;

  // Plain names, plus what each one does and which model is behind it.
  //
  // The model comes from models_in_use — read off the actual calls — NOT from
  // the rate card. The rate card lists every model we hold a price for, so
  // reading it would happily report the cheap model while the expensive one is
  // running, which is exactly the mistake this panel exists to catch.
  const used = economics.models_in_use ?? {};
  const modelOf = (kind: string) => (used[kind] ?? []).join(', ');
  const META: Record<string, { name: string; does: string; model: string }> = {
    telephony: { name: 'Phone line', does: 'carries the call', model: modelOf('telephony') },
    llm: { name: 'AI brain', does: 'decides what to say', model: modelOf('llm') },
    tts: { name: 'Voice', does: 'speaks the words', model: modelOf('tts') },
    stt: { name: 'Hearing', does: 'understands the caller', model: modelOf('stt') },
    overhead: { name: 'Other', does: 'fixed cost per call', model: '' },
  };

  const rows = Object.entries(economics.cost.by_provider as Record<string, number>)
    .map(([key, amount]) => ({
      key,
      amount,
      perMin: talkedMin > 0 ? amount / talkedMin : 0,
      share: paid > 0 ? (amount / paid) * 100 : 0,
      ...(META[key] ?? { name: key, does: '', model: '' }),
    }))
    .sort((a, b) => b.amount - a.amount);

  const biggest = rows[0];
  const usingBigModel = String(META.llm.model).includes('70b');

  return (
    <div className="space-y-6">
      {/* The whole story in one sentence, before any table. */}
      <Card className="shadow-paper">
        <CardContent className="p-6">
          <p className="text-lg leading-relaxed">
            Your agent talked for{' '}
            <span className="font-display font-semibold tabular-nums">{talkedMin}</span> minutes
            across <span className="font-display font-semibold tabular-nums">{calls}</span> calls
            {avgSeconds > 0 ? ` (about ${avgSeconds}s each)` : ''}. You charged{' '}
            <span className="font-display font-semibold tabular-nums">{rs(earned)}</span>, paid out{' '}
            <span className="font-display font-semibold tabular-nums">{rs(paid)}</span>, and kept{' '}
            <span
              className={cn(
                'font-display font-semibold tabular-nums',
                kept >= 0 ? 'text-primary' : 'text-destructive',
              )}
            >
              {rs(kept)}
            </span>
            .
          </p>
          <div className="mt-4 flex flex-wrap gap-x-8 gap-y-2 border-t border-border/70 pt-4 text-sm text-muted-foreground">
            <span>
              Billed{' '}
              <span className="tabular-nums text-foreground">
                {economics.revenue.billed_minutes} min
              </span>{' '}
              against <span className="tabular-nums text-foreground">{talkedMin} min</span> talked
              {economics.revenue.rounding_uplift_pct > 0 ? (
                <>
                  {' '}— rounding adds{' '}
                  <span className="tabular-nums text-foreground">
                    {economics.revenue.rounding_uplift_pct}%
                  </span>
                </>
              ) : null}
            </span>
            <span>
              Margin <span className="tabular-nums text-foreground">{economics.margin.pct}%</span>
            </span>
          </div>
        </CardContent>
      </Card>

      {/* The money flow: what came in, what went out, what is left. */}
      <Card className="shadow-paper">
        <CardHeader>
          <CardTitle className="font-display">Where the money went</CardTitle>
          <CardDescription>
            Every rupee of the {rs(earned)} the client paid, and what each part costs per minute of
            talk time.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex items-center justify-between border-b border-border pb-3">
            <span className="font-medium">Client paid you</span>
            <span className="font-display text-xl font-semibold tabular-nums">{rs(earned)}</span>
          </div>

          <div className="space-y-3 py-3">
            {rows.map((row) => (
              <div key={row.key} className="flex items-center gap-3">
                <div className="w-32 shrink-0">
                  <div className="text-sm font-medium">{row.name}</div>
                  <div className="truncate text-[11px] text-muted-foreground">{row.does}</div>
                </div>
                <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
                  <div
                    className={cn(
                      'grow-bar h-full rounded-full',
                      row.key === biggest?.key ? 'bg-primary' : 'bg-muted-foreground/40',
                    )}
                    style={{ width: `${Math.max(2, row.share)}%` }}
                  />
                </div>
                <div className="w-20 shrink-0 text-right text-sm tabular-nums">
                  −{rs(row.amount)}
                </div>
                <div className="hidden w-24 shrink-0 text-right text-xs text-muted-foreground tabular-nums sm:block">
                  {rs(row.perMin)}/min
                </div>
                <div className="w-10 shrink-0 text-right text-xs text-muted-foreground tabular-nums">
                  {Math.round(row.share)}%
                </div>
              </div>
            ))}
          </div>

          <div className="flex items-center justify-between border-t border-border pt-3">
            <span className="font-medium">You keep</span>
            <span
              className={cn(
                'font-display text-xl font-semibold tabular-nums',
                kept >= 0 ? 'text-primary' : 'text-destructive',
              )}
            >
              {rs(kept)}
            </span>
          </div>

          {economics.warnings.estimated_components.length > 0 ? (
            <p className="pt-3 text-xs text-muted-foreground">
              &ldquo;Hearing&rdquo; is estimated from call length — that provider does not report
              its own usage. Everything else is measured.
            </p>
          ) : null}
          {economics.warnings.unpriced_models.length > 0 ? (
            <p className="pt-1 text-xs text-destructive">
              Missing from your rate card, so counted as ₹0:{' '}
              {economics.warnings.unpriced_models.join(', ')}. Your real cost is higher than shown.
            </p>
          ) : null}
        </CardContent>
      </Card>

      {/* Raw units, for checking against provider invoices. */}
      <Card className="shadow-paper">
        <CardHeader>
          <CardTitle className="font-display">Check against your invoices</CardTitle>
          <CardDescription>
            What each provider should be billing you for. None of them expose a billing API we
            can read, so this is how you verify: compare these counts with the invoice.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Provider</TableHead>
                <TableHead>They bill on</TableHead>
                <TableHead className="text-right">We measured</TableHead>
                <TableHead className="text-right">Our estimate</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {[
                {
                  who: 'Vobiz',
                  unit: 'whole minutes',
                  measured: `${(economics.measured_usage?.telephony_billed_minutes ?? 0).toLocaleString('en-IN')} min`,
                  cost: economics.cost.by_provider.telephony ?? 0,
                },
                {
                  who: 'Groq',
                  unit: 'tokens in / out',
                  measured: `${(economics.measured_usage?.llm_input_tokens ?? 0).toLocaleString('en-IN')} / ${(economics.measured_usage?.llm_output_tokens ?? 0).toLocaleString('en-IN')}`,
                  cost: economics.cost.by_provider.llm ?? 0,
                },
                {
                  who: 'Cartesia',
                  unit: 'characters spoken',
                  measured: `${(economics.measured_usage?.tts_characters ?? 0).toLocaleString('en-IN')} chars`,
                  cost: economics.cost.by_provider.tts ?? 0,
                },
                {
                  who: 'Deepgram',
                  unit: 'audio minutes',
                  measured: `${economics.measured_usage?.stt_minutes ?? 0} min`,
                  cost: economics.cost.by_provider.stt ?? 0,
                },
              ].map((r) => (
                <TableRow key={r.who}>
                  <TableCell className="font-medium">{r.who}</TableCell>
                  <TableCell className="text-muted-foreground">{r.unit}</TableCell>
                  <TableCell className="text-right tabular-nums">{r.measured}</TableCell>
                  <TableCell className="text-right tabular-nums">{rs(r.cost)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <p className="mt-3 text-xs text-muted-foreground">
            The measured column is counted from the calls themselves and is exact. The estimate
            is that count multiplied by your rate card — so it is only as right as the rates you
            entered. Put your real contracted rates in the Pricing tab and these become your
            actual spend.
          </p>
        </CardContent>
      </Card>

      {/* What to actually do about it. */}
      <Card className="shadow-paper">
        <CardHeader>
          <CardTitle className="font-display">What to fix</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          {biggest ? (
            <div className="flex items-start gap-2">
              <TrendingDown className="mt-0.5 size-4 shrink-0 text-primary" />
              <span>
                <span className="font-medium">{biggest.name}</span> is your biggest cost at{' '}
                {rs(biggest.amount)} — {Math.round(biggest.share)}% of everything you spend, or{' '}
                {rs(biggest.perMin)} per minute.
                {biggest.model ? (
                  <>
                    {' '}Running <span className="font-mono text-xs">{biggest.model}</span>.
                  </>
                ) : null}
              </span>
            </div>
          ) : null}

          {usingBigModel ? (
            <div className="flex items-start gap-2 rounded-lg border border-primary/40 bg-primary/5 p-3">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-primary" />
              <span>
                You are running the <span className="font-medium">70B</span> model, which costs
                roughly 12&times; more per word than the 8B one. Switch it in Dograh &rarr; Model
                Configurations. Your workflow file already claims the 8B model — Dograh ignores
                that block, which is why the change never took effect.
              </span>
            </div>
          ) : null}

          {economics.cost.per_actual_minute > economics.rate.credits_per_minute ? (
            <div className="flex items-start gap-2 rounded-lg border border-primary/40 bg-primary/5 p-3">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-primary" />
              <span>
                A real minute of talking costs you {rs(economics.cost.per_actual_minute)} but you
                charge {rs(economics.rate.credits_per_minute)}. You make money only because calls
                round up to a full minute — a call running just under a minute boundary loses
                money. Either move to the cheaper model, or raise the rate.
              </span>
            </div>
          ) : (
            <div className="flex items-start gap-2">
              <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-primary" />
              <span>
                A real minute costs {rs(economics.cost.per_actual_minute)} against{' '}
                {rs(economics.rate.credits_per_minute)} charged — profitable at any call length.
              </span>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Individual calls that lost money. */}
      <Card className="shadow-paper">
        <CardHeader>
          <CardTitle className="font-display">Calls that lost money</CardTitle>
          <CardDescription>Worst first. Empty is what you want here.</CardDescription>
        </CardHeader>
        <CardContent>
          {economics.loss_making_calls.length === 0 ? (
            <p className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
              <CheckCircle2 className="size-4 text-primary" />
              None — every call in this period made money.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Call</TableHead>
                  <TableHead>Length</TableHead>
                  <TableHead className="text-right">Charged</TableHead>
                  <TableHead className="text-right">Cost</TableHead>
                  <TableHead className="text-right">Lost</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {economics.loss_making_calls.map((c: any) => (
                  <TableRow key={c.dograh_run_id}>
                    <TableCell className="font-mono text-xs">{c.dograh_run_id}</TableCell>
                    <TableCell className="tabular-nums">{c.actual_seconds}s</TableCell>
                    <TableCell className="text-right tabular-nums">{rs(c.revenue_credits)}</TableCell>
                    <TableCell className="text-right tabular-nums">{rs(c.cost_credits)}</TableCell>
                    <TableCell className="text-right font-medium tabular-nums text-destructive">
                      {rs(c.loss_credits)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

/** Price per minute, thresholds, and the payment details the client sees. */
function PricingPanel({ account, onSaved }: { account: any; onSaved: () => void }) {
  const [rate, setRate] = useState(String((account.rate_milli_per_minute ?? 4000) / 1000));
  const [upi, setUpi] = useState(account.payment_instructions?.upi_id ?? '');
  const [qr, setQr] = useState(account.payment_instructions?.qr_image_url ?? '');
  const [rateCard, setRateCard] = useState(JSON.stringify(account.rate_card ?? {}, null, 2));
  const [rateCardError, setRateCardError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function save() {
    // Parse before touching the network: a typo here would otherwise be saved
    // as a broken rate card and quietly make every margin figure wrong.
    let parsedRateCard: any;
    try {
      parsedRateCard = JSON.parse(rateCard);
      setRateCardError(null);
    } catch (err: any) {
      setRateCardError('That is not valid JSON — check for a missing comma or bracket.');
      return;
    }

    setBusy(true);
    try {
      await opFetch('/api/operator/settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          rate_milli_per_minute: Math.round(Number(rate) * 1000),
          rate_card: parsedRateCard,
          payment_instructions: {
            ...(account.payment_instructions ?? {}),
            upi_id: upi.trim(),
            qr_image_url: qr.trim(),
          },
        }),
      });
      toast.success('Saved');
      onSaved();
    } catch (err: any) {
      toast.error('Could not save', { description: err?.message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Pricing and payment</CardTitle>
        <CardDescription>
          What the client is charged, and how they pay you. They cannot see or change any of this.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="w-48 space-y-1.5">
          <label htmlFor="rate" className="text-xs text-muted-foreground">Credits per minute</label>
          <Input id="rate" value={rate} onChange={(e) => setRate(e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <label htmlFor="upi" className="text-xs text-muted-foreground">UPI ID</label>
          <Input id="upi" value={upi} onChange={(e) => setUpi(e.target.value)} placeholder="name@bank" />
        </div>
        <div className="space-y-1.5">
          <label htmlFor="qr" className="text-xs text-muted-foreground">QR image URL</label>
          <Input
            id="qr"
            value={qr}
            onChange={(e) => setQr(e.target.value)}
            placeholder="/payment-qr.png"
          />
          <p className="text-xs text-muted-foreground">
            Put the image in the app&apos;s public folder and enter its path, e.g. /payment-qr.png
          </p>
        </div>
        {qr && (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={qr} alt="QR preview" className="size-32 rounded-md border border-border object-contain" />
        )}
        <div className="space-y-1.5 border-t border-border pt-4">
          <label htmlFor="ratecard" className="text-sm font-medium">
            What you pay your providers
          </label>
          <p className="text-xs text-muted-foreground">
            Put your REAL contracted rates here — your Vobiz per-minute price, and the
            published rates from Groq, Cartesia and Deepgram. The margin figures are only as
            accurate as these numbers, because no provider gives us a billing API to read.
          </p>
          <textarea
            id="ratecard"
            value={rateCard}
            onChange={(e) => setRateCard(e.target.value)}
            spellCheck={false}
            rows={14}
            className="w-full rounded-lg border border-input bg-muted/30 p-3 font-mono text-xs outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
          />
          {rateCardError && <p className="text-xs text-destructive">{rateCardError}</p>}
        </div>

        <Button onClick={save} disabled={busy}>
          {busy && <Loader2 data-icon="inline-start" className="animate-spin" />}
          Save
        </Button>
      </CardContent>
    </Card>
  );
}

/**
 * Dashboard logins. OPERATOR ONLY.
 *
 * There is no public sign-up, on purpose: this dashboard exposes the client's
 * whole lead list and every call recording, so anyone who could register would
 * be able to read all of it. Accounts are created here and handed over.
 *
 * A generated password is shown ONCE. Supabase stores only a bcrypt hash, so it
 * genuinely cannot be retrieved afterwards — a forgotten password is reset, not
 * looked up.
 */
function UsersPanel({ users, onChanged }: { users: any[]; onChanged: () => void }) {
  const [email, setEmail] = useState('');
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [reveal, setReveal] = useState<{ email: string; password: string } | null>(null);

  async function createUser() {
    setBusy(true);
    try {
      const result = await opFetch('/api/operator/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email.trim(), name: name.trim() || undefined }),
      });
      setReveal({ email: result.user.email, password: result.password });
      setEmail('');
      setName('');
      onChanged();
    } catch (err: any) {
      toast.error('Could not create the login', { description: err?.message });
    } finally {
      setBusy(false);
    }
  }

  async function resetPassword(user: any) {
    setBusy(true);
    try {
      const result = await opFetch('/api/operator/users', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: user.id }),
      });
      setReveal({ email: user.email, password: result.password });
      onChanged();
    } catch (err: any) {
      toast.error('Could not reset', { description: err?.message });
    } finally {
      setBusy(false);
    }
  }

  async function removeUser(user: any) {
    setBusy(true);
    try {
      await opFetch(`/api/operator/users?id=${encodeURIComponent(user.id)}`, { method: 'DELETE' });
      toast.success(`Removed ${user.email}`);
      onChanged();
    } catch (err: any) {
      toast.error('Could not remove', { description: err?.message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      {reveal && (
        <Card className="border-primary/50 bg-primary/5">
          <CardHeader>
            <CardTitle className="font-display">Password for {reveal.email}</CardTitle>
            <CardDescription>
              Copy this now and send it to them. It cannot be shown again — only reset.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap items-center gap-3">
            <code className="rounded-md border border-border bg-background px-3 py-2 font-mono text-base tracking-wide">
              {reveal.password}
            </code>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                navigator.clipboard.writeText(reveal.password);
                toast.success('Copied');
              }}
            >
              Copy
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setReveal(null)}>
              Done
            </Button>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Create a login</CardTitle>
          <CardDescription>
            For the client, or anyone else who needs the dashboard. They get the leads,
            calls and credits — never this console.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-end gap-3">
          <div className="min-w-56 flex-1 space-y-1.5">
            <label htmlFor="new-email" className="text-xs text-muted-foreground">Email</label>
            <Input
              id="new-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="client@company.com"
            />
          </div>
          <div className="w-44 space-y-1.5">
            <label htmlFor="new-name" className="text-xs text-muted-foreground">Name (optional)</label>
            <Input
              id="new-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Sreekanth"
            />
          </div>
          <Button onClick={createUser} disabled={busy || !email.includes('@')}>
            {busy && <Loader2 data-icon="inline-start" className="animate-spin" />}
            Create login
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Who can sign in</CardTitle>
          <CardDescription>{users.length} account{users.length === 1 ? '' : 's'}.</CardDescription>
        </CardHeader>
        <CardContent>
          {users.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">No logins yet.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Email</TableHead>
                  <TableHead>Name</TableHead>
                  <TableHead>Last signed in</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {users.map((u) => (
                  <TableRow key={u.id}>
                    <TableCell className="font-medium">{u.email}</TableCell>
                    <TableCell className="text-muted-foreground">{u.name ?? '—'}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {u.last_sign_in_at
                        ? new Date(u.last_sign_in_at).toLocaleString('en-IN', {
                            day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit',
                          })
                        : 'Never'}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-2">
                        <Button variant="outline" size="sm" disabled={busy} onClick={() => resetPassword(u)}>
                          Reset password
                        </Button>
                        <Button variant="ghost" size="sm" disabled={busy} onClick={() => removeUser(u)}>
                          Remove
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

/**
 * What the client actually thinks, in their own words.
 *
 * Read-only and operator-only: /api/feedback is write-only from the dashboard
 * side, so the client never sees this list. That is the point — a client who can
 * see their own past complaints on a screen their staff also use will stop
 * writing honest ones.
 */
function FeedbackPanel({ data }: { data: any }) {
  if (!data) {
    return (
      <Card>
        <CardContent className="py-8 text-center text-sm text-muted-foreground">
          No feedback yet — or the <code>feedback</code> table is missing. Run
          <code className="mx-1">scripts/008_trash_and_feedback.sql</code> in Supabase if this is a fresh deployment.
        </CardContent>
      </Card>
    );
  }

  const s = data.summary ?? {};
  const rows: any[] = data.submissions ?? [];

  const CHANGE_LABELS: Record<string, string> = {
    much_better: 'Improved a lot',
    better: 'Improved a little',
    same: 'About the same',
    worse: 'Got worse',
    too_early: 'Too early to say',
  };
  const HOURS_LABELS: Record<string, string> = {
    none: 'None yet',
    under_5: 'Under 5h',
    '5_to_15': '5–15h',
    over_15: '15h+',
  };

  const tiles = [
    { label: 'Submissions', value: s.total ?? 0 },
    { label: 'Dashboard', value: s.dashboard != null ? `${s.dashboard} / 5` : '—' },
    { label: 'Voice quality', value: s.voice != null ? `${s.voice} / 5` : '—' },
    { label: 'Understanding', value: s.understanding != null ? `${s.understanding} / 5` : '—' },
    // NPS is meaningless on two responses, so the sample size travels with it.
    { label: 'NPS', value: s.nps != null ? `${s.nps} (${s.npsResponses})` : '—' },
    { label: 'Say qualification improved', value: s.improvedCount ?? 0 },
  ];

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-6">
        {tiles.map((tile) => (
          <Card key={tile.label}>
            <CardContent className="p-4">
              <p className="text-2xl font-semibold">{tile.value}</p>
              <p className="text-xs text-muted-foreground">{tile.label}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Every submission</CardTitle>
          <CardDescription>Newest first. The client cannot see this list.</CardDescription>
        </CardHeader>
        <CardContent>
          {rows.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">Nobody has submitted feedback yet.</p>
          ) : (
            <div className="space-y-4">
              {rows.map((row) => (
                <div key={row.id} className="rounded-lg border p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline" className="capitalize">{row.vertical}</Badge>
                    {row.qualification_change && (
                      <Badge
                        variant="outline"
                        className={cn(
                          'border-transparent',
                          row.qualification_change === 'worse'
                            ? 'bg-destructive/10 text-destructive'
                            : row.qualification_change === 'much_better' || row.qualification_change === 'better'
                              ? 'bg-emerald-500/10 text-emerald-600'
                              : 'bg-muted text-muted-foreground',
                        )}
                      >
                        {CHANGE_LABELS[row.qualification_change] ?? row.qualification_change}
                      </Badge>
                    )}
                    <span className="ms-auto text-xs text-muted-foreground">
                      {new Date(row.created_at).toLocaleString('en-IN')}
                      {row.submitted_by ? ` · ${row.submitted_by}` : ''}
                    </span>
                  </div>

                  <div className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-sm text-muted-foreground">
                    <span>Dashboard {row.dashboard_rating ?? '—'}/5</span>
                    <span>Voice {row.voice_rating ?? '—'}/5</span>
                    <span>Understanding {row.understanding_rating ?? '—'}/5</span>
                    {row.recommend_score != null && <span>Would recommend {row.recommend_score}/10</span>}
                    {row.hours_saved && <span>Saves {HOURS_LABELS[row.hours_saved] ?? row.hours_saved}/week</span>}
                    {(row.qualified_before_week != null || row.qualified_after_week != null) && (
                      <span>
                        Qualified/week {row.qualified_before_week ?? '—'} → {row.qualified_after_week ?? '—'}
                      </span>
                    )}
                  </div>

                  {row.improvements && (
                    <p className="mt-3 whitespace-pre-wrap border-l-2 border-border pl-3 text-sm">{row.improvements}</p>
                  )}
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
