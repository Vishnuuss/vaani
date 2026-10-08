"use client"

import { useState } from "react"
import { CalendarRange, X } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog"
import { cn } from "@/lib/utils"

/**
 * "Show me a date range", shared by the Leads and Call History pages.
 *
 * Held as ISO timestamps rather than plain dates because both endpoints compare
 * against timestamptz columns. A bare "2026-08-19" would be read as midnight UTC,
 * which in IST is 05:30 the same morning — so a client asking for "today" would
 * silently lose every call placed before half past five and, worse, a delete run
 * on that filter would spare rows the table had already shown them. The range is
 * therefore snapped to the START of the from-day and the END of the to-day in the
 * browser's own timezone.
 *
 * `to` is exclusive-by-construction: it is the last millisecond of the chosen
 * day, so `lt`/`lte` on either endpoint includes the whole day either way.
 */

export interface DateRange {
  /** ISO timestamp, inclusive. */
  from?: string
  /** ISO timestamp, inclusive to the end of that day. */
  to?: string
  /** Which preset produced this, for the label. 'custom' when hand-picked. */
  preset?: string
}

export const EMPTY_RANGE: DateRange = {}

function startOfDay(d: Date) {
  const x = new Date(d)
  x.setHours(0, 0, 0, 0)
  return x
}

function endOfDay(d: Date) {
  const x = new Date(d)
  x.setHours(23, 59, 59, 999)
  return x
}

function daysAgo(n: number) {
  const x = new Date()
  x.setDate(x.getDate() - n)
  return x
}

/** `yyyy-mm-dd` for a native date input, in local time rather than UTC. */
function toInputValue(iso?: string): string {
  if (!iso) return ""
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ""
  const pad = (n: number) => String(n).padStart(2, "0")
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

const PRESETS: { label: string; build: () => DateRange }[] = [
  { label: "Today", build: () => ({ from: startOfDay(new Date()).toISOString(), to: endOfDay(new Date()).toISOString(), preset: "Today" }) },
  { label: "Yesterday", build: () => ({ from: startOfDay(daysAgo(1)).toISOString(), to: endOfDay(daysAgo(1)).toISOString(), preset: "Yesterday" }) },
  { label: "Last 7 days", build: () => ({ from: startOfDay(daysAgo(6)).toISOString(), to: endOfDay(new Date()).toISOString(), preset: "Last 7 days" }) },
  { label: "Last 30 days", build: () => ({ from: startOfDay(daysAgo(29)).toISOString(), to: endOfDay(new Date()).toISOString(), preset: "Last 30 days" }) },
  { label: "This month", build: () => { const n = new Date(); return { from: startOfDay(new Date(n.getFullYear(), n.getMonth(), 1)).toISOString(), to: endOfDay(n).toISOString(), preset: "This month" } } },
]

const fmt = (iso: string) => new Date(iso).toLocaleDateString("en-IN", { day: "2-digit", month: "short" })

/** How the current range reads on the trigger button. */
export function describeRange(range: DateRange): string | null {
  if (!range.from && !range.to) return null
  if (range.preset && range.preset !== "custom") return range.preset
  if (range.from && range.to) return `${fmt(range.from)} – ${fmt(range.to)}`
  if (range.from) return `From ${fmt(range.from)}`
  return `Until ${fmt(range.to!)}`
}

export function DateRangeFilter({
  value,
  onChange,
  /** What the dates refer to, e.g. "Imported" or "Called". */
  noun,
  className,
}: {
  value: DateRange
  onChange: (next: DateRange) => void
  noun: string
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const [fromDraft, setFromDraft] = useState("")
  const [toDraft, setToDraft] = useState("")

  const active = describeRange(value)

  function openDialog() {
    setFromDraft(toInputValue(value.from))
    setToDraft(toInputValue(value.to))
    setOpen(true)
  }

  function applyCustom() {
    // A one-sided range is valid and useful — "everything before 1 July" is the
    // shape of every clear-out request this page gets — so neither end is required.
    const next: DateRange = {
      from: fromDraft ? startOfDay(new Date(`${fromDraft}T00:00:00`)).toISOString() : undefined,
      to: toDraft ? endOfDay(new Date(`${toDraft}T00:00:00`)).toISOString() : undefined,
      preset: "custom",
    }
    onChange(next.from || next.to ? next : EMPTY_RANGE)
    setOpen(false)
  }

  return (
    <>
      <div className={cn("flex items-center gap-1.5", className)}>
        <Button
          variant={active ? "default" : "outline"}
          size="sm"
          onClick={openDialog}
          // Truncates rather than wraps: this sits in a filter row that is only
          // ~360px wide on a phone and a two-line button would shove the table down.
          className="min-w-0 max-w-[62vw] sm:max-w-none"
        >
          <CalendarRange data-icon="inline-start" />
          <span className="truncate">{active ?? "Any date"}</span>
        </Button>
        {active && (
          // Clearing is a separate control rather than an option inside the
          // dialog: undoing a filter should never cost two taps and a read.
          <Button
            variant="ghost"
            size="icon"
            aria-label={`Clear the ${noun.toLowerCase()} date filter`}
            onClick={() => onChange(EMPTY_RANGE)}
          >
            <X className="size-4" />
          </Button>
        )}
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{noun} between</DialogTitle>
            <DialogDescription>
              Narrows the list below. Anything you delete while a date range is on is limited to
              that range too.
            </DialogDescription>
          </DialogHeader>

          <DialogBody className="flex flex-col gap-5">
            <div className="flex flex-wrap gap-2">
              {PRESETS.map((p) => {
                const isActive = value.preset === p.label
                return (
                  <Button
                    key={p.label}
                    variant={isActive ? "default" : "outline"}
                    size="sm"
                    onClick={() => {
                      onChange(p.build())
                      setOpen(false)
                    }}
                  >
                    {p.label}
                  </Button>
                )
              })}
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="grid gap-1.5">
                <label className="text-sm font-medium" htmlFor="range-from">
                  From
                </label>
                <input
                  id="range-from"
                  type="date"
                  value={fromDraft}
                  max={toDraft || undefined}
                  onChange={(e) => setFromDraft(e.target.value)}
                  className="h-11 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
                />
              </div>
              <div className="grid gap-1.5">
                <label className="text-sm font-medium" htmlFor="range-to">
                  To
                </label>
                <input
                  id="range-to"
                  type="date"
                  value={toDraft}
                  min={fromDraft || undefined}
                  onChange={(e) => setToDraft(e.target.value)}
                  className="h-11 w-full rounded-md border border-input bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
                />
              </div>
            </div>
            <p className="text-xs text-muted-foreground">
              Leave one side empty for an open-ended range — a &ldquo;To&rdquo; on its own means
              everything up to that day.
            </p>
          </DialogBody>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                onChange(EMPTY_RANGE)
                setOpen(false)
              }}
            >
              Any date
            </Button>
            <Button onClick={applyCustom} disabled={!fromDraft && !toDraft}>
              Apply
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
