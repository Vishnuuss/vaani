'use client';

import { CircleDollarSign, Plus, TriangleAlert } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { Skeleton } from '@/components/motion/primitives';
import { cn } from '@/lib/utils';
import { useCredits } from '@/hooks/use-credits';

/**
 * The running balance, in the top bar.
 *
 * Four states, because "you have credits" and "you are about to be cut off"
 * must not look the same at a glance. Colour comes from existing tokens so it
 * sits inside the palette rather than shouting over it.
 */
export function CreditsPill({ onTopUp }: { onTopUp: () => void }) {
  const { data, isLoading } = useCredits();

  // Never flash a zero while the first request is in flight — a momentary "0"
  // reads as "out of credits" and is alarming for no reason.
  if (!data && isLoading) {
    return <Skeleton className="h-9 w-28 rounded-full" />;
  }
  if (!data) return null;

  const { balance_credits: balance, state, runway, rate } = data;
  const isNegative = balance < 0;

  const styles: Record<string, string> = {
    healthy: 'border-input bg-muted/40 text-foreground hover:bg-muted/70',
    low: 'border-primary/40 bg-primary/5 text-primary hover:bg-primary/10',
    critical: 'border-primary/60 bg-primary/10 text-primary hover:bg-primary/15 pulse-soft',
    empty: 'border-destructive bg-destructive text-destructive-foreground hover:bg-destructive/90',
  };

  const label = isNegative
    ? 'Overdrawn'
    : state === 'empty'
      ? 'Out of credits'
      : `${balance.toLocaleString('en-IN', { maximumFractionDigits: 0 })} credits`;

  const runwayText = (() => {
    if (state === 'empty') {
      return isNegative
        ? `Overdrawn by ${Math.abs(balance).toFixed(0)} credits. Calling has stopped.`
        : 'No credits left. Calling has stopped.';
    }
    const minutes = `about ${runway.minutes.toLocaleString('en-IN')} minutes of calling left`;
    if (runway.days === null) return `${minutes}. No recent calls to estimate from.`;
    return `${minutes} — roughly ${runway.days} day${runway.days === 1 ? '' : 's'} at your current pace.`;
  })();

  return (
    // Own provider: nothing else in the app uses Tooltip, so there is no
    // ancestor provider to inherit a delay from.
    <TooltipProvider delay={200}>
      <Tooltip>
        <TooltipTrigger
        render={
          <Button
            variant="outline"
            size="sm"
            onClick={onTopUp}
            aria-label={`${label}. Add credits.`}
            className={cn(
              'gap-1.5 rounded-full px-2.5 font-medium tabular-nums transition-colors sm:px-3',
              styles[state],
            )}
          >
            {state === 'empty'
              ? <TriangleAlert data-icon="inline-start" />
              : <CircleDollarSign data-icon="inline-start" />}
            <span>{label}</span>
            {(state === 'low' || state === 'critical' || state === 'empty') && (
              <Plus className="size-3.5 opacity-80" />
            )}
          </Button>
        }
        />
        {/* The popup is a single-line flex row by default; this content is
            three stacked lines, so override to a block column. */}
        <TooltipContent side="bottom" className="block w-64 max-w-64 py-2 leading-relaxed">
          <p className="font-medium">{runwayText}</p>
          <p className="mt-1 opacity-80">
            Billed at {rate.credits_per_minute} credits a minute, rounded up to the minute.
          </p>
          <p className="mt-1 opacity-80">Click to add credits.</p>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
