"use client"

import { useCallback, useEffect, useState } from "react"
import { toast } from "sonner"
import { Phone, PhoneCall, Loader2, AlertTriangle } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { VERTICALS, VERTICAL_LABELS, VERTICAL_STYLES, parseVertical, type Vertical } from "@/lib/verticals"
import { cn } from "@/lib/utils"

/**
 * Placing a call by hand.
 *
 * Every path here ends at POST /api/calls/single, so there is exactly ONE place
 * in the app where a manual call can be placed. A confirmation dialog always
 * stands in front of it: this dials a real person and spends real credits, and a
 * misclick on a table row is otherwise indistinguishable from an intention.
 */

/** Statuses that mean a campaign already holds this lead. */
const CAMPAIGN_HELD = new Set(["queued"])

export type CallableLead = {
  id: string
  name?: string | null
  phone?: string | null
  vertical?: string | null
  status?: string | null
  retry_count?: number | null
  last_attempt_at?: string | null
}

/** Why this lead cannot be called right now, or null when it can. */
export function callBlockedReason(lead: CallableLead | null | undefined): string | null {
  if (!lead) return "No lead selected"
  if (!lead.phone) return "This lead has no phone number"
  if (CAMPAIGN_HELD.has(String(lead.status)))
    return "Already queued in a campaign — calling now would ring them twice"
  return null
}

function formatWhen(value: string | null | undefined) {
  if (!value) return null
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return null
  return d.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })
}

/**
 * The confirmation step. Shows who is being called, by which agent, and every
 * warning that should make an operator pause — but never refuses on its own,
 * because the decision is theirs.
 */
function ConfirmCallDialog({
  open,
  onOpenChange,
  lead,
  vertical,
  onPlaced,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  lead: CallableLead | null
  vertical: Vertical
  onPlaced?: () => void
}) {
  const [isCalling, setIsCalling] = useState(false)
  const timesCalled = lead?.retry_count ?? 0
  const lastCall = formatWhen(lead?.last_attempt_at)

  const placeCall = useCallback(async () => {
    if (!lead || isCalling) return
    setIsCalling(true)
    try {
      const res = await fetch("/api/calls/single", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ leadId: lead.id }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.error || `Could not place the call (${res.status})`)
      toast.success(data.message || "Calling now.", { duration: 8000 })
      onOpenChange(false)
      onPlaced?.()
    } catch (err: any) {
      toast.error(err.message || "Could not place the call")
    } finally {
      setIsCalling(false)
    }
  }, [lead, isCalling, onOpenChange, onPlaced])

  return (
    <Dialog open={open} onOpenChange={(v) => !isCalling && onOpenChange(v)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Call this person now?</DialogTitle>
          <DialogDescription>
            This places a real phone call straight away and uses credits.
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="grid gap-4">
          <div className="rounded-lg border bg-muted/20 p-3">
            <p className="text-base font-medium">{lead?.name?.trim() || "Unnamed lead"}</p>
            <p className="font-mono text-sm text-muted-foreground">{lead?.phone}</p>
            <div className="mt-2 flex items-center gap-2">
              <span className="text-xs text-muted-foreground">Called by</span>
              <Badge
                variant="outline"
                className={cn(
                  VERTICAL_STYLES[vertical].color,
                  VERTICAL_STYLES[vertical].bg,
                  "border-transparent",
                )}
              >
                {VERTICAL_LABELS[vertical]} agent
              </Badge>
            </div>
          </div>

          {/* Shown so the choice is informed. None of these block the call. */}
          {(timesCalled > 0 || lastCall) && (
            <div className="flex gap-2 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-sm">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
              <div>
                {timesCalled > 0 && (
                  <p>
                    Already called <span className="font-medium">{timesCalled}</span>{" "}
                    {timesCalled === 1 ? "time" : "times"}.
                  </p>
                )}
                {lastCall && <p className="text-muted-foreground">Last call: {lastCall}</p>}
              </div>
            </div>
          )}

          <p className="text-xs text-muted-foreground">
            One call is placed. It will not be automatically redialled, and the recording,
            transcript and score appear on this lead when the call finishes.
          </p>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={isCalling}>
            Cancel
          </Button>
          <Button onClick={placeCall} disabled={isCalling}>
            {isCalling ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Placing call…
              </>
            ) : (
              <>
                <PhoneCall className="mr-2 h-4 w-4" /> Call now
              </>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/**
 * The Call button that sits beside a lead. Disabled — with the reason visible —
 * rather than hidden, so it is obvious WHY a particular lead cannot be called.
 */
export function CallLeadButton({
  lead,
  onPlaced,
  size = "sm",
  variant = "outline",
  className,
}: {
  lead: CallableLead
  onPlaced?: () => void
  size?: "sm" | "default" | "icon"
  variant?: "outline" | "default" | "ghost"
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const blocked = callBlockedReason(lead)
  const vertical = parseVertical(lead.vertical) ?? "loan"

  return (
    <>
      <Button
        size={size}
        variant={variant}
        className={className}
        disabled={!!blocked}
        title={blocked ?? `Call ${lead.name?.trim() || lead.phone} now`}
        onClick={(e) => {
          // Rows open the detail panel on click; calling must not also do that.
          e.stopPropagation()
          setOpen(true)
        }}
      >
        <Phone className={size === "icon" ? "h-4 w-4" : "mr-2 h-4 w-4"} />
        {size === "icon" ? null : "Call"}
      </Button>
      <ConfirmCallDialog
        open={open}
        onOpenChange={setOpen}
        lead={lead}
        vertical={vertical}
        onPlaced={onPlaced}
      />
    </>
  )
}

/**
 * Add one person and call them, without importing a file.
 *
 * The business line is a required, unseeded choice for the same reason it is on
 * the import: it decides which agent, with which script, calls a real person,
 * and it cannot be worked out from the number.
 */
export function QuickCallDialog({
  open,
  onOpenChange,
  onPlaced,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  onPlaced?: () => void
}) {
  const [vertical, setVertical] = useState<Vertical | null>(null)
  const [phone, setPhone] = useState("")
  const [name, setName] = useState("")
  const [city, setCity] = useState("")
  const [lookup, setLookup] = useState<any>(null)
  const [isCalling, setIsCalling] = useState(false)

  // Cleared on every open so one call's details can never leak into the next.
  useEffect(() => {
    if (open) {
      setVertical(null)
      setPhone("")
      setName("")
      setCity("")
      setLookup(null)
    }
  }, [open])

  // Debounced so typing a number does not fire a request per keystroke.
  useEffect(() => {
    if (!phone.trim()) {
      setLookup(null)
      return
    }
    const id = setTimeout(async () => {
      try {
        const qs = new URLSearchParams({ phone })
        if (vertical) qs.set("vertical", vertical)
        const res = await fetch(`/api/leads/lookup?${qs}`)
        setLookup(res.ok ? await res.json() : null)
      } catch {
        setLookup(null)
      }
    }, 400)
    return () => clearTimeout(id)
  }, [phone, vertical])

  const existing = lookup?.found ? lookup.lead : null
  /*
   * A NEW lead needs a name; an existing one already has one.
   *
   * `leads.name` is NOT NULL in the live database, so submitting this form with
   * the name blank failed the insert and surfaced as "Could not save this person
   * as a lead" — the field said Optional and the database disagreed.
   *
   * Required rather than defaulted, because the name is not decoration: it is
   * sent to the agent as `customer_name` and spoken in the greeting. Filling it
   * with the phone number would have the agent greet someone as
   * "+91 63024 88456 garu" on a real call.
   */
  const needsName = !existing
  const canCall =
    !!vertical && phone.trim().length >= 10 && (!needsName || name.trim().length > 0) && !isCalling

  const placeCall = useCallback(async () => {
    if (!canCall) return
    setIsCalling(true)
    try {
      const res = await fetch("/api/calls/single", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ phone, vertical, name: name.trim() || undefined, city: city.trim() || undefined }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.error || `Could not place the call (${res.status})`)
      toast.success(data.message || "Calling now.", { duration: 8000 })
      onOpenChange(false)
      onPlaced?.()
    } catch (err: any) {
      toast.error(err.message || "Could not place the call")
    } finally {
      setIsCalling(false)
    }
  }, [canCall, phone, vertical, name, city, onOpenChange, onPlaced])

  return (
    <Dialog open={open} onOpenChange={(v) => !isCalling && onOpenChange(v)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Call one person</DialogTitle>
          <DialogDescription>
            Add someone and call them straight away — no file needed. They are saved as a lead,
            and the recording, transcript and score land on them like any other call.
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="grid gap-4">
          <div className="grid gap-2">
            <label className="text-sm font-medium" htmlFor="quick-vertical">
              Business line
            </label>
            <Select value={vertical ?? undefined} onValueChange={(v) => setVertical(v as Vertical)}>
              <SelectTrigger id="quick-vertical">
                <SelectValue placeholder="Choose a business line…" />
              </SelectTrigger>
              <SelectContent>
                {VERTICALS.map((v) => (
                  <SelectItem key={v} value={v}>
                    {VERTICAL_LABELS[v]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              This decides which agent calls them, and with which script.
            </p>
          </div>

          <div className="grid gap-2">
            <label className="text-sm font-medium" htmlFor="quick-phone">
              Phone number
            </label>
            <Input
              id="quick-phone"
              inputMode="tel"
              placeholder="9876543210"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
            />
          </div>

          {/* The "already exists" notice. Informs, never blocks. */}
          {existing && vertical && (
            <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-sm">
              <p className="font-medium">Already a {VERTICAL_LABELS[vertical]} lead</p>
              <p className="text-muted-foreground">
                {existing.name?.trim() ? `${existing.name} · ` : ""}score {existing.score ?? 0} ·{" "}
                called {existing.timesCalled ?? 0}{" "}
                {(existing.timesCalled ?? 0) === 1 ? "time" : "times"}
                {formatWhen(existing.last_attempt_at) ? ` · last ${formatWhen(existing.last_attempt_at)}` : ""}
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                Calling adds to this person&rsquo;s existing history — no duplicate is created.
              </p>
            </div>
          )}

          {/* Other business lines are information only: they are separate leads. */}
          {lookup?.otherLines?.length > 0 && (
            <p className="text-xs text-muted-foreground">
              This number is also a lead in{" "}
              {lookup.otherLines.map((o: any) => o.line).join(", ")}. Those are separate leads with
              their own history and are not affected.
            </p>
          )}

          <div className="grid gap-2 sm:grid-cols-2">
            <div className="grid gap-2">
              <label className="text-sm font-medium" htmlFor="quick-name">
                Name {needsName && <span className="text-destructive">*</span>}
              </label>
              <Input
                id="quick-name"
                placeholder={needsName ? "Required" : "Optional"}
                value={name}
                onChange={(e) => setName(e.target.value)}
                aria-required={needsName}
              />
              {needsName && (
                <p className="text-xs text-muted-foreground">
                  The agent greets them by name, so this is spoken on the call.
                </p>
              )}
            </div>
            <div className="grid gap-2">
              <label className="text-sm font-medium" htmlFor="quick-city">
                City
              </label>
              <Input
                id="quick-city"
                placeholder="Optional"
                value={city}
                onChange={(e) => setCity(e.target.value)}
              />
            </div>
          </div>

          <p className="text-xs text-muted-foreground">
            One call is placed as soon as you press Call. It is not automatically redialled.
          </p>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={isCalling}>
            Cancel
          </Button>
          <Button onClick={placeCall} disabled={!canCall}>
            {isCalling ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Placing call…
              </>
            ) : (
              <>
                <PhoneCall className="mr-2 h-4 w-4" />
                {vertical ? `Call now with ${VERTICAL_LABELS[vertical]} agent` : "Choose a business line first"}
              </>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
