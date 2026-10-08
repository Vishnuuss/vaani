"use client"

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useLeads, useLeadStats, useLead, updateLead, type LeadQuery, type VerticalFilter } from '@/hooks/use-leads'
import { useCampaigns, useLeadSegmentCounts, launchCampaign, pauseCampaign, resumeCampaign } from '@/hooks/use-campaigns'
import type { LeadSegment } from '@/lib/lead-segments'
import {
  DEFAULT_VERTICAL,
  VERTICALS,
  VERTICAL_LABELS,
  VERTICAL_STYLES,
  parseVertical,
  type Vertical,
} from '@/lib/verticals'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useCalls, useCallStats, useTranscript } from '@/hooks/use-calls'
import { useSettings } from '@/hooks/use-settings'
import { useHealth, useOverview, useQuality, useSources, useWeekly } from '@/hooks/use-reports'
import { useCredits } from '@/hooks/use-credits'
import { CreditsPill } from '@/components/billing/credits-pill'
import { TopUpDialog } from '@/components/billing/topup-dialog'
import { UsageBillingPanel } from '@/components/billing/usage-billing-panel'
import {
  Activity,
  AlertTriangle,
  ArrowUpRight,
  Bell,
  Bot,
  Calendar,
  CalendarClock,
  Check,
  ChevronDown,
  CircleDollarSign,
  Clock3,
  Copy,
  Download,
  Eye,
  FileSpreadsheet,
  Globe,
  Headphones,
  LayoutDashboard,
  ListFilter,
  LogOut,
  Mail,
  MapPin,
  Megaphone,
  Menu,
  MessageSquare,
  Pause,
  Phone,
  PhoneCall,
  PhoneForwarded,
  PhoneMissed,
  PhoneOff,
  Play,
  Plus,
  RefreshCw,
  Search,
  Settings,
  Target,
  TrendingUp,
  Upload,
  Users,
  Volume2,
  X,
  Zap,
  Wallet,
  Trash2,
} from "lucide-react"
import { toast } from "sonner"
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Pie, PieChart, XAxis, YAxis } from "recharts"

import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Button, buttonVariants } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { ChartConfig, ChartContainer, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Sheet, SheetBody, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Input } from "@/components/ui/input"
import { Progress } from "@/components/ui/progress"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { cn } from "@/lib/utils"
import { createBrowserClient } from "@/lib/supabase-browser"
import { BsWealthLockupInline } from "@/components/brand/bs-wealth-mark"
import { IntroSequence } from "@/components/motion/intro-sequence"
import { Skeleton, StatSkeletonRow } from "@/components/motion/primitives"
import { CallLeadButton, QuickCallDialog, callBlockedReason } from "@/components/manual-call"
import { BulkDeleteBar, RowCheckbox, SelectButton, useRowSelection } from "@/components/bulk-delete"
import { DateRangeFilter, EMPTY_RANGE, type DateRange } from "@/components/date-range-filter"
import { RecycleBinPage } from "@/components/recycle-bin"
import { FeedbackDialog } from "@/components/feedback-dialog"

// ─── CONSTANTS & HELPERS ───────────────────────────────

const chartConfig = {
  calls: { label: "Calls", color: "var(--chart-1)" },
  qualified: { label: "Qualified", color: "var(--chart-2)" },
} satisfies ChartConfig

const CHART_COLORS = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)"]

/**
 * The lead filters offered in the UI, mapped to what the API actually accepts.
 * The old menu offered "Site visit" / "Not interested" and sent them as
 * `status=Site visit`, which matches no row in the table — so filtering always
 * emptied the list.
 */
const LEAD_FILTERS: { label: string; query: LeadQuery }[] = [
  { label: "All", query: {} },
  { label: "New", query: { status: "new" } },
  { label: "Queued", query: { status: "queued" } },
  { label: "Called", query: { status: "called" } },
  { label: "Qualified", query: { qualification: "qualified" } },
  { label: "Not qualified", query: { qualification: "not_qualified" } },
  { label: "Follow-up", query: { followUp: true } },
  { label: "Retry pending", query: { status: "retry_pending" } },
  { label: "Unreachable", query: { status: "no_answer,unreachable" } },
  // Filter on how the last call ENDED, not on the lead's state. A busy number
  // and a voicemail both sit at status retry_pending, so they were invisible
  // until now — there was no filter in the app that could single them out.
  { label: "Busy", query: { outcome: "busy" } },
  { label: "No answer", query: { outcome: "no_answer" } },
  { label: "Voicemail", query: { outcome: "voicemail" } },
]

/** call_logs.outcome -> how it reads in the UI. */
const OUTCOME_LABELS: Record<string, string> = {
  completed: "Connected",
  no_answer: "No answer",
  busy: "Busy",
  voicemail: "Voicemail",
  failed: "Failed",
  cancelled: "Cancelled",
}

function formatDuration(seconds?: number | null) {
  const value = Number(seconds)
  if (!Number.isFinite(value) || value <= 0) return "0:00"
  return `${Math.floor(value / 60)}:${String(Math.round(value % 60)).padStart(2, "0")}`
}

function formatDateTime(value?: string | null) {
  if (!value) return "—"
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString()
}

function formatDate(value?: string | null) {
  if (!value) return "—"
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleDateString()
}

/**
 * Short timestamp for the phone cards: "19 Aug, 11:03 am".
 *
 * formatDateTime's full form ("8/19/2026, 11:03:04 AM") is 22 characters, which
 * on a 390px card leaves the caller's name about half the row and truncates
 * people like "Anreddysammireddy" to nothing useful. Seconds and the year are
 * the parts nobody reads on a call list, so they are the parts that go.
 */
function formatDateTimeShort(value?: string | null) {
  if (!value) return "—"
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return "—"
  return d
    .toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "numeric", minute: "2-digit", hour12: true })
    .replace(/\s?([ap])\.?m\.?/i, (_m, p) => ` ${p.toLowerCase()}m`)
}

function initialsOf(name?: string | null) {
  if (!name) return "UK"
  return name
    .split(" ")
    .filter(Boolean)
    .map((word) => word[0])
    .join("")
    .substring(0, 2)
    .toUpperCase()
}

/** Digits only, so tel:/wa.me links work regardless of how the number was stored. */
function dialable(phone?: string | null) {
  return (phone || "").replace(/[^\d+]/g, "")
}

/** Time-of-day greeting, so the hero is not permanently stuck on "morning". */
function greetingFor(date: Date) {
  const hour = date.getHours()
  if (hour < 12) return "Good morning"
  if (hour < 17) return "Good afternoon"
  return "Good evening"
}

/**
 * Counts a figure up to its value on mount, then tracks it thereafter.
 *
 * Driven by rAF against wall-clock time (not a fixed per-frame step), so the
 * duration holds regardless of refresh rate and it cannot overshoot. Skips
 * straight to the final value when the OS asks for reduced motion, and for
 * subsequent live updates — a number that re-counts on every 15s poll would be
 * unreadable, so only the first paint animates.
 */
function useCountUp(value: number, durationMs = 900) {
  const [display, setDisplay] = useState(0)
  const hasAnimated = useRef(false)

  useEffect(() => {
    const reduced =
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches

    if (reduced || hasAnimated.current || !Number.isFinite(value) || value === 0) {
      setDisplay(value)
      hasAnimated.current = true
      return
    }

    hasAnimated.current = true
    const from = 0
    const start = performance.now()
    let raf = 0

    const tick = (now: number) => {
      const t = Math.min((now - start) / durationMs, 1)
      // expo-out, matching the easing token the rest of the product uses.
      const eased = t === 1 ? 1 : 1 - Math.pow(2, -10 * t)
      setDisplay(Math.round(from + (value - from) * eased))
      if (t < 1) raf = requestAnimationFrame(tick)
    }

    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [value, durationMs])

  // Live updates after the intro land immediately rather than re-counting.
  useEffect(() => {
    if (hasAnimated.current) setDisplay(value)
  }, [value])

  return display
}

/** A single headline figure with its label, used across the stat rows. */
function StatFigure({
  icon: Icon,
  value,
  label,
  hint,
  raw,
}: {
  icon: React.ElementType
  value: number
  label: string
  hint?: React.ReactNode
  /** Pass a pre-formatted string (e.g. a duration) to skip the count-up. */
  raw?: string
}) {
  const counted = useCountUp(value)
  return (
    <Card className="hover-lift group/stat">
      <CardContent className="p-5">
        <div className="flex items-start justify-between">
          <div className="flex size-10 items-center justify-center rounded-lg bg-accent text-accent-foreground transition-colors duration-300 group-hover/stat:bg-primary group-hover/stat:text-primary-foreground">
            <Icon className="size-4" />
          </div>
          {hint}
        </div>
        <p className="mt-5 font-display text-3xl font-semibold tracking-tight tabular-nums">
          {raw ?? counted.toLocaleString()}
        </p>
        <p className="mt-1.5 text-xs tracking-wide text-muted-foreground">{label}</p>
      </CardContent>
    </Card>
  )
}

/**
 * A lead's business line, from the `vertical` column set at upload time.
 *
 * The qual_data fallback is kept deliberately: it is where this used to be read
 * from, and leads created before the column existed still carry it there. Every
 * lead gets a real column value from the migration onward, so this fallback only
 * ever matters for history.
 */
function verticalOf(lead: any): Vertical {
  return parseVertical(lead?.vertical) ?? parseVertical(lead?.qual_data?.vertical) ?? DEFAULT_VERTICAL
}

function verticalLabelOf(lead: any) {
  return VERTICAL_LABELS[verticalOf(lead)]
}

function verticalStyle(lead: any) {
  return VERTICAL_STYLES[verticalOf(lead)]
}

/**
 * What the caller asked for — "Home loan", "Business loan".
 *
 * The webhook stores the extracted loan type on `property_type` (the column
 * predates the loan agent and was named for the real-estate workflow), and also
 * inside qual_data. Read both so a lead scored before either path existed still
 * shows something.
 */
function loanTypeOf(lead: any): string | null {
  const raw = lead?.property_type ?? lead?.qual_data?.loan_type
  if (!raw) return null
  const s = String(raw).trim()
  if (!s) return null
  const titled = s.charAt(0).toUpperCase() + s.slice(1)
  return /loan/i.test(titled) ? titled : `${titled} loan`
}

/**
 * "Own house" / "Rented house".
 *
 * Read from the column first and qual_data second: the column only exists once
 * 007_solar_fields.sql has been run, and any lead scored before that has the
 * answer only in qual_data. Both paths are written by the same webhook.
 */
function houseOf(lead: any): string | null {
  const raw = lead?.house_ownership ?? lead?.qual_data?.house_ownership
  if (raw === "own") return "Own house"
  if (raw === "rent") return "Rented house"
  return null
}

/** "Planning solar" / "Not planning" — null when they never got to that question. */
function solarPlanOf(lead: any): string | null {
  const raw = lead?.solar_planning ?? lead?.qual_data?.solar_planning
  if (raw === true) return "Planning solar"
  if (raw === false) return "Not planning"
  return null
}

/**
 * "SIP", "Mutual fund", "FD" — what the caller said they already put money into.
 *
 * Investing has no columns of its own on the leads table and does not need any:
 * the webhook writes both answers into qual_data, which every lead has. Nothing
 * to run in the SQL editor before this shows up.
 */
function investmentTypeOf(lead: any): string | null {
  const raw = lead?.qual_data?.investment_type
  if (!raw) return null
  const s = String(raw).trim()
  if (!s) return null
  return s.charAt(0).toUpperCase() + s.slice(1)
}

/** "Already investing · SIP" / "Not investing yet" — null when never answered. */
function investingOf(lead: any): string | null {
  const raw = lead?.qual_data?.currently_investing
  const type = investmentTypeOf(lead)
  if (raw === true) return type ? `Already investing · ${type}` : "Already investing"
  if (raw === false) return "Not investing yet"
  // The agent sometimes names the product without ever setting the flag; naming
  // one IS the answer, so do not show a blank for it.
  return type ? `Already investing · ${type}` : null
}

/**
 * The investing agent's second signal: did they agree to the advisor calling.
 * "Yes" / "No", never a blank guess — null means they never got that far.
 */
function advisorInterestOf(lead: any): string | null {
  const raw = lead?.qual_data?.interested
  if (raw === true) return "Yes"
  if (raw === false) return "No"
  return null
}

/**
 * Real estate has no columns of its own on the leads table, same as investing:
 * the webhook writes all four answers into qual_data, which every lead has.
 */
function propertyTypeOf(lead: any): string | null {
  const raw = lead?.qual_data?.property_type
  if (!raw) return null
  const s = String(raw).trim()
  if (!s) return null
  return s.charAt(0).toUpperCase() + s.slice(1)
}

function dealTypeOf(lead: any): string | null {
  const raw = lead?.qual_data?.deal_type
  if (!raw) return null
  const s = String(raw).trim()
  if (!s) return null
  return s.charAt(0).toUpperCase() + s.slice(1)
}

function locationOf(lead: any): string | null {
  const raw = lead?.qual_data?.location
  if (!raw) return null
  const s = String(raw).trim()
  return s || null
}

/** "Plot · Buy" / "Independent house" — null when neither was ever said. */
function propertyWantOf(lead: any): string | null {
  const type = propertyTypeOf(lead)
  const deal = dealTypeOf(lead)
  return [type, deal].filter(Boolean).join(" · ") || null
}

/**
 * The one fact that matters about a lead — which is a different fact per
 * business line. A loan lead's is the loan type; a solar lead's is whether the
 * house is theirs and whether they are planning solar; an investing lead's is
 * whether they already invest and whether they want the advisor call. Reading
 * the loan type on a solar lead was showing "Own house loan", which is not a
 * thing anyone sells — and on an investing lead it showed nothing at all.
 */
function requirementOf(lead: any): string | null {
  if (verticalOf(lead) === "solar") {
    return [houseOf(lead), solarPlanOf(lead)].filter(Boolean).join(" · ") || null
  }
  if (verticalOf(lead) === "investing") {
    const interest = advisorInterestOf(lead)
    return [investingOf(lead), interest ? `advisor: ${interest.toLowerCase()}` : null].filter(Boolean).join(" · ") || null
  }
  if (verticalOf(lead) === "realestate") {
    return [propertyWantOf(lead), locationOf(lead)].filter(Boolean).join(" · ") || null
  }
  return loanTypeOf(lead)
}

/**
 * The two middle table columns, per business line.
 *
 * Each agent asks its own questions, so "Requirement / Budget" only describes a
 * loan lead. Rather than a `solar` boolean — which had to be re-plumbed through
 * every table the moment a third agent shipped — each line names its own pair of
 * headers and how to read them off a lead. `null` means the default loan pair.
 */
type LineColumns = { headers: [string, string]; values: (lead: any) => [string | null, string | null] }

const LINE_COLUMNS: Partial<Record<Vertical, LineColumns>> = {
  solar: {
    headers: ["House", "Solar plan"],
    values: (lead) => [houseOf(lead), solarPlanOf(lead)],
  },
  investing: {
    headers: ["Investing", "Advisor interest"],
    values: (lead) => [investingOf(lead), advisorInterestOf(lead)],
  },
  realestate: {
    headers: ["Property", "Location"],
    values: (lead) => [propertyWantOf(lead), locationOf(lead)],
  },
}

/**
 * Only when ONE business line is selected. Under "All" the table mixes lines and
 * a single set of headers cannot honestly describe all four, so it keeps the
 * loan pair and the per-lead facts stay in the Requirement column.
 */
function lineColumnsFor(vertical: VerticalFilter): LineColumns | null {
  return vertical === "all" ? null : LINE_COLUMNS[vertical] ?? null
}

/**
 * Why the lead has the score it has, and who decided it. Written by the webhook
 * into qual_data.scoring. Older leads predate it and get a plain fallback rather
 * than an invented explanation.
 */
function scoreReasonOf(lead: any): string {
  const s = lead?.qual_data?.scoring
  if (!s?.reason) return "Scored before reasons were recorded"
  const who = s.scored_by === "agent" ? "the agent" : s.scored_by === "none" ? "no scoring" : "scoring rules"
  return `${s.reason} — decided by ${who}`
}

function getScoreLabel(score: number) {
  if (score >= 80) return { label: "Hot", color: "text-red-500", bg: "bg-red-500/10" }
  if (score >= 60) return { label: "Warm", color: "text-orange-500", bg: "bg-orange-500/10" }
  if (score >= 40) return { label: "Cool", color: "text-blue-500", bg: "bg-blue-500/10" }
  return { label: "Cold", color: "text-slate-500", bg: "bg-slate-500/10" }
}

/** One display label per lead, derived from the real status + qualification columns. */
function leadStatusLabel(lead: any): string {
  if (lead?.qualification === "qualified") return "Qualified"
  switch (lead?.status) {
    case "new":
      return "New"
    case "queued":
      return "Queued"
    case "called":
      return lead?.qualification === "not_qualified" ? "Not qualified" : "Called"
    case "retry_pending":
      return "Retry pending"
    case "no_answer":
      return "No answer"
    case "unreachable":
      return "Unreachable"
    default:
      return lead?.status || "New"
  }
}

function StatusBadge({ status }: { status: string }) {
  const strong = status === "Qualified"
  const weak = status === "Unreachable" || status === "No answer" || status === "Not qualified"
  return <Badge variant={strong ? "default" : weak ? "outline" : "secondary"}>{status}</Badge>
}

function CallOutcomeIcon({ outcome }: { outcome?: string | null }) {
  if (outcome === "failed" || outcome === "cancelled") return <PhoneMissed className="size-4 text-destructive" />
  if (outcome === "no_answer" || outcome === "busy" || outcome === "voicemail")
    return <PhoneOff className="size-4 text-muted-foreground" />
  return <PhoneForwarded className="size-4 text-primary" />
}

function CampaignStatusBadge({ status }: { status: string }) {
  if (status === "running" || status === "active") return <Badge variant="default">Active</Badge>
  if (status === "paused") return <Badge variant="secondary">Paused</Badge>
  if (status === "failed") return <Badge variant="outline">Failed</Badge>
  if (status === "pending" || status === "queued") return <Badge variant="secondary">Starting</Badge>
  return <Badge variant="outline">Completed</Badge>
}

function EmptyRow({ colSpan, children }: { colSpan: number; children: React.ReactNode }) {
  return (
    <TableRow>
      <TableCell colSpan={colSpan} className="py-8 text-center text-muted-foreground">
        {children}
      </TableCell>
    </TableRow>
  )
}

/**
 * Shared lead table row, used by the overview, leads and follow-up tables.
 *
 * `line` switches the two middle columns from the loan pair (requirement,
 * budget) to whatever the selected business line actually asks — the solar pair
 * (house, plan), the investing pair (investing, advisor interest). It follows
 * the header's business-line switch, not the individual lead: a table has one
 * set of headers, so the columns can only match the line being viewed.
 */
function LeadRow({
  lead,
  onSelect,
  columns,
  line = null,
  onCallPlaced,
  selected,
  onToggleSelect,
}: {
  lead: any
  onSelect: (lead: any) => void
  columns: "compact" | "full"
  line?: LineColumns | null
  onCallPlaced?: () => void
  /** Selection is opt-in: passing onToggleSelect adds the tick column. The
   *  Overview and Follow-ups tables share this row and have no bulk delete, so
   *  they simply do not pass it and their column counts stay as they were. */
  selected?: boolean
  onToggleSelect?: (id: string) => void
}) {
  const score = lead.score ?? 0
  const scoreData = getScoreLabel(score)
  return (
    <TableRow className="cursor-pointer hover:bg-muted/50" onClick={() => onSelect(lead)}>
      {onToggleSelect && (
        <TableCell className="w-10">
          <RowCheckbox
            checked={!!selected}
            onChange={() => onToggleSelect(lead.id)}
            label={`Select ${lead.name || lead.phone}`}
          />
        </TableCell>
      )}
      <TableCell>
        <div className="flex items-center gap-3">
          <Avatar className="size-8">
            <AvatarFallback>{initialsOf(lead.name)}</AvatarFallback>
          </Avatar>
          <div>
            <p className="font-medium">{lead.name || "Unknown"}</p>
            <p className="text-xs text-muted-foreground">{lead.phone}</p>
          </div>
        </div>
      </TableCell>
      {/*
        Hidden in step with its header — see the column-priority note there.

        Dropped entirely in `compact`, which is the Overview card. That card
        lives in a two-column grid, so it is ~620px wide no matter how big the
        screen is; a viewport-based breakpoint cannot help it, because the
        viewport is not what is squeezing it. Five columns measured 741px in
        that 624px card even on a laptop. Source is the one the Overview needs
        least — it is a six-row "what happened lately" glance, not a filterable
        list — so it goes, and the remaining four fit.
      */}
      <TableCell className={cn("hidden", columns === "full" && "xl:table-cell")}>
        <span className="flex items-center gap-2 text-sm">
          {lead.source === "Excel" || lead.source === "CSV Upload" ? (
            <FileSpreadsheet className="size-4 shrink-0 text-muted-foreground" />
          ) : lead.source === "Website" ? (
            <Globe className="size-4 shrink-0 text-muted-foreground" />
          ) : (
            <Megaphone className="size-4 shrink-0 text-muted-foreground" />
          )}
          {lead.source || "Unknown"}
        </span>
      </TableCell>
      {columns === "full" && (
        <>
          <TableCell className="hidden 2xl:table-cell">
            <span className="text-sm text-muted-foreground">{lead.city || "—"}</span>
          </TableCell>
          {/* Solar asks about the house, investing asks what they already put
              money into — neither is a loan type or a budget, so on those views
              those two columns ARE that line's answers. */}
          {line ? (
            <>
              <TableCell>
                <span className="text-sm">{line.values(lead)[0] || "—"}</span>
              </TableCell>
              <TableCell>
                <span className="text-sm">{line.values(lead)[1] || "—"}</span>
              </TableCell>
            </>
          ) : (
            <>
              {/* What the caller actually asked for. The single most useful fact
                  about the lead, so it earns its own column rather than hiding
                  in the detail drawer. */}
              <TableCell>
                <span className="text-sm">{requirementOf(lead) || "—"}</span>
              </TableCell>
              <TableCell>
                <span className="text-sm">{lead.budget || "—"}</span>
              </TableCell>
            </>
          )}
        </>
      )}
      <TableCell>
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-1.5">
            {/* title= carries the reason, so hovering a score answers "why 50?" */}
            <Badge
              variant="outline"
              className={`${scoreData.color} ${scoreData.bg} border-transparent`}
              title={scoreReasonOf(lead)}
            >
              {score > 0 ? `${scoreData.label} · ${score}` : "Unscored"}
            </Badge>
            {/* Which business line earned that score. Once solar, real estate and
                investing agents exist, one lead list holds all four and a bare
                number says nothing about what the lead actually wanted. */}
            <Badge variant="outline" className={`${verticalStyle(lead).color} ${verticalStyle(lead).bg} border-transparent`}>
              {verticalLabelOf(lead)}
            </Badge>
          </div>
          {/* The single most useful fact about the lead, visible without opening
              the row: the loan they asked for, or the solar house/plan answer. */}
          {requirementOf(lead) && (
            <span className="text-xs text-muted-foreground">{requirementOf(lead)}</span>
          )}
        </div>
      </TableCell>
      <TableCell>
        <StatusBadge status={leadStatusLabel(lead)} />
      </TableCell>
      <TableCell className="whitespace-nowrap text-right text-muted-foreground">
        {/* The Call button lives in the existing last cell rather than a new
            column so both lead tables (and their empty-row colSpans) stay valid.
            It stops row-click propagation itself, so it cannot open the panel. */}
        <div className="flex items-center justify-end gap-3">
          <span>{formatDate(lead.last_attempt_at || lead.created_at)}</span>
          <CallLeadButton lead={lead} onPlaced={onCallPlaced} />
        </div>
      </TableCell>
    </TableRow>
  )
}

/**
 * A lead as a card, for phones.
 *
 * The table this replaces below `md` had nine columns. At 390px exactly two of
 * them were on screen — Lead and Source — and Score and Status, which are the
 * two the client actually works from, sat behind a horizontal scroll inside the
 * card that nothing advertised. Reflowing the same fields into a stack fixes
 * that without hiding anything: everything the wide table shows is here, just
 * arranged down instead of across.
 *
 * These live in this file rather than their own because they read eight local
 * helpers (score labels, vertical styling, requirement extraction, status
 * wording). Exporting all eight to save 120 lines here would put the field
 * vocabulary in two places, and the wide table and the card MUST describe a lead
 * identically or the same row reads differently on a phone and a laptop.
 */
function LeadCardMobile({
  lead,
  onSelect,
  line = null,
  onCallPlaced,
  selectable,
  selected,
  onToggleSelect,
}: {
  lead: any
  onSelect: (lead: any) => void
  line?: LineColumns | null
  onCallPlaced?: () => void
  selectable: boolean
  selected: boolean
  onToggleSelect: (id: string) => void
}) {
  const score = lead.score ?? 0
  const scoreData = getScoreLabel(score)
  const [detail, detail2] = line ? line.values(lead) : [requirementOf(lead), lead.budget]

  return (
    <div
      role="button"
      tabIndex={0}
      // While selecting, the whole card is the tick. A 16px checkbox is below
      // every touch-target guideline there is, and asking someone to hit one
      // repeatedly on a moving bus is how you delete the wrong lead.
      onClick={() => (selectable ? onToggleSelect(lead.id) : onSelect(lead))}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault()
          selectable ? onToggleSelect(lead.id) : onSelect(lead)
        }
      }}
      aria-pressed={selectable ? selected : undefined}
      className={cn(
        "flex w-full cursor-pointer flex-col gap-3 border-b border-border/70 p-4 text-left transition-colors last:border-b-0",
        selected ? "bg-primary/5" : "active:bg-muted/60",
      )}
    >
      <div className="flex items-start gap-3">
        {selectable && (
          <span className="mt-0.5 shrink-0">
            <RowCheckbox
              checked={selected}
              onChange={() => onToggleSelect(lead.id)}
              label={`Select ${lead.name || lead.phone}`}
            />
          </span>
        )}
        <Avatar className="size-9 shrink-0">
          <AvatarFallback>{initialsOf(lead.name)}</AvatarFallback>
        </Avatar>
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium">{lead.name || "Unknown"}</p>
          <p className="truncate text-xs text-muted-foreground">{lead.phone}</p>
        </div>
        <StatusBadge status={leadStatusLabel(lead)} />
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        <Badge
          variant="outline"
          className={`${scoreData.color} ${scoreData.bg} border-transparent`}
          title={scoreReasonOf(lead)}
        >
          {score > 0 ? `${scoreData.label} · ${score}` : "Unscored"}
        </Badge>
        <Badge
          variant="outline"
          className={`${verticalStyle(lead).color} ${verticalStyle(lead).bg} border-transparent`}
        >
          {verticalLabelOf(lead)}
        </Badge>
      </div>

      {(detail || detail2) && (
        <p className="truncate text-sm text-muted-foreground">
          {[detail, detail2].filter(Boolean).join(" · ")}
        </p>
      )}

      <div className="flex items-center justify-between gap-3">
        <span className="truncate text-xs text-muted-foreground">
          {lead.city ? `${lead.city} · ` : ""}
          {formatDate(lead.last_attempt_at || lead.created_at)}
        </span>
        {/* Hidden while selecting: a Call button inside a row you are trying to
            tick is a real call placed by a mis-tap, and this one dials for money. */}
        {!selectable && <CallLeadButton lead={lead} onPlaced={onCallPlaced} />}
      </div>
    </div>
  )
}

/** A call log as a card, for phones. Same reasoning as LeadCardMobile. */
function CallCardMobile({
  call,
  onSelect,
  selectable,
  selected,
  onToggleSelect,
}: {
  call: any
  onSelect: (lead: any, callId?: string | null) => void
  selectable: boolean
  selected: boolean
  onToggleSelect: (id: string) => void
}) {
  const openable = !!call.leads
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => (selectable ? onToggleSelect(call.id) : openable && onSelect(call.leads, call.id))}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault()
          selectable ? onToggleSelect(call.id) : openable && onSelect(call.leads, call.id)
        }
      }}
      aria-pressed={selectable ? selected : undefined}
      className={cn(
        "flex w-full flex-col gap-2 border-b border-border/70 p-4 text-left transition-colors last:border-b-0",
        selected ? "bg-primary/5" : "active:bg-muted/60",
        (selectable || openable) && "cursor-pointer",
      )}
    >
      <div className="flex items-start gap-3">
        {selectable && (
          <span className="mt-0.5 shrink-0">
            <RowCheckbox
              checked={selected}
              onChange={() => onToggleSelect(call.id)}
              label={`Select call to ${call.leads?.name || call.leads?.phone || "unknown"}`}
            />
          </span>
        )}
        <span className="mt-0.5 shrink-0">
          <CallOutcomeIcon outcome={call.outcome} />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium">{call.leads?.name || "Unknown"}</p>
          <p className="truncate text-xs text-muted-foreground">{call.leads?.phone || ""}</p>
        </div>
        <span className="shrink-0 text-xs text-muted-foreground">{formatDateTimeShort(call.called_at)}</span>
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 ps-8 text-xs text-muted-foreground">
        <span>{OUTCOME_LABELS[call.outcome] || call.outcome || "—"}</span>
        <span className="font-mono">{formatDuration(call.duration)}</span>
        <span>Attempt #{call.attempt_no ?? 1}</span>
        {call.recording_url && !selectable && (
          <a
            href={`/api/calls/${call.id}/recording`}
            target="_blank"
            rel="noreferrer"
            onClick={(e) => e.stopPropagation()}
            // -my-2 py-2 gives the link a 36px tap height without adding 16px of
            // space to the row: as bare text it was 16px tall, the smallest
            // target left in the app and the one people reach for most.
            className="-my-2 inline-flex min-h-9 items-center py-2 font-medium text-primary underline underline-offset-4"
          >
            Play recording
          </a>
        )}
      </div>
    </div>
  )
}

function Pager({
  page,
  totalPages,
  totalCount,
  noun,
  onChange,
}: {
  page: number
  totalPages: number
  totalCount: number
  noun: string
  onChange: (page: number) => void
}) {
  if (totalPages <= 1) return null
  return (
    <div className="flex items-center justify-between">
      <p className="text-sm text-muted-foreground">
        Page {page} of {totalPages} ({totalCount} {noun})
      </p>
      <div className="flex items-center gap-2">
        <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => onChange(page - 1)}>
          Previous
        </Button>
        <Button variant="outline" size="sm" disabled={page >= totalPages} onClick={() => onChange(page + 1)}>
          Next
        </Button>
      </div>
    </div>
  )
}

// ─── PAGE COMPONENTS ──────────────────────────────────

function HealthBadge({ service }: { service?: { state: string; detail: string } }) {
  if (!service) return <Badge variant="outline">Checking…</Badge>
  if (service.state === "connected") return <Badge variant="outline">Connected</Badge>
  if (service.state === "error") return <Badge variant="destructive">Error</Badge>
  return <Badge variant="secondary">Not configured</Badge>
}

function OverviewPage({
  range,
  setRange,
  leads,
  onSelectLead,
  statusFilter,
  setStatusFilter,
  setActiveNav,
  exportReport,
  leadStats,
  callStats,
  chartData,
  health,
  isRefreshing,
}: {
  range: string
  setRange: (v: string) => void
  leads: any[]
  onSelectLead: (lead: any, callId?: string | null) => void
  statusFilter: string
  setStatusFilter: (v: string) => void
  setActiveNav: (v: string) => void
  exportReport: () => void
  leadStats: any
  callStats: any
  chartData: { day: string; calls: number; qualified: number }[]
  health: any
  isRefreshing: boolean
}) {
  const totalLeads = leadStats?.total ?? 0
  const qualifiedLeads = leadStats?.qualified ?? 0
  const totalCalls = callStats?.total ?? 0
  const connectedCalls = callStats?.connected ?? 0
  const connectRate = totalCalls > 0 ? Math.round((connectedCalls / totalCalls) * 100) : 0

  return (
    <>
      {/* Hero. Editorial rather than dashboard-standard: the greeting is set in
          the display Didone at a size that earns the space, and a warm ambient
          wash drifts behind it so the top of the page has depth instead of
          being a bare heading on flat paper. */}
      <section className="ambient-wash relative -mx-4 -mt-4 overflow-hidden border-b border-border/60 px-4 pb-8 pt-10 md:-mx-8 md:-mt-8 md:px-8 md:pb-10 md:pt-14">
        <div className="relative z-10 flex flex-col gap-6 lg:flex-row lg:items-end lg:justify-between">
          <div className="min-w-0">
            <div className="motion-fade flex items-center gap-2.5">
              <span className="relative flex size-1.5">
                <span className="pulse-soft absolute inline-flex size-full rounded-full bg-primary" />
                <span className="relative inline-flex size-1.5 rounded-full bg-primary" />
              </span>
              <span className="text-[10px] font-semibold uppercase tracking-[0.2em] text-muted-foreground">
                Live · updates every 15s
              </span>
              {isRefreshing && <RefreshCw className="size-3 animate-spin text-muted-foreground" />}
            </div>

            <h1 className="motion-rise mt-4 text-balance font-display text-4xl font-semibold leading-[1.05] tracking-tight md:text-5xl lg:text-6xl">
              {greetingFor(new Date())},
              <span className="text-muted-foreground"> team</span>
            </h1>

            <p className="motion-rise mt-4 max-w-[52ch] text-pretty text-sm leading-relaxed text-muted-foreground md:text-base">
              Your agent has placed{" "}
              <span className="font-medium text-foreground tabular-nums">{totalCalls.toLocaleString()}</span>{" "}
              {totalCalls === 1 ? "call" : "calls"} and qualified{" "}
              <span className="font-medium text-foreground tabular-nums">{qualifiedLeads.toLocaleString()}</span>{" "}
              {qualifiedLeads === 1 ? "lead" : "leads"} so far.
            </p>
          </div>

          <div className="motion-fade flex shrink-0 items-center gap-3">
            <Tabs value={range} onValueChange={setRange}>
              <TabsList>
                <TabsTrigger value="24h">24h</TabsTrigger>
                <TabsTrigger value="7d">7 days</TabsTrigger>
                <TabsTrigger value="30d">30 days</TabsTrigger>
              </TabsList>
            </Tabs>
          </div>
        </div>
      </section>

      {health && !health.healthy && (
        <Card className="border-destructive/40 bg-destructive/5">
          <CardContent className="flex flex-col gap-2 p-4">
            <div className="flex items-center gap-2 text-sm font-medium text-destructive">
              <AlertTriangle className="size-4" />
              Some integrations need attention
            </div>
            {Object.entries(health.services as Record<string, any>)
              .filter(([, service]) => service.state !== "connected")
              .map(([name, service]) => (
                <p key={name} className="text-xs text-muted-foreground">
                  <span className="font-medium capitalize">{name}</span>: {service.detail}
                </p>
              ))}
          </CardContent>
        </Card>
      )}

      {/* Skeletons shaped like the cards they stand in for, so the swap when
          data lands is a fade rather than the layout jumping into place. */}
      {!leadStats && !callStats ? (
        <StatSkeletonRow />
      ) : (
      <section className="stagger grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatFigure icon={Users} value={totalLeads} label="Total leads" />
        <StatFigure
          icon={PhoneCall}
          value={totalCalls}
          label="Calls made"
          hint={
            totalCalls > 0 ? (
              <span className="flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">
                {connectRate}% connect
                <ArrowUpRight className="size-3" />
              </span>
            ) : undefined
          }
        />
        <StatFigure icon={Target} value={qualifiedLeads} label="Qualified" />
        <StatFigure
          icon={CircleDollarSign}
          value={0}
          raw={formatDuration(callStats?.avg_duration)}
          label="Avg. connected call"
        />
      </section>
      )}

      <section className="grid gap-4 lg:grid-cols-[1.55fr_1fr]">
        <Card>
          <CardHeader className="flex flex-row items-start justify-between gap-3">
            <div>
              <CardTitle>Call performance</CardTitle>
              <CardDescription>Daily outreach and qualification trend</CardDescription>
            </div>
            <Button variant="ghost" size="sm" onClick={exportReport}>
              <Download data-icon="inline-start" />
              Export
            </Button>
          </CardHeader>
          <CardContent>
            {chartData.length === 0 || chartData.every((d) => d.calls === 0) ? (
              <div className="flex h-60 items-center justify-center text-sm text-muted-foreground">
                No calls in this period yet.
              </div>
            ) : (
              <ChartContainer config={chartConfig} className="draw-line h-64 w-full">
                <AreaChart data={chartData} margin={{ left: -20, right: 4, top: 10 }}>
                  <defs>
                    <linearGradient id="calls-fill" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="var(--color-calls)" stopOpacity={0.22} />
                      <stop offset="95%" stopColor="var(--color-calls)" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid vertical={false} />
                  <XAxis dataKey="day" tickLine={false} axisLine={false} tickMargin={10} />
                  <YAxis tickLine={false} axisLine={false} allowDecimals={false} />
                  <ChartTooltip cursor={false} content={<ChartTooltipContent indicator="line" />} />
                  <Area type="monotone" dataKey="calls" stroke="var(--color-calls)" fill="url(#calls-fill)" strokeWidth={2} />
                  <Area type="monotone" dataKey="qualified" stroke="var(--color-qualified)" fill="transparent" strokeWidth={2} />
                </AreaChart>
              </ChartContainer>
            )}
            <div className="mt-3 flex items-center gap-6 text-xs text-muted-foreground">
              <span className="flex items-center gap-2">
                <i className="size-2 rounded-full bg-chart-1" />
                Total calls
              </span>
              <span className="flex items-center gap-2">
                <i className="size-2 rounded-full bg-chart-2" />
                Qualified
              </span>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Funnel</CardTitle>
            <CardDescription>From {totalLeads} total leads</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-5">
            {[
              { label: "Total leads", value: totalLeads, percent: 100 },
              { label: "Calls made", value: totalCalls, percent: totalLeads > 0 ? Math.round((totalCalls / totalLeads) * 100) : 0 },
              { label: "Connected", value: connectedCalls, percent: connectRate },
              {
                label: "Qualified",
                value: qualifiedLeads,
                percent: totalLeads > 0 ? Math.round((qualifiedLeads / totalLeads) * 100) : 0,
              },
            ].map((step) => (
              <div key={step.label} className="flex flex-col gap-2">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted-foreground">{step.label}</span>
                  <span className="font-medium">{step.value}</span>
                </div>
                <Progress value={Math.min(step.percent, 100)} />
              </div>
            ))}
            <div className="flex items-center justify-between rounded-lg bg-primary/10 p-3">
              <div>
                <p className="text-sm font-medium tabular-nums">
                  {totalLeads > 0 ? ((qualifiedLeads / totalLeads) * 100).toFixed(1) : "0"}% qualification
                </p>
                <p className="text-xs text-muted-foreground">From all leads</p>
              </div>
              <Target className="size-5 text-primary" />
            </div>
          </CardContent>
        </Card>
      </section>

      <section className="grid gap-4 lg:grid-cols-[1.55fr_1fr]">
        <Card className="min-w-0">
          <CardHeader className="flex flex-row items-center justify-between gap-3">
            <div>
              <CardTitle>Recent leads</CardTitle>
              <CardDescription>Latest activity across all sources</CardDescription>
            </div>
            <div className="flex gap-2">
              <DropdownMenu>
                {/* base-ui uses `render`, not Radix's `asChild`. With asChild the
                    trigger rendered a <button> wrapping another <button>, which is
                    invalid HTML and swallowed the click on some browsers. */}
                <DropdownMenuTrigger render={<Button variant="outline" size="sm" />}>
                  <ListFilter data-icon="inline-start" />
                  {statusFilter === "All" ? "Filter" : statusFilter}
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  {LEAD_FILTERS.map((option) => (
                    <DropdownMenuItem key={option.label} onClick={() => setStatusFilter(option.label)}>
                      {option.label}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
              <Button variant="ghost" size="sm" onClick={() => setActiveNav("Leads")}>
                View all
              </Button>
            </div>
          </CardHeader>
          {/* Phones get the same card the Leads page uses. This card sits in a
              two-column grid, so even on a laptop it only gets ~620px — a
              five-column table never fitted that and scrolled sideways inside
              its own card, which is the least discoverable scroll there is. */}
          <CardContent className="p-0 md:hidden">
            {leads.length === 0 ? (
              <p className="p-6 text-center text-sm text-muted-foreground">
                No leads yet. Use “Import leads” to upload a CSV.
              </p>
            ) : (
              <div className="stagger-rows flex flex-col">
                {leads.slice(0, 6).map((lead) => (
                  <LeadCardMobile
                    key={lead.id}
                    lead={lead}
                    onSelect={onSelectLead}
                    selectable={false}
                    selected={false}
                    onToggleSelect={() => {}}
                  />
                ))}
              </div>
            )}
          </CardContent>

          <CardContent className="hidden overflow-x-auto p-0 md:block">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Lead</TableHead>
                  {/* No Source header here: LeadRow drops that cell entirely in
                      compact mode. A header that outlives its column shifts
                      every cell in the row one place to the left. */}
                  <TableHead>Score</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="text-right">Last activity</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody className="stagger-rows">
                {leads.length === 0 ? (
                  <EmptyRow colSpan={4}>No leads yet. Use “Import leads” to upload a CSV.</EmptyRow>
                ) : (
                  leads.slice(0, 6).map((lead) => (
                    <LeadRow key={lead.id} lead={lead} onSelect={onSelectLead} columns="compact" />
                  ))
                )}
              </TableBody>
            </Table>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>System status</CardTitle>
            <CardDescription>Live integration checks</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <div className="grid grid-cols-2 gap-3">
              <div className="rounded-lg bg-secondary p-3">
                <p className="text-xs text-muted-foreground">New leads</p>
                <p className="mt-1 text-xl font-semibold">{leadStats?.new_leads ?? 0}</p>
              </div>
              <div className="rounded-lg bg-secondary p-3">
                <p className="text-xs text-muted-foreground">Calls today</p>
                <p className="mt-1 text-xl font-semibold">{callStats?.today ?? 0}</p>
              </div>
            </div>
            {/* These badges used to be hard-coded to "Connected" and stayed green
                even with no credentials configured at all. */}
            {[
              { key: "supabase", label: "Supabase" },
              { key: "dograh", label: "Dograh" },
              { key: "webhook", label: "Call-result webhook" },
              { key: "n8n", label: "n8n import" },
              { key: "cron", label: "Reconcile job" },
              { key: "auth", label: "Login protection" },
            ].map((row) => (
              <div key={row.key} className="flex items-center justify-between gap-2 text-sm">
                <span className="text-muted-foreground">{row.label}</span>
                <HealthBadge service={health?.services?.[row.key]} />
              </div>
            ))}
          </CardContent>
        </Card>
      </section>
    </>
  )
}

function LeadsPage({
  leads,
  onSelectLead,
  statusFilter,
  setStatusFilter,
  totalCount,
  page,
  setPage,
  totalPages,
  leadStats,
  isLoading,
  vertical,
  onCallPlaced,
  deleteFilters,
  onDeleted,
  dateRange,
  setDateRange,
}: {
  leads: any[]
  onSelectLead: (lead: any, callId?: string | null) => void
  statusFilter: string
  setStatusFilter: (v: string) => void
  totalCount: number
  page: number
  setPage: (v: number) => void
  totalPages: number
  leadStats: any
  isLoading: boolean
  vertical: VerticalFilter
  onCallPlaced?: () => void
  /** Exactly the filter the list was fetched with, in the API's parameter
   *  names, so the delete acts on the same set the table is showing. */
  deleteFilters: Record<string, any>
  onDeleted: () => void
  dateRange: DateRange
  setDateRange: (r: DateRange) => void
}) {
  // On the Solar and Investing views the table shows that line's own answers
  // instead of the loan ones. Only when one line is actually selected: under
  // "All" the table mixes business lines and one set of headers cannot describe
  // all four.
  const line = lineColumnsFor(vertical)

  // Ticks are dropped whenever the filter or the business line changes: a row
  // ticked under one filter is not necessarily visible under the next, and
  // deleting something the client can no longer see is the failure this feature
  // must never have.
  const {
    selected,
    toggle,
    setPage: setPageSelection,
    clear,
    active: selecting,
    enter: enterSelection,
    exit: exitSelection,
  } = useRowSelection(`${statusFilter}|${vertical}|${JSON.stringify(deleteFilters)}`)
  const pageIds = leads.map((lead) => lead.id)
  const allOnPage = pageIds.length > 0 && pageIds.every((id) => selected.includes(id))
  const someOnPage = pageIds.some((id) => selected.includes(id))

  return (
    <>
      <section className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
        <div>
          <h1 className="font-display text-3xl font-semibold tracking-tight md:text-4xl">All Leads</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Manage and track all {leadStats?.total ?? totalCount} leads across sources.
          </p>
        </div>
        {/* Scrolls sideways rather than wrapping on a phone: three controls at
            full size wrap to two rows and push the table below the fold, and the
            table is what the client came for. */}
        <div className="-mx-4 flex items-center gap-2 overflow-x-auto px-4 pb-1 md:mx-0 md:overflow-visible md:px-0 md:pb-0">
          <DropdownMenu>
            <DropdownMenuTrigger render={<Button variant={statusFilter === "All" ? "outline" : "default"} size="sm" className="shrink-0" />}>
              <ListFilter data-icon="inline-start" />
              {statusFilter === "All" ? "Filter" : statusFilter}
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {LEAD_FILTERS.map((option) => (
                <DropdownMenuItem key={option.label} onClick={() => setStatusFilter(option.label)}>
                  {option.label}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
          <DateRangeFilter value={dateRange} onChange={setDateRange} noun="Imported" className="shrink-0" />
          <SelectButton
            active={selecting}
            onEnter={enterSelection}
            onExit={exitSelection}
            className="shrink-0"
          />
        </div>
      </section>

      {/* Four across on a phone rather than two-by-two. As a 2x2 grid of
          full-size cards these four numbers cost about 300px — most of a phone
          screen — before the first lead appeared, so the list the client came
          for started below the fold on every visit. They are context, not the
          content; one compact row says the same thing in a quarter of the space. */}
      <div className="stagger grid grid-cols-4 gap-2 md:gap-4">
        <Card>
          <CardContent className="p-3 md:p-4">
            <p className="text-lg font-semibold tabular-nums md:text-2xl">{leadStats?.total ?? 0}</p>
            <p className="text-[11px] leading-tight text-muted-foreground md:text-xs">Total leads</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-3 md:p-4">
            <p className="text-lg font-semibold tabular-nums md:text-2xl">{leadStats?.qualified ?? 0}</p>
            <p className="text-[11px] leading-tight text-muted-foreground md:text-xs">Qualified</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-3 md:p-4">
            <p className="text-lg font-semibold tabular-nums md:text-2xl">{leadStats?.new_leads ?? 0}</p>
            <p className="text-[11px] leading-tight text-muted-foreground md:text-xs">New</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-3 md:p-4">
            <p className="text-lg font-semibold tabular-nums md:text-2xl">{leadStats?.called ?? 0}</p>
            <p className="text-[11px] leading-tight text-muted-foreground md:text-xs">Called</p>
          </CardContent>
        </Card>
      </div>

      <BulkDeleteBar
        entity="leads"
        active={selecting}
        selected={selected}
        onClearSelection={clear}
        onExitSelection={exitSelection}
        onSelectAllOnPage={(checked) => setPageSelection(pageIds, checked)}
        pageCount={pageIds.length}
        allOnPageSelected={allOnPage}
        filters={deleteFilters}
        filterIsActive={statusFilter !== "All" || vertical !== "all" || !!dateRange.from || !!dateRange.to}
        matchingCount={totalCount}
        onDeleted={onDeleted}
      />

      {/* Phones get cards; `md` and up keeps the wide table unchanged. */}
      <Card className="min-w-0 md:hidden">
        <CardContent className="p-0">
          {leads.length === 0 ? (
            <p className="p-6 text-center text-sm text-muted-foreground">
              {isLoading ? "Loading leads…" : "No leads found. Import a file to add leads."}
            </p>
          ) : (
            <div className="stagger-rows flex flex-col">
              {leads.map((lead) => (
                <LeadCardMobile
                  key={lead.id}
                  lead={lead}
                  onSelect={onSelectLead}
                  line={line}
                  onCallPlaced={onCallPlaced}
                  selectable={selecting}
                  selected={selected.includes(lead.id)}
                  onToggleSelect={toggle}
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="hidden min-w-0 md:block">
        <CardContent className="overflow-x-auto p-0">
          <Table>
            <TableHeader>
              <TableRow>
                {selecting && (
                  <TableHead className="w-10">
                    <RowCheckbox
                      checked={allOnPage}
                      indeterminate={someOnPage}
                      onChange={(checked) => setPageSelection(pageIds, checked)}
                      label="Select every lead on this page"
                    />
                  </TableHead>
                )}
                <TableHead>Lead</TableHead>
                {/* Column priority. Eight columns did not fit a 1366px laptop —
                    the table measured 1083px inside a 1044px column and scrolled
                    sideways. Rather than shrink everything until it is all
                    equally unreadable, the two lowest-value columns step aside
                    on narrower screens: Source is almost always "file_upload",
                    and Location is blank for most leads. Both are still in the
                    lead's detail panel and on the phone card, so nothing is lost
                    — they simply stop outranking Score and Status, which are
                    what this table is read for. */}
                <TableHead className="hidden xl:table-cell">Source</TableHead>
                <TableHead className="hidden 2xl:table-cell">Location</TableHead>
                {/* Not "Loan type": this table holds all four business lines.
                    On Solar and Investing these become the questions that line's
                    agent actually asks. */}
                {line ? (
                  <>
                    <TableHead>{line.headers[0]}</TableHead>
                    <TableHead>{line.headers[1]}</TableHead>
                  </>
                ) : (
                  <>
                    <TableHead>Requirement</TableHead>
                    <TableHead>Budget / amount</TableHead>
                  </>
                )}
                <TableHead>Score</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Last activity</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody className="stagger-rows">
              {leads.length === 0 ? (
                <EmptyRow colSpan={selecting ? 9 : 8}>
                  {isLoading ? "Loading leads…" : "No leads found. Upload a CSV to import leads."}
                </EmptyRow>
              ) : (
                leads.map((lead) => (
                  <LeadRow
                    key={lead.id}
                    lead={lead}
                    onSelect={onSelectLead}
                    columns="full"
                    line={line}
                    onCallPlaced={onCallPlaced}
                    selected={selected.includes(lead.id)}
                    // Passing this is what adds the tick column, so withholding
                    // it outside selection mode removes the column entirely
                    // rather than leaving an empty 40px gutter behind.
                    onToggleSelect={selecting ? toggle : undefined}
                  />
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Pager page={page} totalPages={totalPages} totalCount={totalCount} noun="leads" onChange={setPage} />
    </>
  )
}

function CallsPage({
  onSelectLead,
  callStats,
  vertical,
}: {
  onSelectLead: (lead: any, callId?: string | null) => void
  callStats: any
  vertical: VerticalFilter
}) {
  const [callFilter, setCallFilter] = useState("all")
  const [page, setPage] = useState(1)
  const [dateRange, setDateRange] = useState<DateRange>(EMPTY_RANGE)
  // Filtering happens server-side on outcome. The old tabs filtered on a
  // `direction` field that no call row has ever carried, so every tab but "All"
  // rendered an empty table.
  const { calls, totalCount, totalPages, isLoading, refresh } = useCalls(callFilter, page, "", vertical, {
    after: dateRange.from,
    before: dateRange.to,
  })

  useEffect(() => {
    setPage(1)
  }, [callFilter, vertical, dateRange.from, dateRange.to])

  // The tabs group several outcomes under one label ("No answer" covers busy and
  // voicemail too). The delete endpoint takes the raw outcomes, so the grouping
  // is expanded here rather than re-implemented server-side — one definition of
  // what "No answer" means, shared by the list and the delete.
  const CALL_FILTER_OUTCOMES: Record<string, string> = {
    connected: "completed",
    missed: "no_answer,busy,voicemail",
    failed: "failed,cancelled",
  }
  const deleteFilters = {
    outcome: CALL_FILTER_OUTCOMES[callFilter],
    vertical: vertical === "all" ? undefined : vertical,
    // The delete route spells these `calledAfter`/`calledBefore` while the list
    // route reads `startDate`/`endDate`. Same column, two names — see the note
    // on useCalls. Sent under the delete's own spelling so a date-limited delete
    // covers exactly the days the table is showing.
    calledAfter: dateRange.from,
    calledBefore: dateRange.to,
  }

  const {
    selected,
    toggle,
    setPage: setPageSelection,
    clear,
    active: selecting,
    enter: enterSelection,
    exit: exitSelection,
  } = useRowSelection(`${callFilter}|${vertical}|${dateRange.from ?? ""}|${dateRange.to ?? ""}`)
  const pageIds = calls.map((call: any) => call.id)
  const allOnPage = pageIds.length > 0 && pageIds.every((id: string) => selected.includes(id))
  const someOnPage = pageIds.some((id: string) => selected.includes(id))

  return (
    <>
      <section className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
        <div>
          <h1 className="font-display text-3xl font-semibold tracking-tight md:text-4xl">Call History</h1>
          <p className="mt-1 text-sm text-muted-foreground">{callStats?.total ?? 0} calls recorded.</p>
        </div>
        {/* Two scrollable rows on a phone rather than one wrapped block: the
            outcome tabs and the date/select controls are different decisions and
            reading them as one run of eight buttons is what made this page feel
            like a wall. */}
        <div className="flex flex-col gap-2">
          <div className="-mx-4 overflow-x-auto px-4 pb-1 md:mx-0 md:overflow-visible md:px-0 md:pb-0">
            <Tabs value={callFilter} onValueChange={setCallFilter}>
              <TabsList className="w-max">
                <TabsTrigger value="all">All</TabsTrigger>
                <TabsTrigger value="connected">Connected</TabsTrigger>
                <TabsTrigger value="missed">No answer</TabsTrigger>
                <TabsTrigger value="failed">Failed</TabsTrigger>
              </TabsList>
            </Tabs>
          </div>
          <div className="-mx-4 flex items-center gap-2 overflow-x-auto px-4 pb-1 md:mx-0 md:justify-end md:overflow-visible md:px-0 md:pb-0">
            <DateRangeFilter value={dateRange} onChange={setDateRange} noun="Called" className="shrink-0" />
            <SelectButton
              active={selecting}
              onEnter={enterSelection}
              onExit={exitSelection}
              className="shrink-0"
            />
            <Button variant="outline" size="sm" onClick={refresh} className="shrink-0">
              <RefreshCw data-icon="inline-start" className={cn(isLoading && "animate-spin")} />
              Refresh
            </Button>
          </div>
        </div>
      </section>

      {/* Four across on a phone — same reasoning as the Leads tiles. The icon
          hides below `md`: at ~80px per tile it pushes the number onto a second
          line, and the label already says which number this is. */}
      <div className="stagger grid grid-cols-4 gap-2 md:gap-4">
        <Card>
          <CardContent className="p-3 md:p-4">
            <div className="flex items-center gap-2">
              <PhoneCall className="hidden size-4 text-primary md:block" />
              <span className="text-lg font-semibold tabular-nums md:text-2xl">{callStats?.total ?? 0}</span>
            </div>
            <p className="text-[11px] leading-tight text-muted-foreground md:text-xs">Total calls</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-3 md:p-4">
            <div className="flex items-center gap-2">
              <PhoneForwarded className="hidden size-4 text-primary md:block" />
              <span className="text-lg font-semibold tabular-nums md:text-2xl">{callStats?.connected ?? 0}</span>
            </div>
            <p className="text-[11px] leading-tight text-muted-foreground md:text-xs">Connected</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-3 md:p-4">
            <div className="flex items-center gap-2">
              <PhoneMissed className="hidden size-4 text-destructive md:block" />
              <span className="text-lg font-semibold tabular-nums md:text-2xl">
                {(callStats?.missed ?? 0) + (callStats?.failed ?? 0)}
              </span>
            </div>
            <p className="text-[11px] leading-tight text-muted-foreground md:text-xs">Not connected</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-3 md:p-4">
            <div className="flex items-center gap-2">
              <Clock3 className="hidden size-4 text-muted-foreground md:block" />
              <span className="text-lg font-semibold tabular-nums md:text-2xl">
                {formatDuration(callStats?.avg_duration)}
              </span>
            </div>
            <p className="text-[11px] leading-tight text-muted-foreground md:text-xs">Avg. call</p>
          </CardContent>
        </Card>
      </div>

      <BulkDeleteBar
        entity="calls"
        active={selecting}
        selected={selected}
        onClearSelection={clear}
        onExitSelection={exitSelection}
        onSelectAllOnPage={(checked) => setPageSelection(pageIds, checked)}
        pageCount={pageIds.length}
        allOnPageSelected={allOnPage}
        filters={deleteFilters}
        filterIsActive={callFilter !== "all" || vertical !== "all" || !!dateRange.from || !!dateRange.to}
        matchingCount={totalCount}
        onDeleted={refresh}
      />

      {/* Phones get cards; `md` and up keeps the wide table unchanged. */}
      <Card className="min-w-0 md:hidden">
        <CardContent className="p-0">
          {calls.length === 0 ? (
            <p className="p-6 text-center text-sm text-muted-foreground">
              {isLoading ? "Loading calls…" : "No calls recorded yet."}
            </p>
          ) : (
            <div className="stagger-rows flex flex-col">
              {calls.map((call: any) => (
                <CallCardMobile
                  key={call.id}
                  call={call}
                  onSelect={onSelectLead}
                  selectable={selecting}
                  selected={selected.includes(call.id)}
                  onToggleSelect={toggle}
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="hidden min-w-0 md:block">
        <CardContent className="overflow-x-auto p-0">
          <Table>
            <TableHeader>
              <TableRow>
                {selecting && (
                  <TableHead className="w-10">
                    <RowCheckbox
                      checked={allOnPage}
                      indeterminate={someOnPage}
                      onChange={(checked) => setPageSelection(pageIds, checked)}
                      label="Select every call on this page"
                    />
                  </TableHead>
                )}
                <TableHead>Status</TableHead>
                <TableHead>Lead</TableHead>
                <TableHead>Outcome</TableHead>
                <TableHead>Duration</TableHead>
                <TableHead>Attempt</TableHead>
                <TableHead>Recording</TableHead>
                <TableHead className="text-right">Time</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody className="stagger-rows">
              {calls.length === 0 ? (
                <EmptyRow colSpan={selecting ? 8 : 7}>
                  {isLoading ? "Loading calls…" : "No calls recorded yet. Launch a campaign to start calling."}
                </EmptyRow>
              ) : (
                calls.map((call: any) => (
                  <TableRow
                    key={call.id}
                    className={cn(call.leads && "cursor-pointer hover:bg-muted/50")}
                    onClick={() => call.leads && onSelectLead(call.leads, call.id)}
                  >
                    {selecting && (
                      <TableCell className="w-10">
                        <RowCheckbox
                          checked={selected.includes(call.id)}
                          onChange={() => toggle(call.id)}
                          label={`Select call to ${call.leads?.name || call.leads?.phone || "unknown"}`}
                        />
                      </TableCell>
                    )}
                    <TableCell>
                      <CallOutcomeIcon outcome={call.outcome} />
                    </TableCell>
                    <TableCell>
                      <div>
                        {/* /api/calls joins the lead as `leads`, so the name lives at
                            call.leads.name - the flat fields the table used to read
                            never exist and every row showed "Unknown". */}
                        <p className="font-medium">{call.leads?.name || "Unknown"}</p>
                        <p className="text-xs text-muted-foreground">{call.leads?.phone || ""}</p>
                      </div>
                    </TableCell>
                    <TableCell>
                      <span className="text-sm">{OUTCOME_LABELS[call.outcome] || call.outcome || "—"}</span>
                    </TableCell>
                    <TableCell className="whitespace-nowrap">
                      <span className="font-mono text-sm">{formatDuration(call.duration)}</span>
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-sm text-muted-foreground">#{call.attempt_no ?? 1}</TableCell>
                    <TableCell>
                      {call.recording_url ? (
                        <a
                          href={`/api/calls/${call.id}/recording`}
                          target="_blank"
                          rel="noreferrer"
                          onClick={(e) => e.stopPropagation()}
                          className="text-sm font-medium text-primary underline underline-offset-4"
                        >
                          Play
                        </a>
                      ) : (
                        <span className="text-sm text-muted-foreground">—</span>
                      )}
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-right text-sm text-muted-foreground">
                      {formatDateTime(call.called_at)}
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Pager page={page} totalPages={totalPages} totalCount={totalCount} noun="calls" onChange={setPage} />
    </>
  )
}

function CampaignsPage({
  setCampaignOpen,
  campaignsData,
  refreshCampaigns,
  isLoading,
}: {
  setCampaignOpen: (v: boolean) => void
  campaignsData: any[]
  refreshCampaigns: () => void
  isLoading: boolean
}) {
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [pausingId, setPausingId] = useState<string | null>(null)
  const [campaignRuns, setCampaignRuns] = useState<Record<string, any[]>>({})
  const [loadingRuns, setLoadingRuns] = useState<string | null>(null)
  const [settingsCampaign, setSettingsCampaign] = useState<any>(null)
  const [settingsForm, setSettingsForm] = useState({
    concurrency: "1",
    maxRetries: "2",
    retryDelaySeconds: "120",
    retryOnBusy: true,
    retryOnNoAnswer: true,
    retryOnVoicemail: true,
  })
  const [savingSettings, setSavingSettings] = useState(false)

  const openSettings = (campaign: any) => {
    setSettingsCampaign(campaign)
    setSettingsForm({
      concurrency: String(campaign.concurrency || 1),
      // 1, not 2: this is redials AFTER the first call, so the default matches a
      // 2-calls-per-person policy. Defaulting to 2 meant a legacy campaign with
      // no retry_config opened showing three calls' worth and the server
      // (correctly) refused to save it.
      maxRetries: String(campaign.retry_config?.max_retries ?? 1),
      retryDelaySeconds: String(campaign.retry_config?.retry_delay_seconds ?? 120),
      // Dograh's PATCH replaces the whole retry_config object rather than
      // merging it, so these have to round-trip through the form too -
      // omitting them here would silently reset a campaign that had them
      // turned off back to "retry on everything" the moment you changed
      // anything else.
      retryOnBusy: campaign.retry_config?.retry_on_busy ?? true,
      retryOnNoAnswer: campaign.retry_config?.retry_on_no_answer ?? true,
      retryOnVoicemail: campaign.retry_config?.retry_on_voicemail ?? true,
    })
  }

  const handleSaveSettings = async () => {
    if (!settingsCampaign) return
    const concurrency = Number(settingsForm.concurrency)
    if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 100) {
      toast.error("Concurrency must be a whole number between 1 and 100")
      return
    }
    const maxRetries = Number(settingsForm.maxRetries)
    if (!Number.isInteger(maxRetries) || maxRetries < 0 || maxRetries > 10) {
      toast.error("Max retries must be a whole number between 0 and 10")
      return
    }
    const retryDelaySeconds = Number(settingsForm.retryDelaySeconds)
    if (!Number.isInteger(retryDelaySeconds) || retryDelaySeconds < 30 || retryDelaySeconds > 3600) {
      toast.error("Retry delay must be a whole number of seconds between 30 and 3600")
      return
    }
    setSavingSettings(true)
    try {
      const res = await fetch(`/api/campaigns/${settingsCampaign.id}/settings`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          concurrency,
          retry_config: {
            max_retries: maxRetries,
            retry_delay_seconds: retryDelaySeconds,
            retry_on_busy: settingsForm.retryOnBusy,
            retry_on_no_answer: settingsForm.retryOnNoAnswer,
            retry_on_voicemail: settingsForm.retryOnVoicemail,
          },
        }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.error || `Failed to save settings (${res.status})`)
      toast.success("Campaign settings updated")
      setSettingsCampaign(null)
      refreshCampaigns()
    } catch (err: any) {
      toast.error(err.message || "Failed to save settings")
    } finally {
      setSavingSettings(false)
    }
  }

  const handleTogglePause = async (campaign: any) => {
    if (pausingId) return
    setPausingId(campaign.id)
    try {
      const name = campaign.campaign_name || campaign.name
      if (campaign.status === "running" || campaign.status === "active" || campaign.status === "queued") {
        await pauseCampaign(campaign.id)
        toast.success(`Campaign "${name}" paused`)
      } else if (campaign.status === "paused") {
        await resumeCampaign(campaign.id)
        toast.success(`Campaign "${name}" resumed`)
      }
      refreshCampaigns()
    } catch (err: any) {
      toast.error(err.message || "Failed to update campaign")
    } finally {
      setPausingId(null)
    }
  }

  const loadRuns = useCallback(async (campaign: any) => {
    setLoadingRuns(campaign.id)
    try {
      const res = await fetch(`/api/campaigns/${campaign.id}/runs?limit=50`)
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.error || `Failed to load call details (${res.status})`)
      setCampaignRuns((prev) => ({ ...prev, [campaign.id]: data.runs || [] }))
    } catch (err: any) {
      // Previously this failure was swallowed into console.warn and the panel just
      // said "no call records yet", which looked like missing data rather than an error.
      toast.error(err.message || "Could not load call details")
    } finally {
      setLoadingRuns(null)
    }
  }, [])

  const handleToggleDetails = async (campaign: any) => {
    if (expandedId === campaign.id) {
      setExpandedId(null)
      return
    }
    setExpandedId(campaign.id)
    if (!campaignRuns[campaign.id]) await loadRuns(campaign)
  }

  const activeCampaigns = campaignsData.filter((c: any) => c.status === "running" || c.status === "active").length
  const totalTargeted = campaignsData.reduce((sum: number, c: any) => sum + (c.actual_count || c.requested_count || 0), 0)

  // Only finished campaigns can be ticked. A live campaign's row is the only
  // handle the system has on the Dograh campaign it started — the server refuses
  // to delete one either way, and offering a tick that silently does nothing is
  // worse than not offering it.
  const DELETABLE = ["completed", "failed", "cancelled"]
  const deletableCampaigns = campaignsData.filter((c: any) => DELETABLE.includes(c.status))
  const {
    selected,
    toggle,
    setPage: setPageSelection,
    clear,
    active: selecting,
    enter: enterSelection,
    exit: exitSelection,
  } = useRowSelection(campaignsData.length)
  const deletableIds = deletableCampaigns.map((c: any) => c.id)
  const allSelected = deletableIds.length > 0 && deletableIds.every((id: string) => selected.includes(id))
  const someSelected = deletableIds.some((id: string) => selected.includes(id))

  return (
    <>
      <section className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
        <div>
          <h1 className="font-display text-3xl font-semibold tracking-tight md:text-4xl">Campaigns</h1>
          <p className="mt-1 text-sm text-muted-foreground">Manage all AI calling campaigns.</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={refreshCampaigns}>
            <RefreshCw data-icon="inline-start" className={cn(isLoading && "animate-spin")} />
            Refresh
          </Button>
          <Button size="sm" onClick={() => setCampaignOpen(true)}>
            <Plus data-icon="inline-start" />
            New campaign
          </Button>
        </div>
      </section>

      <div className="stagger grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Card>
          <CardContent className="p-4">
            <p className="text-2xl font-semibold">{activeCampaigns}</p>
            <p className="text-xs text-muted-foreground">Active campaigns</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-2xl font-semibold">{totalTargeted.toLocaleString()}</p>
            <p className="text-xs text-muted-foreground">Total leads targeted</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-2xl font-semibold">{campaignsData.length}</p>
            <p className="text-xs text-muted-foreground">Total campaigns</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-2xl font-semibold">{campaignsData.filter((c: any) => c.status === "completed").length}</p>
            <p className="text-xs text-muted-foreground">Completed</p>
          </CardContent>
        </Card>
      </div>

      {deletableCampaigns.length > 0 && (
        <div className="flex flex-col gap-2">
          {/* The "select all finished" line only exists once the client has
              actually asked to select. Outside that it was a permanent tick box
              on a page whose main job is watching campaigns run. */}
          <div className="flex items-center gap-3 px-1">
            <SelectButton active={selecting} onEnter={enterSelection} onExit={exitSelection} />
            {selecting && (
              <label className="flex min-w-0 items-center gap-2 text-sm text-muted-foreground">
                <RowCheckbox
                  checked={allSelected}
                  indeterminate={someSelected}
                  onChange={(checked) => setPageSelection(deletableIds, checked)}
                  label="Select every finished campaign"
                />
                <span className="truncate">
                  All {deletableCampaigns.length} finished campaign
                  {deletableCampaigns.length === 1 ? "" : "s"}
                </span>
              </label>
            )}
          </div>
          <BulkDeleteBar
            entity="campaigns"
            active={selecting}
            selected={selected}
            onClearSelection={clear}
            onExitSelection={exitSelection}
            onSelectAllOnPage={(checked) => setPageSelection(deletableIds, checked)}
            pageCount={deletableIds.length}
            allOnPageSelected={allSelected}
            filters={{ status: "completed,failed,cancelled" }}
            filterIsActive={false}
            matchingCount={deletableCampaigns.length}
            onDeleted={refreshCampaigns}
          />
        </div>
      )}

      <div className="stagger-rows flex flex-col gap-4">
        {campaignsData.length === 0 ? (
          <Card>
            <CardContent className="p-8 text-center text-muted-foreground">
              {isLoading
                ? "Loading campaigns…"
                : "No campaigns yet. Click “New campaign” to launch your first AI calling campaign."}
            </CardContent>
          </Card>
        ) : (
          campaignsData.map((campaign: any) => {
            const name = campaign.campaign_name || "Unnamed"
            const status = campaign.status || "unknown"
            const actual = campaign.actual_count || campaign.requested_count || 0
            const isExpanded = expandedId === campaign.id
            const runs = campaignRuns[campaign.id] || []
            const answered = runs.filter((r: any) => (r.status || (r.is_completed ? "completed" : "")) === "completed").length
            const noAnswer = runs.filter((r: any) =>
              ["no-answer", "no_answer", "unanswered", "voicemail"].includes(r.status),
            ).length
            const busy = runs.filter((r: any) => ["busy", "failed"].includes(r.status)).length
            const canControl = !["completed", "failed", "cancelled"].includes(status)

            return (
              <Card key={campaign.id}>
                <CardContent className="p-5">
                  <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
                    <div className="flex items-center gap-4">
                      {/* Rendered only while selecting, and only for finished
                          campaigns — see DELETABLE above. */}
                      {selecting && DELETABLE.includes(status) && (
                        <RowCheckbox
                          checked={selected.includes(campaign.id)}
                          onChange={() => toggle(campaign.id)}
                          label={`Select campaign ${name}`}
                        />
                      )}
                      <div className="flex size-10 items-center justify-center rounded-lg bg-secondary">
                        <Megaphone className="size-5 text-muted-foreground" />
                      </div>
                      <div>
                        <div className="flex items-center gap-2">
                          <p className="font-semibold">{name}</p>
                          {/* Shown even when the header switch is on a single
                              business line: on "All" the list is mixed, and a
                              campaign name alone rarely says which one it was. */}
                          <Badge
                            variant="outline"
                            className={cn(
                              "border-transparent",
                              VERTICAL_STYLES[verticalOf(campaign)].color,
                              VERTICAL_STYLES[verticalOf(campaign)].bg,
                            )}
                          >
                            {VERTICAL_LABELS[verticalOf(campaign)]}
                          </Badge>
                          <CampaignStatusBadge status={status} />
                        </div>
                        <p className="text-sm text-muted-foreground">
                          Started {formatDate(campaign.started_at || campaign.created_at)} · {actual} leads
                        </p>
                        {campaign.error_message && (
                          <p className="mt-1 text-xs text-destructive">{campaign.error_message}</p>
                        )}
                      </div>
                    </div>
                    {/* Pause/Resume and Settings stay visible on a finished
                        campaign, but disabled with the reason on hover. Hiding
                        them entirely made the controls look missing rather than
                        inapplicable — a finished campaign genuinely cannot be
                        paused or reconfigured, and the UI should say so. */}
                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={!canControl || pausingId === campaign.id}
                        title={canControl ? undefined : `This campaign has ${status}. Only running campaigns can be paused.`}
                        onClick={() => handleTogglePause(campaign)}
                      >
                        {pausingId === campaign.id ? (
                          "Updating…"
                        ) : status === "paused" ? (
                          <>
                            <Play data-icon="inline-start" />
                            Resume
                          </>
                        ) : (
                          <>
                            <Pause data-icon="inline-start" />
                            Pause
                          </>
                        )}
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={!canControl}
                        title={canControl ? undefined : `This campaign has ${status}. Its settings can no longer be changed.`}
                        onClick={() => openSettings(campaign)}
                      >
                        <Settings data-icon="inline-start" />
                        Settings
                      </Button>
                      <Button variant="outline" size="sm" onClick={() => handleToggleDetails(campaign)}>
                        <Eye data-icon="inline-start" />
                        {isExpanded ? "Hide" : "Details"}
                      </Button>
                    </div>
                  </div>
                  <div className="mt-4 grid grid-cols-2 gap-4 md:grid-cols-4">
                    <div>
                      <p className="text-xs text-muted-foreground">Leads</p>
                      <p className="text-lg font-semibold">{actual}</p>
                    </div>
                    <div>
                      <p className="text-xs text-muted-foreground">Answered</p>
                      <p className="text-lg font-semibold text-green-600">{runs.length > 0 ? answered : "—"}</p>
                    </div>
                    <div>
                      <p className="text-xs text-muted-foreground">Not answered</p>
                      <p className="text-lg font-semibold text-orange-500">{runs.length > 0 ? noAnswer : "—"}</p>
                    </div>
                    <div>
                      <p className="text-xs text-muted-foreground">Busy / Failed</p>
                      <p className="text-lg font-semibold text-red-500">{runs.length > 0 ? busy : "—"}</p>
                    </div>
                  </div>
                  {isExpanded && (
                    <div className="mt-4 rounded-lg border bg-muted/30 p-4">
                      <div className="mb-3 flex items-center justify-between">
                        <p className="text-sm font-medium">Call details</p>
                        <Button variant="ghost" size="sm" onClick={() => loadRuns(campaign)}>
                          <RefreshCw data-icon="inline-start" />
                          Reload
                        </Button>
                      </div>
                      {loadingRuns === campaign.id ? (
                        <p className="text-sm text-muted-foreground">Loading call details…</p>
                      ) : runs.length === 0 ? (
                        <p className="text-sm text-muted-foreground">
                          No call records yet. {status === "running" ? "Campaign is still in progress." : ""}
                        </p>
                      ) : (
                        <div className="max-h-64 overflow-y-auto">
                          <Table>
                            <TableHeader>
                              <TableRow>
                                <TableHead>Phone</TableHead>
                                <TableHead>Status</TableHead>
                                <TableHead>Duration</TableHead>
                                <TableHead className="text-right">Called at</TableHead>
                              </TableRow>
                            </TableHeader>
                            <TableBody className="stagger-rows">
                              {runs.map((run: any, idx: number) => {
                                // These rows come straight from Dograh's run records, which nest
                                // the number under initial_context and the length under cost_info,
                                // and report progress via is_completed rather than a `status`
                                // string.
                                const phone = run.initial_context?.phone_number || run.phone_number || run.phone
                                const seconds = run.cost_info?.call_duration_seconds ?? run.duration
                                const state = run.status || (run.is_completed ? "completed" : "in progress")
                                const isAnswered = state === "completed"
                                const isUnanswered = ["no-answer", "no_answer", "unanswered", "busy", "failed", "voicemail"].includes(
                                  state,
                                )
                                return (
                                  <TableRow key={run.id || idx}>
                                    <TableCell className="font-mono text-sm">{phone || "—"}</TableCell>
                                    <TableCell>
                                      <Badge
                                        variant="outline"
                                        className={
                                          isAnswered
                                            ? "border-green-500/20 bg-green-500/10 text-green-600"
                                            : isUnanswered
                                              ? "border-orange-500/20 bg-orange-500/10 text-orange-500"
                                              : "bg-muted text-muted-foreground"
                                        }
                                      >
                                        {state}
                                      </Badge>
                                    </TableCell>
                                    <TableCell>{formatDuration(seconds)}</TableCell>
                                    <TableCell className="text-right text-sm text-muted-foreground">
                                      {formatDateTime(run.created_at)}
                                    </TableCell>
                                  </TableRow>
                                )
                              })}
                            </TableBody>
                          </Table>
                        </div>
                      )}
                      <div className="mt-3 grid grid-cols-2 gap-2 border-t pt-3 text-sm">
                        <div className="flex justify-between">
                          <span className="text-muted-foreground">Created</span>
                          <span>{formatDateTime(campaign.created_at)}</span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-muted-foreground">Completed</span>
                          <span>{formatDateTime(campaign.completed_at)}</span>
                        </div>
                        {campaign.paused_at && (
                          <div className="flex justify-between">
                            <span className="text-muted-foreground">Paused</span>
                            <span>{formatDateTime(campaign.paused_at)}</span>
                          </div>
                        )}
                        <div className="flex justify-between">
                          <span className="text-muted-foreground">Concurrency</span>
                          <span>{campaign.concurrency || 1}</span>
                        </div>
                      </div>
                    </div>
                  )}
                </CardContent>
              </Card>
            )
          })
        )}
      </div>

      {/* Edit settings on a live campaign - Dograh accepts this on any campaign
          that isn't completed/failed, so it works while queued/running/paused. */}
      <Dialog open={!!settingsCampaign} onOpenChange={(open) => !open && setSettingsCampaign(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Campaign settings</DialogTitle>
            <DialogDescription>
              {settingsCampaign?.campaign_name} — changes apply immediately, calls in progress are not interrupted.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-5">
            <div className="grid gap-2">
              <label className="text-sm font-medium" htmlFor="settings-concurrency">
                Concurrency
              </label>
              <Input
                id="settings-concurrency"
                type="number"
                min={1}
                max={100}
                value={settingsForm.concurrency}
                onChange={(e) => setSettingsForm({ ...settingsForm, concurrency: e.target.value })}
              />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div className="grid gap-2">
                <label className="text-sm font-medium" htmlFor="settings-max-retries">
                  Redials after the first call
                </label>
                <Input
                  id="settings-max-retries"
                  type="number"
                  min={0}
                  max={10}
                  value={settingsForm.maxRetries}
                  onChange={(e) => setSettingsForm({ ...settingsForm, maxRetries: e.target.value })}
                />
                <p className="text-xs text-muted-foreground">
                  Redials only — the first call is not counted here. Limited by &ldquo;Max retries&rdquo; on the AI
                  Agent page, which caps total calls per person.
                </p>
              </div>
              <div className="grid gap-2">
                <label className="text-sm font-medium" htmlFor="settings-retry-delay">
                  Retry delay (sec)
                </label>
                <Input
                  id="settings-retry-delay"
                  type="number"
                  min={30}
                  max={3600}
                  value={settingsForm.retryDelaySeconds}
                  onChange={(e) => setSettingsForm({ ...settingsForm, retryDelaySeconds: e.target.value })}
                />
              </div>
            </div>
            <div className="grid gap-2">
              <span className="text-sm font-medium">Retry on</span>
              <div className="flex flex-col gap-1.5">
                {[
                  { key: "retryOnBusy" as const, label: "Busy" },
                  { key: "retryOnNoAnswer" as const, label: "No answer" },
                  { key: "retryOnVoicemail" as const, label: "Voicemail" },
                ].map((opt) => (
                  <label key={opt.key} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={settingsForm[opt.key]}
                      onChange={(e) => setSettingsForm({ ...settingsForm, [opt.key]: e.target.checked })}
                    />
                    {opt.label}
                  </label>
                ))}
              </div>
            </div>
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" onClick={() => setSettingsCampaign(null)}>
              Cancel
            </Button>
            <Button onClick={handleSaveSettings} disabled={savingSettings}>
              {savingSettings ? "Saving…" : "Save settings"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

function FollowUpsPage({
  onSelectLead,
  vertical,
}: {
  onSelectLead: (lead: any, callId?: string | null) => void
  vertical: VerticalFilter
}) {
  const [page, setPage] = useState(1)
  // Real data. This page used to filter a module-level array that was permanently
  // empty, so it showed "0 leads need follow-up" no matter what was in the database.
  // Soonest first. The default sort is created_at desc, which buried the callback
  // due today under whichever leads happened to be uploaded most recently.
  const { leads, totalCount, totalPages, isLoading, refresh } = useLeads(
    "",
    { followUp: true, sort: "follow_up_date", order: "asc" },
    page,
    vertical,
  )
  const { leads: retryLeads } = useLeads("", { status: "retry_pending" }, 1, vertical)

  const now = Date.now()
  const dayMs = 24 * 3600 * 1000
  const ageOf = (l: any) => {
    const date = new Date(l.follow_up_date as string).getTime()
    return Number.isFinite(date) ? date - now : null
  }
  // A callback whose date has already passed used to be counted under "due in
  // the next 2 days", so a month-old missed follow-up looked like upcoming work
  // and nothing in the app ever said it had been missed.
  const overdue = leads.filter((l: any) => { const d = ageOf(l); return d !== null && d < 0 })
  const dueSoon = leads.filter((l: any) => { const d = ageOf(l); return d !== null && d >= 0 && d < 2 * dayMs })
  const upcoming = leads.filter((l: any) => { const d = ageOf(l); return d !== null && d >= 2 * dayMs })

  return (
    <>
      <section className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
        <div>
          <h1 className="font-display text-3xl font-semibold tracking-tight md:text-4xl">Follow-ups</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {totalCount === 1 ? "1 lead has" : `${totalCount} leads have`} a follow-up scheduled.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={refresh}>
          <RefreshCw data-icon="inline-start" className={cn(isLoading && "animate-spin")} />
          Refresh
        </Button>
      </section>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Card className={cn(overdue.length > 0 && "border-destructive/40")}>
          <CardContent className="p-4">
            <div className="flex items-center gap-2">
              <CalendarClock className={cn("size-4", overdue.length > 0 ? "text-destructive" : "text-muted-foreground")} />
              <span className="text-2xl font-semibold">{overdue.length}</span>
            </div>
            <p className="text-xs text-muted-foreground">Overdue</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <div className="flex items-center gap-2">
              <Calendar className="size-4 text-primary" />
              <span className="text-2xl font-semibold">{dueSoon.length}</span>
            </div>
            <p className="text-xs text-muted-foreground">Due in the next 2 days</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <div className="flex items-center gap-2">
              <CalendarClock className="size-4 text-muted-foreground" />
              <span className="text-2xl font-semibold">{upcoming.length}</span>
            </div>
            <p className="text-xs text-muted-foreground">Later</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <div className="flex items-center gap-2">
              <Target className="size-4 text-primary" />
              <span className="text-2xl font-semibold">{retryLeads.length}</span>
            </div>
            <p className="text-xs text-muted-foreground">Awaiting retry</p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Scheduled follow-ups</CardTitle>
          <CardDescription>Leads that asked to be contacted again</CardDescription>
        </CardHeader>
        {/* Phone layout. A follow-up is something you act on while holding the
            phone you are about to call from, so the two actions are full-width
            buttons rather than 32px icons in a table's last column. */}
        <CardContent className="p-0 md:hidden">
          {leads.length === 0 ? (
            <p className="p-6 text-center text-sm text-muted-foreground">
              {isLoading
                ? "Loading follow-ups…"
                : "No follow-ups scheduled. A follow-up appears here when a call captures a callback date."}
            </p>
          ) : (
            <div className="stagger-rows flex flex-col">
              {leads.map((lead: any) => (
                <div
                  key={lead.id}
                  role="button"
                  tabIndex={0}
                  onClick={() => onSelectLead(lead)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault()
                      onSelectLead(lead)
                    }
                  }}
                  className="flex cursor-pointer flex-col gap-3 border-b border-border/70 p-4 transition-colors last:border-b-0 active:bg-muted/60"
                >
                  <div className="flex items-start gap-3">
                    <Avatar className="size-9 shrink-0">
                      <AvatarFallback>{initialsOf(lead.name)}</AvatarFallback>
                    </Avatar>
                    <div className="min-w-0 flex-1">
                      <p className="truncate font-medium">{lead.name || "Unknown"}</p>
                      <p className="truncate text-xs text-muted-foreground">{lead.phone}</p>
                    </div>
                    <StatusBadge status={leadStatusLabel(lead)} />
                  </div>

                  <p className="text-sm">
                    <span className="text-muted-foreground">Call back on </span>
                    <span className="font-medium">{formatDate(lead.follow_up_date)}</span>
                  </p>

                  {lead.notes && (
                    <p className="line-clamp-2 text-sm text-muted-foreground">{lead.notes}</p>
                  )}

                  <div className="grid grid-cols-2 gap-2">
                    <a
                      href={`tel:${dialable(lead.phone)}`}
                      onClick={(e) => e.stopPropagation()}
                      className={cn(buttonVariants({ variant: "outline", size: "sm" }))}
                    >
                      <Phone data-icon="inline-start" />
                      Call
                    </a>
                    <a
                      href={`https://wa.me/${dialable(lead.phone).replace(/^\+/, "")}`}
                      target="_blank"
                      rel="noreferrer"
                      onClick={(e) => e.stopPropagation()}
                      className={cn(buttonVariants({ variant: "outline", size: "sm" }))}
                    >
                      <MessageSquare data-icon="inline-start" />
                      WhatsApp
                    </a>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>

        <CardContent className="hidden overflow-x-auto p-0 md:block">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Lead</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Follow-up date</TableHead>
                <TableHead>Notes</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody className="stagger-rows">
              {leads.length === 0 ? (
                <EmptyRow colSpan={5}>
                  {isLoading
                    ? "Loading follow-ups…"
                    : "No follow-ups scheduled. A follow-up appears here when a call captures a callback date."}
                </EmptyRow>
              ) : (
                leads.map((lead: any) => (
                  <TableRow key={lead.id} className="cursor-pointer hover:bg-muted/50" onClick={() => onSelectLead(lead)}>
                    <TableCell>
                      <div className="flex items-center gap-3">
                        <Avatar className="size-8">
                          <AvatarFallback>{initialsOf(lead.name)}</AvatarFallback>
                        </Avatar>
                        <div>
                          <p className="font-medium">{lead.name || "Unknown"}</p>
                          <p className="text-xs text-muted-foreground">{lead.phone}</p>
                        </div>
                      </div>
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={leadStatusLabel(lead)} />
                    </TableCell>
                    <TableCell className="whitespace-nowrap">
                      <span className="text-sm font-medium">{formatDate(lead.follow_up_date)}</span>
                    </TableCell>
                    {/* Capped, because notes hold whole call logs. Uncapped this
                        one column set the width of the entire table. */}
                    <TableCell className="max-w-xs">
                      <span className="line-clamp-2 text-sm text-muted-foreground">{lead.notes || "—"}</span>
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-1">
                        {/* Real links instead of a toast that pretended to dial. */}
                        <a
                          href={`tel:${dialable(lead.phone)}`}
                          aria-label={`Call ${lead.name || lead.phone}`}
                          onClick={(e) => e.stopPropagation()}
                          className={cn(buttonVariants({ variant: "ghost", size: "icon" }))}
                        >
                          <Phone className="size-4" />
                        </a>
                        <a
                          href={`https://wa.me/${dialable(lead.phone).replace(/^\+/, "")}`}
                          target="_blank"
                          rel="noreferrer"
                          aria-label={`WhatsApp ${lead.name || lead.phone}`}
                          onClick={(e) => e.stopPropagation()}
                          className={cn(buttonVariants({ variant: "ghost", size: "icon" }))}
                        >
                          <MessageSquare className="size-4" />
                        </a>
                      </div>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Pager page={page} totalPages={totalPages} totalCount={totalCount} noun="follow-ups" onChange={setPage} />
    </>
  )
}

function ReportsPage({
  leadStats,
  callStats,
  onTopUp,
  vertical,
}: {
  leadStats: any
  callStats: any
  onTopUp: () => void
  vertical: VerticalFilter
}) {
  const { data: sources, isLoading: sourcesLoading } = useSources(vertical)
  const { data: weekly, isLoading: weeklyLoading } = useWeekly(vertical)
  const { data: quality, isLoading: qualityLoading } = useQuality(30, vertical)
  const [exporting, setExporting] = useState(false)
  // Performance and billing answer different questions, so they get their own
  // views rather than one endlessly scrolling page. Segmented control rather
  // than TabsContent, matching how Tabs is already used elsewhere in this file.
  const [tab, setTab] = useState("performance")
  const { data: credits } = useCredits()

  const pieData = useMemo(
    () => sources.map((row, i) => ({ name: row.source, value: row.count, fill: CHART_COLORS[i % CHART_COLORS.length] })),
    [sources],
  )

  const pieConfig = useMemo(
    () =>
      Object.fromEntries(
        pieData.map((row, i) => [row.name, { label: row.name, color: CHART_COLORS[i % CHART_COLORS.length] }]),
      ) as ChartConfig,
    [pieData],
  )

  const barConfig = {
    leads: { label: "Leads", color: "var(--chart-1)" },
    calls: { label: "Calls", color: "var(--chart-2)" },
    qualified: { label: "Qualified", color: "var(--chart-3)" },
  } satisfies ChartConfig

  const outcomes = (callStats?.by_outcome ?? {}) as Record<string, number>
  const outcomeRows = Object.entries(outcomes).sort((a, b) => b[1] - a[1])
  const totalCalls = callStats?.total ?? 0
  const qualificationRate =
    (leadStats?.total ?? 0) > 0 ? (((leadStats?.qualified ?? 0) / leadStats.total) * 100).toFixed(1) : "0"
  const bestSource = sources[0]

  /** Full lead export straight from the database, not the 7 points on the chart. */
  const downloadLeadExport = async () => {
    setExporting(true)
    try {
      // Exports what is on screen, not everything - a "Solar" report that
      // downloads all four business lines is a quiet way to hand the client
      // the wrong list.
      const res = await fetch(`/api/reports/export?vertical=${vertical}`)
      if (res.status === 404) {
        toast.info("There are no leads to export yet.")
        return
      }
      if (!res.ok) throw new Error(`Export failed (${res.status})`)
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const link = document.createElement("a")
      link.href = url
      link.download = `leads-export-${new Date().toISOString().slice(0, 10)}.csv`
      link.click()
      URL.revokeObjectURL(url)
      toast.success("Lead export downloaded")
    } catch (err: any) {
      toast.error(err.message || "Export failed")
    } finally {
      setExporting(false)
    }
  }

  return (
    <>
      {/* Reports hero. The four rates that actually answer "is this working?"
          are pulled out of the charts and set large across an editorial band,
          so the answer is legible before any chart is read. */}
      <section className="ambient-wash relative -mx-4 -mt-4 overflow-hidden border-b border-border/60 px-4 pb-8 pt-10 md:-mx-8 md:-mt-8 md:px-8 md:pb-10 md:pt-14">
        <div className="relative z-10">
          <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between">
            <div className="min-w-0">
              <p className="motion-fade text-[10px] font-semibold uppercase tracking-[0.2em] text-muted-foreground">
                Performance
              </p>
              <h1 className="motion-rise mt-3 text-balance font-display text-4xl font-semibold leading-[1.05] tracking-tight md:text-5xl lg:text-6xl">
                Reports
                <span className="text-muted-foreground"> &amp; analytics</span>
              </h1>
              <p className="motion-rise mt-4 max-w-[52ch] text-pretty text-sm leading-relaxed text-muted-foreground md:text-base">
                Where your leads come from, how the agent performs on the phone, and what it gets wrong.
              </p>
            </div>
            <div className="flex shrink-0 flex-wrap items-center gap-2">
              {/* Billing moved out to its own sidebar destination, so there is
                  exactly one place to read charges rather than two that could
                  disagree. This page is purely performance now. */}
              <Button
                variant="outline"
                onClick={downloadLeadExport}
                disabled={exporting}
                className="motion-fade"
              >
                <Download data-icon="inline-start" />
                {exporting ? "Preparing…" : "Export all leads"}
              </Button>
            </div>
          </div>

          {/* Headline rates, divided rather than boxed — cards here would add
              four more containers to a page that is already all containers. */}
          <dl className="stagger mt-10 grid grid-cols-2 gap-x-6 gap-y-8 border-t border-border/60 pt-8 lg:grid-cols-4 lg:divide-x lg:divide-border/60">
            {([
                  {
                    label: "Qualification rate",
                    value: `${qualificationRate}%`,
                    sub: `${(leadStats?.qualified ?? 0).toLocaleString()} of ${(leadStats?.total ?? 0).toLocaleString()} leads`,
                  },
                  {
                    label: "Connect rate",
                    value: totalCalls > 0 ? `${Math.round(((callStats?.connected ?? 0) / totalCalls) * 100)}%` : "—",
                    sub: `${(callStats?.connected ?? 0).toLocaleString()} of ${totalCalls.toLocaleString()} calls`,
                  },
                  {
                    label: "Avg. connected call",
                    value: formatDuration(callStats?.avg_duration),
                    sub: "Time on the phone",
                  },
                  {
                    label: "Top source",
                    value: bestSource?.source ?? "—",
                    sub: bestSource ? `${bestSource.count.toLocaleString()} leads` : "No leads yet",
                  },
                ]
            ).map((kpi, i) => (
              <div key={kpi.label} className={cn("min-w-0", i > 0 && "lg:pl-6")}>
                <dt className="text-[10px] font-semibold uppercase tracking-[0.16em] text-muted-foreground">
                  {kpi.label}
                </dt>
                <dd className="mt-2.5 truncate font-display text-3xl font-semibold tracking-tight tabular-nums md:text-4xl">
                  {kpi.value}
                </dd>
                <dd className="mt-1.5 truncate text-xs text-muted-foreground">{kpi.sub}</dd>
              </div>
            ))}
          </dl>
        </div>
      </section>

      <>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Lead source breakdown</CardTitle>
            <CardDescription>Distribution across channels</CardDescription>
          </CardHeader>
          <CardContent>
            {pieData.length === 0 ? (
              <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
                {sourcesLoading ? "Loading…" : "No leads imported yet."}
              </div>
            ) : (
              <>
                <div className="relative">
                <ChartContainer config={pieConfig} className="mx-auto h-64 w-full">
                  <PieChart>
                    <ChartTooltip content={<ChartTooltipContent />} />
                    {/* Donut rather than pie: the hole carries the total, and
                        the thinner ring reads as a considered chart instead of
                        a default one. Slice labels are dropped in favour of the
                        legend below — they collided at narrow widths. */}
                    <Pie
                      data={pieData}
                      dataKey="value"
                      nameKey="name"
                      cx="50%"
                      cy="50%"
                      innerRadius={58}
                      outerRadius={92}
                      paddingAngle={2}
                      cornerRadius={4}
                      strokeWidth={0}
                      animationDuration={900}
                    >
                      {pieData.map((entry, i) => (
                        <Cell key={i} fill={entry.fill} />
                      ))}
                    </Pie>
                  </PieChart>
                </ChartContainer>
                {/* Total sits in the donut's hole, absolutely centred over the
                    ring it belongs to, so the part-to-whole relationship reads
                    without the eye leaving the chart. */}
                <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
                  <span className="font-display text-3xl font-semibold tabular-nums">
                    {pieData.reduce((sum, s) => sum + s.value, 0).toLocaleString()}
                  </span>
                  <span className="mt-0.5 text-[10px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
                    Leads
                  </span>
                </div>
                </div>
                <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-2 border-t border-border/60 pt-4 text-xs">
                  {pieData.map((src, i) => (
                    <span key={i} className="flex items-center gap-2">
                      <i className="size-2 shrink-0 rounded-full" style={{ background: src.fill }} />
                      <span className="text-muted-foreground">{src.name}</span>
                      <span className="font-medium tabular-nums">{src.value}</span>
                    </span>
                  ))}
                </div>
              </>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Weekly performance</CardTitle>
            <CardDescription>Leads, calls, and qualifications by week</CardDescription>
          </CardHeader>
          <CardContent>
            {weekly.length === 0 ? (
              <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
                {weeklyLoading ? "Loading…" : "No activity in the last four weeks."}
              </div>
            ) : (
              <ChartContainer config={barConfig} className="h-64 w-full">
                <BarChart data={weekly} margin={{ left: -20, right: 4 }}>
                  <CartesianGrid vertical={false} />
                  <XAxis dataKey="week" tickLine={false} axisLine={false} />
                  <YAxis tickLine={false} axisLine={false} allowDecimals={false} />
                  <ChartTooltip content={<ChartTooltipContent />} />
                  <Bar dataKey="leads" fill="var(--chart-1)" radius={[4, 4, 0, 0]} />
                  <Bar dataKey="calls" fill="var(--chart-2)" radius={[4, 4, 0, 0]} />
                  <Bar dataKey="qualified" fill="var(--chart-3)" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ChartContainer>
            )}
          </CardContent>
        </Card>
      </div>

      {/* The feedback loop: Dograh's QA node grades every sampled call and these
          are the recurring faults, worst first, each with the lever that fixes it. */}
      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-3">
          <div>
            <CardTitle>What the agent is getting wrong</CardTitle>
            <CardDescription>
              {quality
                ? `${quality.calls_reviewed} of ${quality.calls_total} calls reviewed by QA in the last 30 days` +
                  (quality.avg_quality_score !== null ? ` · average quality ${quality.avg_quality_score}/10` : '')
                : 'Automatic QA review of every call'}
            </CardDescription>
          </div>
          {quality?.avg_quality_score !== null && quality?.avg_quality_score !== undefined && (
            <Badge variant={quality.avg_quality_score >= 7 ? 'default' : 'destructive'}>
              {quality.avg_quality_score}/10
            </Badge>
          )}
        </CardHeader>
        <CardContent className="overflow-x-auto p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Problem</TableHead>
                <TableHead>Calls</TableHead>
                <TableHead>Example</TableHead>
                <TableHead>How to fix</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody className="stagger-rows">
              {!quality || quality.issues.length === 0 ? (
                <EmptyRow colSpan={4}>
                  {qualityLoading
                    ? 'Loading QA review…'
                    : quality && quality.calls_reviewed === 0
                      ? 'No calls have been QA-reviewed yet. Reviews appear after the next calls complete.'
                      : 'No problems detected in the reviewed calls.'}
                </EmptyRow>
              ) : (
                quality.issues.map((issue) => (
                  <TableRow key={issue.tag}>
                    <TableCell>
                      <p className="font-medium capitalize">{issue.label}</p>
                      <p className="font-mono text-xs text-muted-foreground">{issue.tag}</p>
                    </TableCell>
                    <TableCell>
                      <span className="font-semibold">{issue.count}</span>
                      <span className="ml-1 text-xs text-muted-foreground">({issue.share}%)</span>
                    </TableCell>
                    <TableCell className="max-w-sm">
                      <span className="text-sm text-muted-foreground">{issue.example || '—'}</span>
                    </TableCell>
                    <TableCell className="max-w-xs">
                      <span className="text-sm">{issue.fix || '—'}</span>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Call outcomes</CardTitle>
          <CardDescription>
            {/* The old "Agent performance" table listed invented agents with made-up
                satisfaction scores; nothing in the schema records per-agent metrics. */}
            Every recorded call, grouped by how it ended
          </CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Outcome</TableHead>
                <TableHead>Calls</TableHead>
                <TableHead>Share</TableHead>
                <TableHead className="text-right">Distribution</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody className="stagger-rows">
              {outcomeRows.length === 0 ? (
                <EmptyRow colSpan={4}>No calls recorded yet.</EmptyRow>
              ) : (
                outcomeRows.map(([outcome, count]) => {
                  const share = totalCalls > 0 ? Math.round((count / totalCalls) * 100) : 0
                  return (
                    <TableRow key={outcome}>
                      <TableCell className="font-medium">{OUTCOME_LABELS[outcome] || outcome}</TableCell>
                      <TableCell>{count}</TableCell>
                      <TableCell>{share}%</TableCell>
                      <TableCell>
                        <div className="flex justify-end">
                          {/* A fixed 128px bar was the last 13px of overflow on
                              this table. It is a proportion, so it can be any
                              width. */}
                          <Progress value={share} className="h-2 w-16 sm:w-32" />
                        </div>
                      </TableCell>
                    </TableRow>
                  )
                })
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardContent className="p-5">
            <div className="flex items-center gap-3">
              <TrendingUp className="size-5 text-primary" />
              <div>
                <p className="font-medium">Largest lead source</p>
                <p className="text-sm text-muted-foreground">
                  {bestSource ? `${bestSource.source} — ${bestSource.count} leads` : "No leads yet"}
                </p>
              </div>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-5">
            <div className="flex items-center gap-3">
              <Target className="size-5 text-primary" />
              <div>
                <p className="font-medium">Qualification rate</p>
                <p className="text-sm text-muted-foreground">
                  {qualificationRate}% of {leadStats?.total ?? 0} leads
                </p>
              </div>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-5">
            <div className="flex items-center gap-3">
              <Zap className="size-5 text-primary" />
              <div>
                <p className="font-medium">Connect rate</p>
                <p className="text-sm text-muted-foreground">
                  {callStats?.connect_rate ?? 0}% of {totalCalls} calls answered
                </p>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>
      </>
    </>
  )
}

/**
 * Only the blocks the loaded agent actually has are rendered, so this list is a
 * superset of two different agent shapes.
 *
 * The four BS Wealth agents on Vaani are single-prompt: one node holding an
 * opening line and a script, and no global/agenda/end nodes at all. They
 * therefore show "Opening line" and "Agent script" and nothing else. The older
 * four-node agents show the other three instead and have no separate script.
 */
const PROMPT_BLOCKS: { key: "global" | "start" | "script" | "agenda" | "end"; label: string; hint: string }[] = [
  { key: "global", label: "Global rules", hint: "Applied to every turn of the call — tone, language, what never to say." },
  { key: "start", label: "Opening line", hint: "The first thing the agent says, word for word, before the customer has spoken." },
  {
    key: "script",
    label: "Agent script",
    hint: "Everything after the opening line — what to ask, in what order, how to handle objections, and when to hand off.",
  },
  { key: "agenda", label: "Main questions", hint: "What the agent asks once the customer shows interest." },
  { key: "end", label: "Closing line", hint: "The last thing the agent says before hanging up." },
]

function AIAgentPage({
  leadStats,
  callStats,
  vertical,
}: {
  leadStats: any
  callStats: any
  vertical: VerticalFilter
}) {
  // Which agent's script this page edits. "All" is not a thing that can have a
  // script, so it resolves to Loan - the same default the API uses.
  const promptVertical: Vertical = vertical === "all" ? DEFAULT_VERTICAL : vertical
  const { settings, updateSetting, isLoading } = useSettings()
  const [form, setForm] = useState({
    language: "Telugu",
    voice: "Female — Natural",
    maxRetries: "2",
    callGap: "30",
  })
  const [saving, setSaving] = useState<string | null>(null)

  // Hydrate from the database once the settings arrive, so a reload no longer
  // resets everything to the hard-coded defaults it used to display.
  // maxRetries/callGap live under the separate "call_behavior" settings key
  // (that's what the retry sweep actually reads) - hydrating only ai_agent
  // meant a saved value here was never shown again after a reload.
  useEffect(() => {
    setForm((prev) => ({
      ...prev,
      ...(settings?.ai_agent ?? {}),
      ...(settings?.call_behavior?.maxRetries !== undefined ? { maxRetries: String(settings.call_behavior.maxRetries) } : {}),
      ...(settings?.call_behavior?.callGapMinutes !== undefined
        ? { callGap: String(settings.call_behavior.callGapMinutes) }
        : {}),
    }))
  }, [settings])

  // --- Live agent prompts ---------------------------------------------------
  // Previously "Opening greeting" saved to our own settings table and never
  // touched the live agent at all - the button worked, it just didn't do what
  // it looked like it did. This reads and writes the actual Dograh workflow.
  const [agents, setAgents] = useState<any[]>([])
  const [prompts, setPrompts] = useState<Record<string, string>>({})
  const [promptVersion, setPromptVersion] = useState<number | null>(null)
  const [promptsLoading, setPromptsLoading] = useState(false)
  const [promptsError, setPromptsError] = useState<string | null>(null)
  const [savingPrompt, setSavingPrompt] = useState<string | null>(null)

  const loadPrompts = useCallback(async () => {
    setPromptsLoading(true)
    setPromptsError(null)
    try {
      const res = await fetch(`/api/agent/prompts?vertical=${promptVertical}`)
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.error || `Failed to load agent prompts (${res.status})`)
      setPrompts(data.prompts || {})
      setPromptVersion(data.version ?? null)
    } catch (err: any) {
      setPromptsError(err.message || "Could not load the live agent prompts")
    } finally {
      setPromptsLoading(false)
    }
  }, [promptVertical])

  useEffect(() => {
    loadPrompts()
  }, [loadPrompts])

  useEffect(() => {
    let cancelled = false
    fetch("/api/agent/agents")
      .then((r) => r.json())
      .then((d) => {
        if (!cancelled) setAgents(d.agents ?? [])
      })
      .catch(() => {
        /* the card falls back to "Checking agents…"; a failed probe is not
           worth a toast on a page the user came to for something else */
      })
    return () => {
      cancelled = true
    }
  }, [])

  const savePrompt = async (key: string) => {
    setSavingPrompt(key)
    try {
      // Same business line the prompts were LOADED with. Saving to a different
      // agent than the one on screen would rewrite what a live agent says.
      const res = await fetch(`/api/agent/prompts?vertical=${promptVertical}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [key]: prompts[key] }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.error || `Failed to save (${res.status})`)
      setPromptVersion(data.version ?? promptVersion)
      toast.success(`Saved and published (v${data.version ?? "?"})`)
    } catch (err: any) {
      toast.error(err.message || "Could not save the prompt")
    } finally {
      setSavingPrompt(null)
    }
  }

  const save = async (key: string, value: any, label: string) => {
    setSaving(key)
    try {
      await updateSetting(key, value)
      toast.success(`${label} saved`)
    } catch (err: any) {
      // The old handlers fired a success toast without calling anything at all.
      toast.error(err.message || `Could not save ${label.toLowerCase()}`)
    } finally {
      setSaving(null)
    }
  }

  const connectRate = callStats?.connect_rate ?? 0
  const qualificationRate =
    (leadStats?.total ?? 0) > 0 ? (((leadStats?.qualified ?? 0) / leadStats.total) * 100).toFixed(1) : "0"

  return (
    <>
      <section>
        <h1 className="font-display text-3xl font-semibold tracking-tight md:text-4xl">AI Agent Configuration</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Voice agent preferences. Prompts and voices live in the Dograh workflow; these values are stored here for your
          team&apos;s reference and used by the retry rules.
        </p>
      </section>

      {/* Which business lines actually have an agent. Without this the only way
          to discover that Solar has no agent was to try launching a campaign
          and have it refused. */}
      <Card>
        <CardHeader>
          <CardTitle>Agents by business line</CardTitle>
          <CardDescription>
            A campaign can only run for a business line that has an agent. The others are refused rather than
            called by the wrong script.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-2 sm:grid-cols-2">
          {agents.length === 0 ? (
            <p className="text-sm text-muted-foreground">Checking agents…</p>
          ) : (
            agents.map((agent) => (
              <div
                key={agent.vertical}
                className="flex items-start justify-between gap-3 rounded-lg border border-border/70 p-3"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <Badge
                      variant="outline"
                      className={cn(
                        "border-transparent",
                        VERTICAL_STYLES[agent.vertical as Vertical]?.color,
                        VERTICAL_STYLES[agent.vertical as Vertical]?.bg,
                      )}
                    >
                      {agent.label}
                    </Badge>
                    {agent.reachable ? (
                      <span className="text-xs font-medium text-emerald-500">Ready</span>
                    ) : agent.configured ? (
                      <span className="text-xs font-medium text-destructive">Not responding</span>
                    ) : (
                      <span className="text-xs text-muted-foreground">Not built yet</span>
                    )}
                  </div>
                  <p className="mt-1 truncate text-xs text-muted-foreground">
                    {agent.name ?? agent.detail}
                  </p>
                </div>
                {agent.workflow_id != null && (
                  <span className="shrink-0 text-xs text-muted-foreground">#{agent.workflow_id}</span>
                )}
              </div>
            ))
          )}
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Live agent metrics</CardTitle>
            <CardDescription>Real numbers from your call history</CardDescription>
          </CardHeader>
          <CardContent className="grid grid-cols-2 gap-3">
            {/* These four tiles used to be hard-coded (142 calls, 3:12, 54.9%, 14.8%). */}
            <div className="rounded-lg bg-secondary p-3">
              <p className="text-xs text-muted-foreground">Calls today</p>
              <p className="text-xl font-semibold">{callStats?.today ?? 0}</p>
            </div>
            <div className="rounded-lg bg-secondary p-3">
              <p className="text-xs text-muted-foreground">Avg. call duration</p>
              <p className="text-xl font-semibold">{formatDuration(callStats?.avg_duration)}</p>
            </div>
            <div className="rounded-lg bg-secondary p-3">
              <p className="text-xs text-muted-foreground">Connect rate</p>
              <p className="text-xl font-semibold">{connectRate}%</p>
            </div>
            <div className="rounded-lg bg-secondary p-3">
              <p className="text-xs text-muted-foreground">Qualification rate</p>
              <p className="text-xl font-semibold">{qualificationRate}%</p>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Voice &amp; language</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <div className="grid gap-2">
              <label className="text-sm font-medium" htmlFor="agent-language">
                Language
              </label>
              <select
                id="agent-language"
                className="flex h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-sm sm:h-10"
                value={form.language}
                onChange={(e) => setForm({ ...form, language: e.target.value })}
              >
                {["Telugu", "Hindi", "English", "Tamil", "Kannada"].map((lang) => (
                  <option key={lang}>{lang}</option>
                ))}
              </select>
            </div>
            <div className="grid gap-2">
              <label className="text-sm font-medium" htmlFor="agent-voice">
                Voice profile
              </label>
              <select
                id="agent-voice"
                className="flex h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-sm sm:h-10"
                value={form.voice}
                onChange={(e) => setForm({ ...form, voice: e.target.value })}
              >
                {["Female — Natural", "Male — Natural", "Female — Professional", "Male — Professional"].map((voice) => (
                  <option key={voice}>{voice}</option>
                ))}
              </select>
            </div>
            <Button size="sm" disabled={saving === "ai_agent" || isLoading} onClick={() => save("ai_agent", form, "Voice settings")}>
              <Volume2 data-icon="inline-start" />
              {saving === "ai_agent" ? "Saving…" : "Save voice settings"}
            </Button>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-3">
          <div>
            {/* Names the business line explicitly. With four agents, "Agent
                script" alone gave no clue which one was on screen - and the
                page follows the header switch, which is not obvious from here. */}
            <CardTitle>{VERTICAL_LABELS[promptVertical]} agent script</CardTitle>
            <CardDescription>
              What the {VERTICAL_LABELS[promptVertical]} agent actually says, live in Dograh. Saving publishes
              immediately — the next call uses the new text. Switch business line at the top of the page to edit a
              different agent.
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            {promptVersion !== null && <Badge variant="outline">v{promptVersion}</Badge>}
            <Button variant="ghost" size="sm" onClick={loadPrompts} disabled={promptsLoading}>
              <RefreshCw data-icon="inline-start" className={cn(promptsLoading && "animate-spin")} />
              Reload
            </Button>
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          {promptsError && (
            <div className="rounded-lg border border-destructive/50 bg-destructive/5 p-3">
              <p className="text-sm font-medium text-destructive">
                Could not load the {VERTICAL_LABELS[promptVertical]} agent&apos;s script
              </p>
              <p className="mt-1 text-xs text-muted-foreground">{promptsError}</p>
              {/* The overwhelmingly common cause is a workflow id that is set
                  locally but missing from the deployed environment - which shows
                  up as a blank script page and nothing else. Say so, rather than
                  leaving a bare provider error. */}
              <p className="mt-2 text-xs text-muted-foreground">
                If this business line has an agent built, its workflow id is probably missing from the deployed
                environment. It needs <code className="font-mono">DOGRAH_WORKFLOW_ID_{promptVertical.toUpperCase()}</code>{" "}
                set where the dashboard is hosted, not only in local development.
              </p>
            </div>
          )}
          {promptsLoading && !promptsError && Object.keys(prompts).length === 0 ? (
            <p className="text-sm text-muted-foreground">Loading the live script…</p>
          ) : (
            // Only what this agent actually has. Rendering all five would put
            // three permanently empty boxes ("Global rules", "Main questions",
            // "Closing line") under every single-prompt agent, which reads as
            // the script having gone missing rather than the agent not having
            // those parts.
            PROMPT_BLOCKS.filter((block) => prompts[block.key] !== undefined).map((block) => (
              <div key={block.key} className="grid gap-2">
                <div className="flex items-center justify-between">
                  <label className="text-sm font-medium" htmlFor={`prompt-${block.key}`}>
                    {block.label}
                  </label>
                  <Button
                    size="sm"
                    disabled={savingPrompt === block.key || !prompts[block.key]?.trim()}
                    onClick={() => savePrompt(block.key)}
                  >
                    {savingPrompt === block.key ? "Publishing…" : "Save & publish"}
                  </Button>
                </div>
                <p className="text-xs text-muted-foreground">{block.hint}</p>
                <textarea
                  id={`prompt-${block.key}`}
                  // The script runs to several thousand characters; the opening
                  // line is one sentence. Same box for both makes the script
                  // unreadable and unreviewable.
                  className={`flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm ${
                    block.key === "script" ? "min-h-[420px] font-mono leading-relaxed" : "min-h-[110px]"
                  }`}
                  value={prompts[block.key] ?? ""}
                  onChange={(e) => setPrompts((prev) => ({ ...prev, [block.key]: e.target.value }))}
                />
              </div>
            ))
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Calling limits</CardTitle>
          <CardDescription>
            The hard cap on how often one person is dialled. This governs every campaign — a campaign&apos;s own
            redial setting can only go lower, never higher.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-2">
          <div className="grid gap-2">
            <label className="text-sm font-medium" htmlFor="max-retries">
              Max calls per person (total)
            </label>
            <Input
              id="max-retries"
              type="number"
              min={0}
              max={10}
              value={form.maxRetries}
              onChange={(e) => setForm({ ...form, maxRetries: e.target.value })}
            />
            <p className="text-xs text-muted-foreground">
              Counts the first call and every redial, across all campaigns. Set it to 2 and nobody is ever rung more
              than twice. After that the lead moves to Unreachable.
            </p>
          </div>
          <div className="grid gap-2">
            <label className="text-sm font-medium" htmlFor="call-gap">
              Gap between retries (min)
            </label>
            <Input
              id="call-gap"
              type="number"
              min={0}
              max={1440}
              value={form.callGap}
              onChange={(e) => setForm({ ...form, callGap: e.target.value })}
            />
            <p className="text-xs text-muted-foreground">
              A retry-pending lead won&apos;t show as callable again until this long after the last attempt.
            </p>
          </div>
          <div className="md:col-span-2">
            <Button
              size="sm"
              disabled={saving === "call_behavior"}
              onClick={() =>
                save(
                  "call_behavior",
                  { maxRetries: Number(form.maxRetries) || 0, callGapMinutes: Number(form.callGap) || 0 },
                  "Retry sweep settings",
                )
              }
            >
              {saving === "call_behavior" ? "Saving…" : "Save settings"}
            </Button>
          </div>
        </CardContent>
      </Card>
    </>
  )
}

function SettingsPage({ health, onLogout }: { health: any; onLogout: () => void }) {
  const { settings, updateSetting, isLoading } = useSettings()
  const [workspace, setWorkspace] = useState({
    companyName: "BS Wealth Finance",
    projectName: "Loan campaign",
    timezone: "Asia/Kolkata",
  })
  const [notifications, setNotifications] = useState({ email: true, whatsapp: true })
  const [saving, setSaving] = useState<string | null>(null)
  const [isDark, setIsDark] = useState(true)

  useEffect(() => {
    if (settings?.workspace) setWorkspace((prev) => ({ ...prev, ...settings.workspace }))
    if (settings?.notifications) setNotifications((prev) => ({ ...prev, ...settings.notifications }))
  }, [settings])

  useEffect(() => {
    setIsDark(document.documentElement.classList.contains("dark"))
  }, [])

  const save = async (key: string, value: any, label: string) => {
    setSaving(key)
    try {
      await updateSetting(key, value)
      toast.success(`${label} saved`)
    } catch (err: any) {
      toast.error(err.message || `Could not save ${label.toLowerCase()}`)
    } finally {
      setSaving(null)
    }
  }

  const toggleNotification = async (channel: "email" | "whatsapp") => {
    const next = { ...notifications, [channel]: !notifications[channel] }
    setNotifications(next)
    await save("notifications", next, "Notification preferences")
  }

  /** Actually flips the theme instead of only toasting about it. */
  const toggleTheme = () => {
    const next = !isDark
    setIsDark(next)
    document.documentElement.classList.toggle("dark", next)
    updateSetting("appearance", { theme: next ? "dark" : "light" }).catch(() => {
      /* Theme is applied locally regardless of whether the preference persists. */
    })
  }

  const serviceRows = [
    { key: "supabase", label: "Supabase" },
    { key: "dograh", label: "Dograh calling API" },
    { key: "webhook", label: "Call-result webhook" },
    { key: "n8n", label: "n8n lead import" },
    { key: "cron", label: "Reconcile cron job" },
    { key: "auth", label: "Login protection" },
  ]

  return (
    <>
      <section>
        <h1 className="font-display text-3xl font-semibold tracking-tight md:text-4xl">Settings</h1>
        <p className="mt-1 text-sm text-muted-foreground">Manage your workspace, integrations, and preferences.</p>
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Workspace</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <div className="grid gap-2">
              <label className="text-sm font-medium" htmlFor="company-name">
                Company name
              </label>
              <Input
                id="company-name"
                value={workspace.companyName}
                onChange={(e) => setWorkspace({ ...workspace, companyName: e.target.value })}
              />
            </div>
            <div className="grid gap-2">
              <label className="text-sm font-medium" htmlFor="project-name">
                Project name
              </label>
              <Input
                id="project-name"
                value={workspace.projectName}
                onChange={(e) => setWorkspace({ ...workspace, projectName: e.target.value })}
              />
            </div>
            <div className="grid gap-2">
              <label className="text-sm font-medium" htmlFor="timezone">
                Timezone
              </label>
              <select
                id="timezone"
                className="flex h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-sm sm:h-10"
                value={workspace.timezone}
                onChange={(e) => setWorkspace({ ...workspace, timezone: e.target.value })}
              >
                <option value="Asia/Kolkata">Asia/Kolkata (IST)</option>
                <option value="America/New_York">America/New York (EST)</option>
                <option value="Europe/London">Europe/London (GMT)</option>
                <option value="Asia/Dubai">Asia/Dubai (GST)</option>
              </select>
            </div>
            <Button
              size="sm"
              className="self-start"
              disabled={saving === "workspace" || isLoading}
              onClick={() => save("workspace", workspace, "Workspace settings")}
            >
              {saving === "workspace" ? "Saving…" : "Save changes"}
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Integrations</CardTitle>
            <CardDescription>Checked live — not a static list</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {serviceRows.map((row) => {
              const service = health?.services?.[row.key]
              return (
                <div key={row.key} className="flex items-center justify-between gap-3 rounded-lg border p-3">
                  <div>
                    <p className="text-sm font-medium">{row.label}</p>
                    <p className="text-xs text-muted-foreground">{service?.detail ?? "Checking…"}</p>
                  </div>
                  <HealthBadge service={service} />
                </div>
              )
            })}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Notifications</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex items-center justify-between rounded-lg border p-4">
            <div>
              <p className="text-sm font-medium">Email notifications</p>
              <p className="text-xs text-muted-foreground">Receive daily summary and qualified lead alerts</p>
            </div>
            <Button
              variant={notifications.email ? "default" : "outline"}
              size="sm"
              disabled={saving === "notifications"}
              onClick={() => toggleNotification("email")}
            >
              {notifications.email ? "Enabled" : "Disabled"}
            </Button>
          </div>
          <div className="flex items-center justify-between rounded-lg border p-4">
            <div>
              <p className="text-sm font-medium">WhatsApp alerts</p>
              <p className="text-xs text-muted-foreground">Instant alerts for qualified leads</p>
            </div>
            <Button
              variant={notifications.whatsapp ? "default" : "outline"}
              size="sm"
              disabled={saving === "notifications"}
              onClick={() => toggleNotification("whatsapp")}
            >
              {notifications.whatsapp ? "Enabled" : "Disabled"}
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            Preferences are stored for the automation workflows to read; delivery itself is handled by n8n.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Appearance</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-center justify-between rounded-lg border p-4">
            <div>
              <p className="text-sm font-medium">Dark mode</p>
              <p className="text-xs text-muted-foreground">Applies immediately</p>
            </div>
            <Button variant={isDark ? "default" : "outline"} size="sm" onClick={toggleTheme}>
              {isDark ? "Dark" : "Light"}
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Session</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-center justify-between rounded-lg border p-4">
            <div>
              <p className="text-sm font-medium">Sign out</p>
              <p className="text-xs text-muted-foreground">End this session on this device</p>
            </div>
            <Button variant="outline" size="sm" onClick={onLogout}>
              <LogOut data-icon="inline-start" />
              Sign out
            </Button>
          </div>
        </CardContent>
      </Card>
    </>
  )
}

// ─── LEAD DETAIL PANEL ─────────────────────────────────

function TranscriptView({ callId }: { callId: string | null }) {
  const { messages, text, isLoading, error } = useTranscript(callId)

  if (!callId) return null
  if (isLoading) return <p className="text-sm text-muted-foreground">Loading transcript…</p>
  if (error) return <p className="text-sm text-destructive">{error}</p>
  if (messages && messages.length > 0) {
    // Turn times let you see where the agent stalled — a long gap before a
    // reply is the thing you are usually hunting for when reading one of these.
    const startedAt = messages.find((m) => m.at)?.at
    const offsetOf = (at?: string) => {
      if (!at || !startedAt) return null
      const seconds = Math.round((new Date(at).getTime() - new Date(startedAt).getTime()) / 1000)
      if (!Number.isFinite(seconds) || seconds < 0) return null
      return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`
    }

    return (
      <div className="flex max-h-80 flex-col gap-3 overflow-y-auto pr-1">
        {messages.map((msg, i) => {
          const offset = offsetOf(msg.at)
          return (
            <div key={i} className={cn("flex flex-col", msg.speaker === "Agent" ? "items-start" : "items-end")}>
              <span className="mb-1 flex items-center gap-1.5 text-xs text-muted-foreground">
                {msg.speaker === "Agent" ? "Shreya" : "Customer"}
                {offset && <span className="tabular-nums opacity-70">{offset}</span>}
              </span>
              <div
                className={cn(
                  "max-w-[85%] whitespace-pre-wrap rounded-lg px-3 py-2 text-sm",
                  msg.speaker === "Agent" ? "bg-primary text-primary-foreground" : "bg-muted",
                )}
              >
                {msg.text}
              </div>
            </div>
          )
        })}
      </div>
    )
  }
  if (text) {
    return <pre className="max-h-80 overflow-y-auto whitespace-pre-wrap text-sm text-muted-foreground">{text}</pre>
  }
  return <p className="text-sm text-muted-foreground">No transcript was stored for this call.</p>
}

function LeadDetailSheet({
  leadId,
  focusCallId,
  onClose,
  onSaved,
}: {
  leadId: string | null
  /** Set when opened from the Calls list: show only that call, not every attempt. */
  focusCallId?: string | null
  onClose: () => void
  onSaved: () => void
}) {
  const { lead, callHistory, otherLines, isLoading, refresh } = useLead(leadId)
  const [openTranscriptId, setOpenTranscriptId] = useState<string | null>(null)
  // Opening from a specific call and being shown eleven attempts is disorienting
  // — you clicked one row and expected that row. The rest stay one click away.
  const [showAllCalls, setShowAllCalls] = useState(false)

  // Only narrow when the requested call is actually in this lead's history —
  // otherwise a stale id would render an empty "This call" panel.
  const focusedOnly =
    Boolean(focusCallId) && !showAllCalls && callHistory.some((c: any) => c.id === focusCallId)
  const visibleCalls = focusedOnly
    ? callHistory.filter((c: any) => c.id === focusCallId)
    : callHistory

  // A fresh row should reopen focused, and its transcript should already be
  // open — reading it is why you clicked.
  useEffect(() => {
    setShowAllCalls(false)
    setOpenTranscriptId(focusCallId ?? null)
  }, [focusCallId, leadId])
  const [followUp, setFollowUp] = useState("")
  const [note, setNote] = useState("")
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    setOpenTranscriptId(null)
    setNote("")
    setFollowUp(lead?.follow_up_date ? String(lead.follow_up_date).slice(0, 10) : "")
  }, [lead?.id, lead?.follow_up_date])

  const score = lead?.score ?? 0
  const qualData = (lead?.qual_data ?? {}) as Record<string, any>

  const saveChanges = async () => {
    if (!lead) return
    setSaving(true)
    try {
      const patch: Record<string, any> = {}
      // "" is sent deliberately when a date is cleared — the API turns it into
      // null. Only skipping falsy values meant a follow-up could be set but
      // never removed once it was wrong.
      const existingFollowUp = lead.follow_up_date ? String(lead.follow_up_date).slice(0, 10) : ""
      if (followUp !== existingFollowUp) patch.follow_up_date = followUp
      if (note.trim()) {
        const stamp = new Date().toISOString().slice(0, 16).replace("T", " ")
        patch.notes = [lead.notes, `[${stamp}] ${note.trim()}`].filter(Boolean).join("\n")
      }
      if (Object.keys(patch).length === 0) {
        toast.info("Nothing to save")
        return
      }
      await updateLead(lead.id, patch)
      toast.success("Lead updated")
      setNote("")
      refresh()
      onSaved()
    } catch (err: any) {
      toast.error(err.message || "Could not save the lead")
    } finally {
      setSaving(false)
    }
  }

  const copyPhone = async () => {
    if (!lead?.phone) return
    try {
      await navigator.clipboard.writeText(lead.phone)
      toast.success("Phone number copied")
    } catch {
      toast.error("Could not copy the number")
    }
  }

  return (
    <Sheet open={!!leadId} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full sm:max-w-lg">
        {/* Identity lives in the header rather than being repeated below it —
            the panel previously said "Lead Details" then the name, then the
            name again on an avatar row, which is three headings for one record. */}
        <SheetHeader>
          <SheetTitle>{lead?.name || (isLoading ? "Loading…" : "Lead details")}</SheetTitle>
          <SheetDescription>
            {lead ? lead.phone : isLoading ? "Fetching record" : "No lead selected"}
          </SheetDescription>
        </SheetHeader>

        <SheetBody className="flex flex-col gap-6">
        {isLoading && !lead && (
          <div className="flex flex-col gap-4">
            <Skeleton className="h-16 w-full rounded-xl" />
            <Skeleton className="h-9 w-full rounded-lg" />
            <Skeleton className="h-24 w-full rounded-xl" />
            <Skeleton className="h-24 w-full rounded-xl" />
          </div>
        )}
        {lead && (
          <>
            <div className="flex items-center gap-4 rounded-xl border border-border/70 bg-accent/40 p-4">
              <Avatar className="size-14 ring-1 ring-primary/25">
                <AvatarFallback className="bg-card text-lg font-semibold text-primary">
                  {initialsOf(lead.name)}
                </AvatarFallback>
              </Avatar>
              <div className="min-w-0 flex-1">
                <p className="truncate font-semibold">{lead.name || "Unknown"}</p>
                <p className="truncate text-sm text-muted-foreground">{lead.email || "No email on file"}</p>
              </div>
              <StatusBadge status={leadStatusLabel(lead)} />
            </div>

            {/* Have the AI agent call this lead. Distinct from the tel: link
                below, which opens the operator's OWN phone — these are two very
                different actions and were both called "Call" until now. */}
            <div className="mb-2">
              <CallLeadButton
                lead={lead}
                onPlaced={refresh}
                variant="default"
                className="w-full"
              />
              {callBlockedReason(lead) && (
                <p className="mt-1 text-xs text-muted-foreground">{callBlockedReason(lead)}</p>
              )}
            </div>

            {/* Real actions. These used to be three toasts that did nothing. */}
            <div className="flex flex-wrap gap-2">
              <a href={`tel:${dialable(lead.phone)}`} className={cn(buttonVariants({ size: "sm", variant: "outline" }), "flex-1")}>
                <Phone data-icon="inline-start" />
                My phone
              </a>
              <a
                href={`https://wa.me/${dialable(lead.phone).replace(/^\+/, "")}`}
                target="_blank"
                rel="noreferrer"
                className={cn(buttonVariants({ size: "sm", variant: "outline" }), "flex-1")}
              >
                <MessageSquare data-icon="inline-start" />
                WhatsApp
              </a>
              {lead.email ? (
                <a
                  href={`mailto:${lead.email}`}
                  className={cn(buttonVariants({ size: "sm", variant: "outline" }), "flex-1")}
                >
                  <Mail data-icon="inline-start" />
                  Email
                </a>
              ) : (
                <Button size="sm" variant="outline" className="flex-1" onClick={copyPhone}>
                  <Copy data-icon="inline-start" />
                  Copy number
                </Button>
              )}
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="rounded-lg bg-secondary p-3">
                <p className="text-xs text-muted-foreground">Score</p>
                {/* "100 from Loan" - the number alone is ambiguous once solar,
                    real estate and investing agents are live. */}
                <p className="text-lg font-semibold text-primary">
                  {score}/100 <span className="text-sm font-normal text-muted-foreground">from {verticalLabelOf(lead)}</span>
                </p>
                {/* A score nobody can audit is a score nobody trusts. */}
                <p className="mt-1 text-xs text-muted-foreground">{scoreReasonOf(lead)}</p>
              </div>
              <div className="rounded-lg bg-secondary p-3">
                <p className="text-xs text-muted-foreground">Source</p>
                <p className="text-lg font-semibold">{lead.source || "—"}</p>
              </div>
              {/* Solar and investing calls each ask two questions and neither
                  line's questions are a loan type or a budget, so each gets its
                  own two tiles. Keyed off THIS lead's business line, not the
                  header switch: the drawer shows one lead, so it can be exact
                  even when the list behind it is set to "All". */}
              {LINE_COLUMNS[verticalOf(lead)] ? (
                <>
                  <div className="rounded-lg bg-secondary p-3">
                    <p className="text-xs text-muted-foreground">{LINE_COLUMNS[verticalOf(lead)]!.headers[0]}</p>
                    <p className="text-sm font-semibold">{LINE_COLUMNS[verticalOf(lead)]!.values(lead)[0] || "—"}</p>
                  </div>
                  <div className="rounded-lg bg-secondary p-3">
                    <p className="text-xs text-muted-foreground">{LINE_COLUMNS[verticalOf(lead)]!.headers[1]}</p>
                    <p className="text-sm font-semibold">{LINE_COLUMNS[verticalOf(lead)]!.values(lead)[1] || "—"}</p>
                  </div>
                </>
              ) : (
                <>
                  <div className="rounded-lg bg-secondary p-3">
                    <p className="text-xs text-muted-foreground">Amount / budget</p>
                    <p className="text-sm font-semibold">{lead.budget || qualData.loan_amount || "—"}</p>
                  </div>
                  <div className="rounded-lg bg-secondary p-3">
                    <p className="text-xs text-muted-foreground">Loan type</p>
                    <p className="text-sm font-semibold">{loanTypeOf(lead) || "—"}</p>
                  </div>
                </>
              )}
            </div>

            {(qualData.profession || qualData.summary || qualData.customer_intent) && (
              <div className="rounded-lg border p-4 text-sm">
                {qualData.profession && (
                  <p>
                    <span className="text-muted-foreground">Profession: </span>
                    {qualData.profession}
                  </p>
                )}
                {qualData.customer_intent && (
                  <p className="mt-1">
                    <span className="text-muted-foreground">Intent: </span>
                    {qualData.customer_intent}
                  </p>
                )}
                {qualData.summary && (
                  <p className="mt-1">
                    <span className="text-muted-foreground">Agent summary: </span>
                    {qualData.summary}
                  </p>
                )}
              </div>
            )}

            <div className="rounded-lg border p-4">
              <div className="flex items-center gap-2 text-sm">
                <MapPin className="size-4 text-muted-foreground" />
                <span>{lead.city || "No location"}</span>
              </div>
              <div className="mt-2 flex items-center gap-2 text-sm">
                <Clock3 className="size-4 text-muted-foreground" />
                <span>Last attempt: {formatDateTime(lead.last_attempt_at)}</span>
              </div>
              {lead.follow_up_date && (
                <div className="mt-2 flex items-center gap-2 text-sm">
                  <CalendarClock className="size-4 text-muted-foreground" />
                  <span>Follow-up: {formatDate(lead.follow_up_date)}</span>
                </div>
              )}
            </div>

            <div className="flex flex-col gap-3 rounded-lg border p-4">
              <h4 className="text-sm font-medium">Schedule / annotate</h4>
              <div className="grid gap-2">
                <label className="text-xs text-muted-foreground" htmlFor="follow-up-date">
                  Follow-up date
                </label>
                <Input
                  id="follow-up-date"
                  type="date"
                  value={followUp}
                  onChange={(e) => setFollowUp(e.target.value)}
                />
              </div>
              <div className="grid gap-2">
                <label className="text-xs text-muted-foreground" htmlFor="lead-note">
                  Add a note
                </label>
                <textarea
                  id="lead-note"
                  className="min-h-[70px] w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  placeholder="What happened on this lead?"
                />
              </div>
              <Button size="sm" className="self-end" onClick={saveChanges} disabled={saving}>
                {saving ? "Saving…" : "Save"}
              </Button>
            </div>

            {lead.notes && (
              <div>
                <h4 className="mb-2 text-sm font-medium">Notes</h4>
                <p className="whitespace-pre-wrap rounded-lg bg-muted/50 p-3 text-sm text-muted-foreground">
                  {lead.notes}
                </p>
              </div>
            )}

            <div>
              <div className="mb-3 flex items-center justify-between gap-2">
                {/* The business line is named in the heading because the same
                    person can have a second, completely separate history under
                    another line — "Call history" alone was ambiguous. */}
                <h4 className="font-medium">
                  {focusedOnly ? "This call" : "Call history"}
                  <span className={`ml-2 text-xs font-normal ${verticalStyle(lead).color}`}>
                    {verticalLabelOf(lead)}
                  </span>
                </h4>
                {focusCallId && callHistory.length > 1 && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setShowAllCalls((v) => !v)}
                    className="h-7 px-2 text-xs"
                  >
                    {showAllCalls
                      ? "Show only this call"
                      : `Show all ${callHistory.length} calls`}
                  </Button>
                )}
              </div>
              {/* The panel used to read a transcript array that was always empty, so
                  even a fully-called lead showed a blank box. This is the real
                  call_logs history for the lead, with playback and transcript. */}
              {callHistory.length === 0 ? (
                <p className="rounded-lg border bg-muted/10 p-4 text-sm text-muted-foreground">
                  No calls recorded for this lead yet. Recording and transcript appear here once a call completes.
                </p>
              ) : (
                <div className="flex flex-col gap-3">
                  {visibleCalls.map((call: any) => (
                    <div key={call.id} className="flex flex-col gap-2 rounded-lg border bg-muted/10 p-3">
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex items-center gap-2">
                          <CallOutcomeIcon outcome={call.outcome} />
                          <span className="text-sm font-medium">
                            {OUTCOME_LABELS[call.outcome] || call.outcome || "Call"}
                          </span>
                          <span className="text-xs text-muted-foreground">
                            attempt #{call.attempt_no ?? 1} · {formatDuration(call.duration)}
                          </span>
                        </div>
                        <span className="text-xs text-muted-foreground">{formatDateTime(call.called_at)}</span>
                      </div>

                      {call.recording_url && <audio controls preload="none" className="w-full" src={`/api/calls/${call.id}/recording`} />}

                      {call.transcript_url ? (
                        <div className="flex items-center gap-3">
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setOpenTranscriptId(openTranscriptId === call.id ? null : call.id)}
                          >
                            {openTranscriptId === call.id ? "Hide transcript" : "Show transcript"}
                          </Button>
                          {/* The "Open original" link that used to sit here pointed
                              straight at the stored file. That file is served as
                              application/octet-stream, so clicking it downloaded a
                              blob instead of showing anything — the transcript now
                              renders inline, which is all anyone wanted from it. */}
                        </div>
                      ) : (
                        !call.recording_url && (
                          <p className="text-xs text-muted-foreground">
                            No recording or transcript was returned for this call.
                          </p>
                        )
                      )}

                      {openTranscriptId === call.id && <TranscriptView callId={call.id} />}

                      {/* QA verdict for this specific call, so a bad call can be
                          understood without listening to the recording. */}
                      {call.gathered_context?.qa && (
                        <div className="rounded-md border border-amber-500/30 bg-amber-500/5 p-2 text-xs">
                          <div className="flex items-center gap-2">
                            <span className="font-medium">QA</span>
                            {call.gathered_context.qa.score !== null && (
                              <Badge variant="outline">{call.gathered_context.qa.score}/10</Badge>
                            )}
                            {call.gathered_context.qa.sentiment && (
                              <span className="text-muted-foreground">{call.gathered_context.qa.sentiment}</span>
                            )}
                          </div>
                          {call.gathered_context.qa.summary && (
                            <p className="mt-1 text-muted-foreground">{call.gathered_context.qa.summary}</p>
                          )}
                          {(call.gathered_context.qa.tags || []).length > 0 && (
                            <ul className="mt-1 list-inside list-disc text-muted-foreground">
                              {call.gathered_context.qa.tags.map((t: any, i: number) => (
                                <li key={i}>
                                  <span className="font-mono">{t.tag}</span>
                                  {t.reason ? ` — ${t.reason}` : ''}
                                </li>
                              ))}
                            </ul>
                          )}
                        </div>
                      )}

                      {call.gathered_context && Object.keys(call.gathered_context).length > 0 && (
                        <div className="rounded-md bg-muted/40 p-2 text-xs text-muted-foreground">
                          {Object.entries(call.gathered_context)
                            .filter(([key]) => key !== "call_outcome" && key !== "qa")
                            .map(([key, value]) => (
                              <div key={key}>
                                <span className="font-medium">{key.replace(/_/g, " ")}:</span>{" "}
                                {typeof value === "object" ? JSON.stringify(value) : String(value)}
                              </div>
                            ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* The same phone number in the OTHER business lines.
                Each one is a separate lead with its own score, its own
                recordings and its own transcripts — that separation is correct
                and is preserved here. They are listed, never merged, so it is
                obvious at a glance that this person has also been called by
                another agent, and whose call each recording is. */}
            {otherLines.length > 0 && (
              <div>
                <h4 className="mb-1 font-medium">Same person in other business lines</h4>
                <p className="mb-3 text-xs text-muted-foreground">
                  {lead?.phone} is also a lead in {otherLines.length === 1 ? "another business line" : `${otherLines.length} other business lines`}.
                  Each keeps its own score, recordings and transcripts.
                </p>
                <div className="flex flex-col gap-3">
                  {otherLines.map((other: any) => (
                    <div key={other.id} className="rounded-lg border bg-muted/10 p-3">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex items-center gap-2">
                          <Badge
                            variant="outline"
                            className={`${VERTICAL_STYLES[verticalOf(other)].color} ${VERTICAL_STYLES[verticalOf(other)].bg} border-transparent`}
                          >
                            {VERTICAL_LABELS[verticalOf(other)]}
                          </Badge>
                          <span className="text-sm font-medium">Score {other.score ?? 0}/100</span>
                          <span className="text-xs text-muted-foreground">
                            {other.callCount} {other.callCount === 1 ? "call" : "calls"} · {other.status}
                          </span>
                        </div>
                        <span className="text-xs text-muted-foreground">
                          {other.last_attempt_at ? formatDateTime(other.last_attempt_at) : "never called"}
                        </span>
                      </div>

                      {other.calls.length === 0 ? (
                        <p className="mt-2 text-xs text-muted-foreground">
                          No calls under this business line yet.
                        </p>
                      ) : (
                        <div className="mt-3 flex flex-col gap-3">
                          {other.calls.map((call: any) => (
                            <div key={call.id} className="rounded-md border border-border/60 bg-background/40 p-2">
                              <div className="flex items-center justify-between gap-2">
                                <div className="flex items-center gap-2">
                                  <CallOutcomeIcon outcome={call.outcome} />
                                  <span className="text-xs font-medium">
                                    {OUTCOME_LABELS[call.outcome] || call.outcome || "Call"}
                                  </span>
                                  <span className="text-xs text-muted-foreground">
                                    attempt #{call.attempt_no ?? 1} · {formatDuration(call.duration)}
                                  </span>
                                </div>
                                <span className="text-xs text-muted-foreground">{formatDateTime(call.called_at)}</span>
                              </div>

                              {call.recording_url && (
                                <audio controls preload="none" className="mt-2 w-full" src={`/api/calls/${call.id}/recording`} />
                              )}

                              {call.transcript_url ? (
                                <>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    className="mt-1 h-7 px-2 text-xs"
                                    onClick={() => setOpenTranscriptId(openTranscriptId === call.id ? null : call.id)}
                                  >
                                    {openTranscriptId === call.id ? "Hide transcript" : "Show transcript"}
                                  </Button>
                                  {openTranscriptId === call.id && <TranscriptView callId={call.id} />}
                                </>
                              ) : (
                                !call.recording_url && (
                                  <p className="mt-1 text-xs text-muted-foreground">
                                    No recording or transcript was returned for this call.
                                  </p>
                                )
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
        </SheetBody>
      </SheetContent>
    </Sheet>
  )
}

/**
 * Usage & billing as a destination of its own.
 *
 * Previously a tab inside Reports, which put the client's balance behind two
 * clicks and a heading that said "Reports & analytics" - the wrong frame for
 * "how much money is left".
 */
function UsageBillingPage({ onTopUp }: { onTopUp: () => void }) {
  return (
    <>
      <section className="ambient-wash relative -mx-4 -mt-4 overflow-hidden border-b border-border/60 px-4 pb-8 pt-10 md:-mx-8 md:-mt-8 md:px-8 md:pb-10 md:pt-14">
        <div className="relative z-10">
          <p className="motion-fade text-[10px] font-semibold uppercase tracking-[0.2em] text-muted-foreground">
            Usage &amp; billing
          </p>
          <h1 className="motion-rise mt-3 text-balance font-display text-4xl font-semibold leading-[1.05] tracking-tight md:text-5xl lg:text-6xl">
            Credits
            <span className="text-muted-foreground"> &amp; charges</span>
          </h1>
          <p className="motion-rise mt-4 max-w-[52ch] text-pretty text-sm leading-relaxed text-muted-foreground md:text-base">
            What is left, what has been added, and what every call has cost — with a statement you
            can check line by line.
          </p>
        </div>
      </section>
      <UsageBillingPanel onTopUp={onTopUp} />
    </>
  )
}

// ─── MAIN DASHBOARD COMPONENT ──────────────────────────

const navItems = [
  { label: "Overview", icon: LayoutDashboard, countKey: null },
  { label: "Leads", icon: Users, countKey: "total" },
  { label: "Calls", icon: PhoneCall, countKey: null },
  { label: "Campaigns", icon: Megaphone, countKey: null },
  { label: "Follow-ups", icon: CalendarClock, countKey: "follow_ups" },
  { label: "Reports", icon: Activity, countKey: null },
] as const

const systemNavItems = [
  { label: "AI agent", icon: Bot },
  // Its own destination rather than a tab inside Reports. Money is not a
  // sub-view of analytics - the client opens the dashboard specifically to
  // check the balance, and hiding that two clicks deep behind "Reports"
  // made it the hardest number on the site to find.
  { label: "Usage & billing", icon: Wallet },
  // Sits beside Settings rather than under it: this is where deleted leads and
  // call history go, and a client who has just deleted the wrong 4,000 rows
  // needs to find it immediately, not two clicks deep.
  { label: "Recycle bin", icon: Trash2 },
  { label: "Settings", icon: Settings },
] as const

/** Where the chosen business line is remembered between visits. */
const VERTICAL_STORAGE_KEY = "bswealth.vertical"

/**
 * The business-line switch that scopes the whole dashboard.
 *
 * One control rather than a filter on each page: the counts in the sidebar, the
 * rows in the table, the campaigns list and the reports all have to agree about
 * which business line is on screen, and four independent dropdowns would
 * eventually disagree. Selecting "Solar" here means every number below it is a
 * solar number.
 *
 * Horizontally scrollable rather than wrapping, so a phone shows a single row
 * that can be swiped instead of a block that pushes the page down.
 */
function VerticalSwitch({
  value,
  onChange,
}: {
  value: VerticalFilter
  onChange: (next: VerticalFilter) => void
}) {
  const options: { value: VerticalFilter; label: string }[] = [
    { value: "all", label: "All" },
    ...VERTICALS.map((v) => ({ value: v as VerticalFilter, label: VERTICAL_LABELS[v] })),
  ]

  return (
    <div
      role="tablist"
      aria-label="Business line"
      className="flex items-center gap-1 overflow-x-auto rounded-full border border-border/70 bg-muted/40 p-1"
    >
      {options.map((option) => {
        const active = value === option.value
        // "All" has no colour of its own; the four business lines keep the same
        // colours they carry on the lead badges, so the switch and the rows
        // below it are visibly the same language.
        const style = option.value === "all" ? null : VERTICAL_STYLES[option.value as Vertical]
        return (
          <button
            key={option.value}
            role="tab"
            aria-selected={active}
            onClick={() => onChange(option.value)}
            className={cn(
              // min-h-9 on touch: these pills were 24px tall, and they switch
              // which business gets called — the most consequential control on
              // the page and, until now, the smallest thing on it.
              "flex min-h-9 shrink-0 items-center whitespace-nowrap rounded-full px-3.5 text-xs font-medium transition-colors sm:min-h-0 sm:px-3 sm:py-1",
              active
                ? style
                  ? `${style.bg} ${style.color}`
                  : "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}

export function LeadCommandDashboard() {
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [query, setQuery] = useState("")
  const [debouncedQuery, setDebouncedQuery] = useState("")
  const [range, setRange] = useState("7d")
  const [activeNav, setActiveNav] = useState("Overview")
  const [selectedLeadId, setSelectedLeadId] = useState<string | null>(null)
  const [focusCallId, setFocusCallId] = useState<string | null>(null)
  const [campaignOpen, setCampaignOpen] = useState(false)
  const [statusFilter, setStatusFilter] = useState("All")
  // Held at the shell rather than inside LeadsPage: the delete filters are built
  // here, and the range has to reach them or a date-limited delete would quietly
  // ignore the dates the client is looking at.
  const [leadDateRange, setLeadDateRange] = useState<DateRange>(EMPTY_RANGE)
  const [notifOpen, setNotifOpen] = useState(false)
  const [topUpOpen, setTopUpOpen] = useState(false)
  const [readNotifs, setReadNotifs] = useState<string[]>([])
  const [isUploading, setIsUploading] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [leadPage, setLeadPage] = useState(1)

  // The business line the whole dashboard is scoped to. Starts at "all" on the
  // server and on first paint, then restores the remembered choice - reading
  // localStorage during the initial render would not match the server-rendered
  // HTML and would hydrate mismatched.
  const [vertical, setVertical] = useState<VerticalFilter>("all")
  // Deliberately NOT seeded from the header switch, and null until the operator
  // picks one. It used to follow whichever business line was being browsed, which
  // meant a real estate file opened from the Investing tab was imported as
  // Investing - silently, with no error, and the rows then collided with that
  // operator's own earlier mis-filed uploads and were reported as "duplicates".
  // That is what a client experienced as "my leads are not importing" on
  // 2026-08-11. The business line is the one fact about an import that cannot be
  // recovered afterwards, so it is now an explicit choice on every single upload.
  const [uploadVertical, setUploadVertical] = useState<Vertical | null>(null)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [quickCallOpen, setQuickCallOpen] = useState(false)
  const [feedbackOpen, setFeedbackOpen] = useState(false)

  useEffect(() => {
    const saved = window.localStorage.getItem(VERTICAL_STORAGE_KEY)
    if (saved === "all" || (saved && parseVertical(saved))) setVertical(saved as VerticalFilter)
  }, [])

  const changeVertical = useCallback((next: VerticalFilter) => {
    setVertical(next)
    window.localStorage.setItem(VERTICAL_STORAGE_KEY, next)
    // Any page position or filter belongs to the business line that was on
    // screen a moment ago, so reset rather than carry them across.
    setLeadPage(1)
    setStatusFilter("All")
  }, [])

  // Debounced so typing in the search box does not fire a request per keystroke.
  useEffect(() => {
    const id = setTimeout(() => setDebouncedQuery(query), 350)
    return () => clearTimeout(id)
  }, [query])

  useEffect(() => {
    setLeadPage(1)
  }, [debouncedQuery, statusFilter, leadDateRange.from, leadDateRange.to])

  // The date range folds into the SAME LeadQuery the status filter produces, so
  // it travels to the list route and the delete route through one object under
  // the parameter names lib/lead-filter.ts already parses. No second code path.
  const leadFilter = useMemo<LeadQuery>(
    () => ({
      ...(LEAD_FILTERS.find((option) => option.label === statusFilter)?.query ?? {}),
      createdAfter: leadDateRange.from,
      createdBefore: leadDateRange.to,
    }),
    [statusFilter, leadDateRange.from, leadDateRange.to],
  )

  // The same filter the list is fetched with, handed to the bulk delete so the
  // count in the confirmation dialog and the rows that actually go are one set.
  // `undefined` values are stripped by the delete route's body parser.
  const leadDeleteFilters = useMemo(
    () => ({
      ...leadFilter,
      search: debouncedQuery || undefined,
      vertical: vertical === "all" ? undefined : vertical,
    }),
    [leadFilter, debouncedQuery, vertical],
  )

  const {
    leads: dbLeads,
    totalCount,
    totalPages,
    isLoading: leadsLoading,
    refresh: refreshLeads,
  } = useLeads(debouncedQuery, leadFilter, leadPage, vertical)
  const { stats: leadStats, refresh: refreshStats } = useLeadStats(vertical)
  const { campaigns: dbCampaigns, isLoading: campaignsLoading, refresh: refreshCampaigns } = useCampaigns(vertical)
  const { stats: callStats, refresh: refreshCallStats } = useCallStats(vertical)
  const { data: chartData } = useOverview(range === "24h" ? 1 : range === "30d" ? 30 : 7, vertical)
  const { data: health } = useHealth()

  const [newCampaignName, setNewCampaignName] = useState("")
  const [newCampaignLeadCount, setNewCampaignLeadCount] = useState("100")
  const [newCampaignSegment, setNewCampaignSegment] = useState<LeadSegment>("new")
  // A campaign always targets exactly ONE business line - "All" is a valid way
  // to look at leads but not a valid thing to dial, because each business line
  // has its own agent and its own script. Seeded from the header switch, and
  // falls back to Loan when the header is on "All".
  const [campaignVertical, setCampaignVertical] = useState<Vertical>(DEFAULT_VERTICAL)

  // A campaign still follows the header switch: it is chosen and confirmed in a
  // dialog that shows a live lead count for that business line, so a wrong one is
  // visible before anything dials. An import has no such feedback, so it is left
  // alone here and must be picked by hand each time.
  useEffect(() => {
    if (vertical !== "all") setCampaignVertical(vertical)
  }, [vertical])

  // Cleared every time the dialog opens so the previous import's choice can never
  // be inherited by the next one.
  useEffect(() => {
    if (uploadOpen) setUploadVertical(null)
  }, [uploadOpen])

  // Counted for the same business line the launch will target, so the dialog
  // cannot offer leads the campaign would not actually claim.
  const {
    segments: leadSegments,
    followUpUpcoming,
    followUpNextDue,
    refresh: refreshLeadSegments,
  } = useLeadSegmentCounts(campaignOpen, campaignVertical)
  const [isLaunching, setIsLaunching] = useState(false)
  const [showAdvanced, setShowAdvanced] = useState(false)
  // Defaults mirror what the launch route falls back to when these are
  // omitted (app/api/campaigns/route.ts), so opening "Advanced" shows the
  // real values that would apply either way, not different-looking ones.
  const [advanced, setAdvanced] = useState({
    concurrency: "1",
    retryDelaySeconds: "120",
    retryOnBusy: true,
    retryOnNoAnswer: true,
    retryOnVoicemail: true,
  })

  // Keep the lead-count field matched to whichever segment is picked, so it
  // does not stay stuck at "100" when the selected segment only has 1 lead.
  useEffect(() => {
    const count = leadSegments.find((s) => s.value === newCampaignSegment)?.count
    if (count !== undefined) setNewCampaignLeadCount(String(count))
  }, [newCampaignSegment, leadSegments])

  const refreshAll = useCallback(() => {
    refreshLeads()
    refreshStats()
    refreshCampaigns()
    refreshCallStats()
  }, [refreshLeads, refreshStats, refreshCampaigns, refreshCallStats])

  /**
   * Notifications derived from live data. The bell previously rendered a
   * module-level empty array, so it could never show anything.
   */
  const notifications = useMemo(() => {
    const items: { id: string; text: string; time: string; severity: "info" | "warn" }[] = []

    for (const [name, service] of Object.entries((health?.services ?? {}) as Record<string, any>)) {
      if (service.state !== "connected") {
        items.push({
          id: `health-${name}-${service.state}`,
          text: `${name}: ${service.detail}`,
          time: "Integration check",
          severity: "warn",
        })
      }
    }

    for (const campaign of dbCampaigns as any[]) {
      if (campaign.status === "failed") {
        items.push({
          id: `campaign-failed-${campaign.id}`,
          text: `Campaign "${campaign.campaign_name}" failed${campaign.error_message ? `: ${campaign.error_message}` : ""}`,
          time: formatDateTime(campaign.updated_at || campaign.created_at),
          severity: "warn",
        })
      } else if (campaign.status === "completed") {
        items.push({
          id: `campaign-done-${campaign.id}`,
          text: `Campaign "${campaign.campaign_name}" completed (${campaign.actual_count || 0} leads)`,
          time: formatDateTime(campaign.completed_at || campaign.updated_at),
          severity: "info",
        })
      }
    }

    for (const lead of dbLeads as any[]) {
      if (lead.qualification === "qualified") {
        items.push({
          id: `qualified-${lead.id}-${lead.last_attempt_at ?? ""}`,
          text: `${lead.name || lead.phone} qualified with a score of ${lead.score ?? 0}`,
          time: formatDateTime(lead.last_attempt_at || lead.updated_at),
          severity: "info",
        })
      }
    }

    return items.slice(0, 25)
  }, [health, dbCampaigns, dbLeads])

  const unreadCount = notifications.filter((n) => !readNotifs.includes(n.id)).length

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    // Cannot happen from the dialog (the button is disabled until a line is
    // picked), but refusing here too means no code path can ever guess.
    if (!uploadVertical) {
      toast.error("Choose a business line before importing.")
      if (fileInputRef.current) fileInputRef.current.value = ""
      return
    }
    setIsUploading(true)
    const formData = new FormData()
    formData.append("file", file)
    try {
      // The business line rides in the URL, not the form body: on a multipart
      // upload n8n moves the file into binary and leaves the parsed body empty,
      // so a form field could not be relied on to arrive. Never inferred from
      // the filename - a mis-tagged file means the wrong agent calls a real
      // customer, which is far worse than a rejected upload.
      const res = await fetch(`/api/leads/upload?vertical=${uploadVertical}`, {
        method: "POST",
        body: formData,
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.error || `Upload failed (${res.status})`)

      // The business line is named back to the operator on purpose: it is the
      // one thing about an upload that cannot be corrected by looking at the
      // data afterwards, so it is worth confirming out loud.
      const into = `into ${VERTICAL_LABELS[uploadVertical]}`

      // n8n parses and inserts in the background and answers `202 accepted` with
      // only a batch id, so the real counts have to be fetched. Without this the
      // operator was told "File uploaded" whether every row landed or every row
      // was skipped — indistinguishable, and the reason a client believed his
      // imports were silently failing.
      const summary = data.summary
      const batchId = data.batch_id ?? data.batchId
      let reported = false

      if (!summary && batchId) {
        for (let attempt = 0; attempt < 10; attempt += 1) {
          await new Promise((r) => setTimeout(r, attempt === 0 ? 1200 : 1500))
          try {
            const s = await fetch(`/api/leads/upload/status?batchId=${encodeURIComponent(batchId)}`)
            const status = await s.json().catch(() => ({}))
            if (!s.ok || !status.ready) continue
            if (status.nothingImported) {
              // Loud on purpose. This is the exact case that used to pass as success.
              toast.error(`Nothing was imported ${into} — ${status.message}.`, { duration: 10000 })
            } else {
              toast.success(`Imported ${into}: ${status.message}`, { duration: 8000 })
            }
            reported = true
            break
          } catch {
            // keep polling; a transient failure here must not be read as success
          }
        }
      }

      if (!reported) {
        if (summary) {
          toast.success(
            `Uploaded ${into}: ${summary.valid ?? 0} leads added, ${summary.duplicate ?? 0} duplicates, ${summary.rejected ?? 0} rejected`,
          )
        } else {
          // Never claim rows landed when that was not confirmed.
          toast.message(`File sent ${into}. Still processing — check the lead list in a moment.`)
        }
      }
      // The browser used to ALSO post the same file straight to a hard-coded n8n
      // cloud URL, which imported every row a second time and sent lead data to a
      // third party from the client. /api/leads/upload already forwards to n8n.
      refreshAll()
    } catch (err: any) {
      toast.error(err.message || "Upload failed")
    } finally {
      setIsUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ""
    }
  }

  const handleLaunchCampaign = async () => {
    if (!newCampaignName.trim()) {
      toast.error("Please enter a campaign name")
      return
    }
    if (isLaunching) return

    const available = leadSegments.find((s) => s.value === newCampaignSegment)?.count ?? 0
    const requested = parseInt(newCampaignLeadCount, 10)
    if (!Number.isInteger(requested) || requested < 1) {
      toast.error("Enter how many leads to call (at least 1)")
      return
    }

    const concurrency = Number(advanced.concurrency)
    if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 100) {
      toast.error("Concurrency must be a whole number between 1 and 100")
      return
    }
    const retryDelaySeconds = Number(advanced.retryDelaySeconds)
    if (!Number.isInteger(retryDelaySeconds) || retryDelaySeconds < 30 || retryDelaySeconds > 3600) {
      toast.error("Retry delay must be a whole number of seconds between 30 and 3600")
      return
    }

    setIsLaunching(true)
    toast.info("Creating campaign… please wait.")
    try {
      const result: any = await launchCampaign({
        campaign_name: newCampaignName.trim(),
        lead_count: Math.min(requested, available),
        lead_segment: newCampaignSegment,
        vertical: campaignVertical,
        concurrency,
        // max_retries is deliberately NOT sent: the server derives it from the
        // "Max retries" setting so the total per person is capped in one place.
        retry_config: {
          retry_delay_seconds: retryDelaySeconds,
          retry_on_busy: advanced.retryOnBusy,
          retry_on_no_answer: advanced.retryOnNoAnswer,
          retry_on_voicemail: advanced.retryOnVoicemail,
        },
      })
      toast.success(`Campaign launched! ${result?.leads_queued ?? result?.actual_count ?? 0} leads queued.`)
      if (Array.isArray(result?.warnings)) result.warnings.forEach((w: string) => toast.warning(w))
      setCampaignOpen(false)
      setNewCampaignName("")
      refreshAll()
      refreshLeadSegments()
    } catch (err: any) {
      toast.error(err.message || "Failed to launch campaign")
    } finally {
      setIsLaunching(false)
    }
  }

  const handleNavClick = useCallback((label: string) => {
    setActiveNav(label)
    setSidebarOpen(false)
    setStatusFilter("All")
    setQuery("")
    setLeadPage(1)
  }, [])

  const exportReport = useCallback(() => {
    if (chartData.length === 0) {
      toast.info("Nothing to export yet.")
      return
    }
    const rows = [["Day", "Calls", "Qualified"], ...chartData.map((item) => [item.day, item.calls, item.qualified])]
    const blob = new Blob([rows.map((row) => row.join(",")).join("\n")], { type: "text/csv" })
    const url = URL.createObjectURL(blob)
    const link = document.createElement("a")
    link.href = url
    link.download = `call-performance-${range}.csv`
    link.click()
    URL.revokeObjectURL(url)
    toast.success("Chart data exported as CSV")
  }, [chartData, range])

  const handleLogout = useCallback(async () => {
    try {
      await createBrowserClient().auth.signOut()
    } catch {
      /* Fall through to the redirect regardless. */
    }
    window.location.href = "/login"
  }, [])

  /**
   * Opens the lead panel.
   *
   * `callId` is passed when the click came from a specific call row, so the
   * panel opens on that call instead of the lead's whole attempt history.
   */
  const selectLead = useCallback((lead: any, callId?: string | null) => {
    if (!lead?.id) return
    setFocusCallId(callId ?? null)
    setSelectedLeadId(lead.id)
  }, [])

  const renderPage = () => {
    switch (activeNav) {
      case "Overview":
        return (
          <OverviewPage
            range={range}
            setRange={setRange}
            leads={dbLeads}
            onSelectLead={selectLead}
            statusFilter={statusFilter}
            setStatusFilter={setStatusFilter}
            setActiveNav={setActiveNav}
            exportReport={exportReport}
            leadStats={leadStats}
            callStats={callStats}
            chartData={chartData}
            health={health}
            isRefreshing={leadsLoading}
          />
        )
      case "Leads":
        return (
          <LeadsPage
            leads={dbLeads}
            onSelectLead={selectLead}
            statusFilter={statusFilter}
            setStatusFilter={setStatusFilter}
            totalCount={totalCount}
            page={leadPage}
            setPage={setLeadPage}
            totalPages={totalPages}
            leadStats={leadStats}
            isLoading={leadsLoading}
            vertical={vertical}
            onCallPlaced={refreshAll}
            deleteFilters={leadDeleteFilters}
            onDeleted={refreshAll}
            dateRange={leadDateRange}
            setDateRange={setLeadDateRange}
          />
        )
      case "Calls":
        return <CallsPage onSelectLead={selectLead} callStats={callStats} vertical={vertical} />
      case "Campaigns":
        return (
          <CampaignsPage
            setCampaignOpen={setCampaignOpen}
            campaignsData={dbCampaigns as any[]}
            refreshCampaigns={refreshCampaigns}
            isLoading={campaignsLoading}
          />
        )
      case "Follow-ups":
        return <FollowUpsPage onSelectLead={selectLead} vertical={vertical} />
      case "Reports":
        return (
          <ReportsPage
            leadStats={leadStats}
            callStats={callStats}
            onTopUp={() => setTopUpOpen(true)}
            vertical={vertical}
          />
        )
      case "AI agent":
        return <AIAgentPage leadStats={leadStats} callStats={callStats} vertical={vertical} />
      case "Usage & billing":
        return <UsageBillingPage onTopUp={() => setTopUpOpen(true)} />
      case "Recycle bin":
        return <RecycleBinPage />
      case "Settings":
        return <SettingsPage health={health} onLogout={handleLogout} />
      default:
        return null
    }
  }

  const navCount = (key: string | null) => {
    if (!key || !leadStats) return null
    const value = (leadStats as any)[key]
    return typeof value === "number" && value > 0 ? value : null
  }

  return (
    <div className="flex min-h-screen bg-background font-sans text-foreground">
      <IntroSequence />
      {sidebarOpen && (
        <button
          aria-label="Close navigation"
          className="fixed inset-0 z-40 bg-foreground/20 lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Sidebar */}
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-50 flex w-72 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground shadow-float transition-transform lg:static lg:z-auto lg:w-64 lg:translate-x-0 lg:shadow-none",
          sidebarOpen ? "translate-x-0" : "-translate-x-full",
        )}
      >
        {/* px-6 is not arbitrary: the nav below is p-3 and each button px-3, so
            its labels start 24px from the sidebar edge. Matching that puts the
            "BS" on the same vertical line as "Overview" and everything under it.
            Left-aligned rather than centred for the same reason — a centred mark
            over a left-aligned nav reads as a mistake.
            min-h keeps the previous 112px header height while py-6 guarantees
            the breathing room regardless of how the mark is sized later. */}
        <div className="relative flex min-h-28 shrink-0 items-center border-b border-sidebar-border px-6 py-6">
          <BsWealthLockupInline />

          <Button variant="ghost" size="icon" className="absolute right-3 top-3 lg:hidden text-sidebar-foreground hover:bg-sidebar-accent" onClick={() => setSidebarOpen(false)}>
            <X />
            <span className="sr-only">Close menu</span>
          </Button>
        </div>

        <nav aria-label="Main navigation" className="flex flex-1 flex-col gap-0.5 overflow-y-auto p-3">
          {/* pt-5 rather than pt-3: with the border above, a little more air
              here separates the mark from the nav instead of letting the
              section header crowd it. */}
          <p className="px-3 pb-2 pt-5 text-[10px] font-semibold uppercase tracking-[0.16em] text-sidebar-foreground/40">Workspace</p>
          {navItems.map((item) => {
            // These badges were hard-coded to 20 and 6 regardless of the data.
            const count = navCount(item.countKey)
            const active = activeNav === item.label
            return (
              <button
                key={item.label}
                onClick={() => handleNavClick(item.label)}
                className={cn(
                  "group relative flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-all duration-300 [transition-timing-function:cubic-bezier(0.16,1,0.3,1)] active:scale-[0.985]",
                  active
                    ? "bg-sidebar-accent text-sidebar-accent-foreground"
                    : "text-sidebar-foreground/70 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
                )}
              >
                {active && (
                  <span className="absolute inset-y-2 left-0 w-[3px] rounded-full bg-primary motion-scale" aria-hidden />
                )}
                <item.icon className={cn("size-4 shrink-0", active ? "text-primary" : "text-sidebar-foreground/50 group-hover:text-sidebar-foreground/80")} />
                <span className="flex-1 text-left">{item.label}</span>
                {count !== null && (
                  <span className={cn("rounded-md px-1.5 py-0.5 text-xs tabular-nums", active ? "bg-primary/20 text-primary" : "bg-sidebar-foreground/10 text-sidebar-foreground/60")}>
                    {count}
                  </span>
                )}
              </button>
            )
          })}

          <p className="px-3 pb-2 pt-6 text-[10px] font-semibold uppercase tracking-[0.16em] text-sidebar-foreground/40">System</p>
          {systemNavItems.map((item) => {
            const active = activeNav === item.label
            return (
              <button
                key={item.label}
                onClick={() => handleNavClick(item.label)}
                className={cn(
                  "group relative flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-all duration-300 [transition-timing-function:cubic-bezier(0.16,1,0.3,1)] active:scale-[0.985]",
                  active
                    ? "bg-sidebar-accent text-sidebar-accent-foreground"
                    : "text-sidebar-foreground/70 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
                )}
              >
                {active && (
                  <span className="absolute inset-y-2 left-0 w-[3px] rounded-full bg-primary motion-scale" aria-hidden />
                )}
                <item.icon className={cn("size-4 shrink-0", active ? "text-primary" : "text-sidebar-foreground/50 group-hover:text-sidebar-foreground/80")} />
                {item.label}
              </button>
            )
          })}
        </nav>

        <div className="shrink-0 p-3">
          <Card className="border-sidebar-border bg-sidebar-accent/40 text-sidebar-foreground shadow-none">
            <CardContent className="flex items-center gap-3 p-3">
              <span className="relative flex size-2">
                <span
                  className={cn(
                    "absolute inline-flex size-full rounded-full opacity-40",
                    health?.healthy ? "animate-ping bg-primary" : "bg-destructive",
                  )}
                />
                <span
                  className={cn(
                    "relative inline-flex size-2 rounded-full",
                    health?.healthy ? "bg-primary" : "bg-destructive",
                  )}
                />
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium">{health?.healthy ? "All systems go" : "Check integrations"}</p>
                <p className="truncate text-xs text-sidebar-foreground/50">
                  {leadStats?.queued ?? 0} queued · {leadStats?.retry_pending ?? 0} retrying
                </p>
              </div>
              <Headphones className="size-4 text-sidebar-foreground/40" />
            </CardContent>
          </Card>
          <div className="mt-3 flex items-center gap-3 px-2 py-2">
            <Avatar className="size-8 ring-1 ring-primary/30">
              <AvatarFallback className="bg-sidebar-accent text-primary">SR</AvatarFallback>
            </Avatar>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium text-sidebar-foreground">Sales Admin</p>
              <p className="truncate text-xs text-sidebar-foreground/50">BS Wealth Finance</p>
            </div>
            <Button variant="ghost" size="icon" className="text-sidebar-foreground/60 hover:bg-sidebar-accent hover:text-sidebar-foreground" onClick={handleLogout} aria-label="Sign out">
              <LogOut className="size-4" />
            </Button>
          </div>
        </div>
      </aside>

      {/* Main content */}
      <main className="min-w-0 flex-1">
        {/*
          Two layouts, not one squeezed layout.

          Below `lg` this row used to carry nine controls that could not shrink —
          hamburger, search, credits, Import, Call one person, Start campaign,
          Refresh, Feedback, Bell — which made the row 430px wide inside a 380px
          phone. That 50px is why the WHOLE PAGE scrolled sideways: every screen
          in the app inherited a horizontal scrollbar from this one bar, and the
          search input, being the only flexible thing in the row, lost the fight
          and collapsed to 48px.

          The phone layout keeps the three things that must be one tap — menu,
          balance, notifications — puts search on its own full-width row where it
          is actually typeable, and moves the three "do something" actions behind
          a single labelled button. The `lg` layout is unchanged.
        */}
        <header className="sticky top-0 z-30 border-b border-border/70 bg-background/80 backdrop-blur-xl">
          <div className="flex h-16 items-center gap-2 px-3 lg:h-20 lg:gap-3 lg:px-6">
            <Button variant="ghost" size="icon" className="shrink-0 lg:hidden" onClick={() => setSidebarOpen(true)}>
              <Menu />
              <span className="sr-only">Open menu</span>
            </Button>

            {/* Desktop search. On a phone it moves to its own row below. */}
            <div className="relative hidden max-w-md flex-1 lg:block">
              <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <input
                className="h-9 w-full min-w-0 rounded-full border border-input bg-muted/40 px-2.5 py-1 pl-9 text-sm outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:bg-background focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                placeholder="Search leads…"
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value)
                  // Searching only makes sense on a lead list, so jump there.
                  if (e.target.value && activeNav !== "Leads" && activeNav !== "Overview") setActiveNav("Leads")
                }}
              />
            </div>

            <input type="file" ref={fileInputRef} className="hidden" accept=".csv,.xlsx,.xls" onChange={handleFileUpload} />

            {/* ── Phone: balance, one actions menu, bell ─────────────────── */}
            <div className="ml-auto flex min-w-0 items-center gap-1 lg:hidden">
              <CreditsPill onTopUp={() => setTopUpOpen(true)} />
              <DropdownMenu>
                <DropdownMenuTrigger render={<Button size="sm" className="shrink-0 px-2.5" />}>
                  <Plus data-icon="inline-start" />
                  New
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-56">
                  <DropdownMenuItem onClick={() => setCampaignOpen(true)}>
                    <Phone data-icon="inline-start" />
                    Start campaign
                  </DropdownMenuItem>
                  <DropdownMenuItem onClick={() => setQuickCallOpen(true)}>
                    <PhoneForwarded data-icon="inline-start" />
                    Call one person
                  </DropdownMenuItem>
                  <DropdownMenuItem disabled={isUploading} onClick={() => setUploadOpen(true)}>
                    <Upload data-icon="inline-start" />
                    {isUploading ? "Uploading…" : "Import leads"}
                  </DropdownMenuItem>
                  <DropdownMenuItem onClick={refreshAll}>
                    <RefreshCw data-icon="inline-start" />
                    Refresh
                  </DropdownMenuItem>
                  <DropdownMenuItem onClick={() => setFeedbackOpen(true)}>
                    <MessageSquare data-icon="inline-start" />
                    Feedback
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
              <Button variant="ghost" size="icon" className="relative shrink-0" onClick={() => setNotifOpen(true)}>
                <Bell />
                {unreadCount > 0 && (
                  <span className="absolute right-0.5 top-0.5 flex size-4 items-center justify-center rounded-full bg-destructive text-[10px] font-bold text-destructive-foreground">
                    {unreadCount}
                  </span>
                )}
                <span className="sr-only">Notifications</span>
              </Button>
            </div>

            {/* ── Desktop: unchanged ─────────────────────────────────────── */}
            <div className="ml-auto hidden items-center gap-2 lg:flex">
              {/* Balance first in the cluster: it is the one number that changes
                  what the other buttons cost, so it reads before them. */}
              <CreditsPill onTopUp={() => setTopUpOpen(true)} />
              {/* Opens the business-line choice first rather than the file picker:
                  which business a list belongs to cannot be recovered from the file
                  itself, so it has to be stated before the rows are imported. */}
              <Button variant="outline" size="sm" disabled={isUploading} onClick={() => setUploadOpen(true)}>
                <Upload data-icon="inline-start" />
                {isUploading ? "Uploading…" : "Import leads"}
              </Button>
              {/* Sits beside Import on purpose: these are the two ways a lead gets
                  into the system, and calling one person should not require
                  building a file first. */}
              <Button variant="outline" size="sm" onClick={() => setQuickCallOpen(true)}>
                <PhoneForwarded data-icon="inline-start" />
                Call one person
              </Button>
              <Button size="sm" onClick={() => setCampaignOpen(true)}>
                <Phone data-icon="inline-start" />
                Start campaign
              </Button>
              <Button variant="ghost" size="icon" onClick={refreshAll} aria-label="Refresh data">
                <RefreshCw className={cn(leadsLoading && "animate-spin")} />
              </Button>
              {/* Ghost, beside the notification bell rather than in the primary
                  cluster: asking for feedback should be findable at any moment
                  without competing with the actions that actually run the
                  business. */}
              <Button variant="ghost" size="sm" onClick={() => setFeedbackOpen(true)}>
                <MessageSquare data-icon="inline-start" />
                Feedback
              </Button>
              <div className="relative">
                <Button variant="ghost" size="icon" onClick={() => setNotifOpen(true)}>
                  <Bell />
                  {unreadCount > 0 && (
                    <span className="absolute -right-0.5 -top-0.5 flex size-4 items-center justify-center rounded-full bg-destructive text-[10px] font-bold text-destructive-foreground">
                      {unreadCount}
                    </span>
                  )}
                  <span className="sr-only">Notifications</span>
                </Button>
              </div>
            </div>
          </div>

          {/* Phone search, full width. A search box you cannot read what you
              typed into is not a search box. */}
          <div className="px-3 pb-2 lg:hidden">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <input
                className="h-10 w-full min-w-0 rounded-full border border-input bg-muted/40 py-1 pl-9 pr-3 text-base outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:bg-background focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input/30"
                placeholder="Search leads…"
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value)
                  if (e.target.value && activeNav !== "Leads" && activeNav !== "Overview") setActiveNav("Leads")
                }}
              />
            </div>
          </div>
        </header>

        {/* Its own bar under the header rather than squeezed into the button
            cluster: it governs everything on the page below it, so it reads as
            a scope for the content, not as one more action. */}
        {/* top- must track the header's real height, which is now two rows on a
            phone (64px bar + 48px search) and one on desktop. */}
        <div className="sticky top-[112px] z-20 flex items-center gap-3 border-b border-border/70 bg-background/80 px-3 py-2 backdrop-blur-xl md:px-6 lg:top-20">
          <span className="hidden shrink-0 text-xs font-medium text-muted-foreground sm:inline">Business line</span>
          <VerticalSwitch value={vertical} onChange={changeVertical} />
        </div>

        <div
          key={`${activeNav}-${vertical}`}
          className="motion-rise mx-auto flex max-w-screen-2xl flex-col gap-6 p-4 pb-24 md:p-8 lg:pb-10"
        >
          {renderPage()}
        </div>
      </main>

      {/* Mobile bottom tab bar — the off-canvas sidebar drawer stays available via
          the hamburger for the full nav, but the 5 most-used destinations get a
          thumb-reachable bar so the phone experience isn't "open a drawer every tap". */}
      <nav
        aria-label="Primary (mobile)"
        className="fixed inset-x-0 bottom-0 z-40 flex items-stretch justify-around border-t border-sidebar-border bg-sidebar text-sidebar-foreground shadow-float lg:hidden"
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
      >
        {[...navItems.slice(0, 4), { label: "Menu", icon: Menu, countKey: null as null }].map((item) => {
          const isMenu = item.label === "Menu"
          const active = !isMenu && activeNav === item.label
          return (
            <button
              key={item.label}
              onClick={() => (isMenu ? setSidebarOpen(true) : handleNavClick(item.label))}
              className={cn(
                "flex min-w-0 flex-1 flex-col items-center justify-center gap-0.5 py-2 text-[10px] font-medium transition-colors",
                active ? "text-primary" : "text-sidebar-foreground/55 active:text-sidebar-foreground",
              )}
              aria-current={active ? "page" : undefined}
            >
              <item.icon className="size-5" />
              <span className="truncate">{item.label}</span>
            </button>
          )
        })}
      </nav>

      <LeadDetailSheet
        leadId={selectedLeadId}
        focusCallId={focusCallId}
        onClose={() => {
          setSelectedLeadId(null)
          setFocusCallId(null)
        }}
        onSaved={refreshAll}
      />

      {/* Notifications Sheet */}
      <Sheet open={notifOpen} onOpenChange={setNotifOpen}>
        <SheetContent className="w-full sm:max-w-md">
          <SheetHeader>
            <SheetTitle>Notifications</SheetTitle>
            <SheetDescription>{unreadCount} unread</SheetDescription>
          </SheetHeader>
          <SheetBody>
            {unreadCount > 0 && (
              <Button
                variant="ghost"
                size="sm"
                className="mb-3"
                onClick={() => {
                  setReadNotifs(notifications.map((n) => n.id))
                  toast.success("All notifications marked as read")
                }}
              >
                <Check data-icon="inline-start" />
                Mark all as read
              </Button>
            )}
            <div className="flex flex-col gap-2">
              {notifications.length === 0 ? (
                <p className="text-sm text-muted-foreground">Nothing needs your attention right now.</p>
              ) : (
                notifications.map((n) => {
                  const read = readNotifs.includes(n.id)
                  return (
                    <div
                      key={n.id}
                      className={cn(
                        "rounded-lg border p-3 transition-colors",
                        !read && (n.severity === "warn" ? "border-destructive/40 bg-destructive/5" : "border-primary/30 bg-primary/5"),
                      )}
                    >
                      <div className="flex items-start gap-3">
                        {!read && (
                          <span
                            className={cn(
                              "mt-1.5 flex size-2 shrink-0 rounded-full",
                              n.severity === "warn" ? "bg-destructive" : "bg-primary",
                            )}
                          />
                        )}
                        <div className="flex-1">
                          <p className="text-sm">{n.text}</p>
                          <p className="mt-1 text-xs text-muted-foreground">{n.time}</p>
                        </div>
                      </div>
                    </div>
                  )
                })
              )}
            </div>
          </SheetBody>
        </SheetContent>
      </Sheet>

      {/* Add credits. Reachable from the top-bar pill, the Reports billing tab,
          and any "out of credits" toast, so it lives at the shell level. */}
      <TopUpDialog open={topUpOpen} onOpenChange={setTopUpOpen} />

      {/* Call one person — no file, no campaign. Same dialling path as the Call
          button on a lead row, so there is one place a manual call happens. */}
      <QuickCallDialog open={quickCallOpen} onOpenChange={setQuickCallOpen} onPlaced={refreshAll} />

      <FeedbackDialog open={feedbackOpen} onOpenChange={setFeedbackOpen} />

      {/* Import leads — business line first, file second. */}
      <Dialog open={uploadOpen} onOpenChange={setUploadOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Import leads</DialogTitle>
            <DialogDescription>
              Choose which business these leads belong to. They will only ever be called by that
              business&rsquo;s agent.
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-5">
            <div className="grid gap-2">
              <label className="text-sm font-medium" htmlFor="upload-vertical">
                Business line
              </label>
              <Select
                value={uploadVertical ?? undefined}
                onValueChange={(v) => setUploadVertical(v as Vertical)}
              >
                <SelectTrigger id="upload-vertical">
                  {/* No pre-selected value: this used to arrive already filled in
                      with whatever business line was being browsed, which reads as
                      "already decided" and gets clicked past. */}
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
                This cannot be worked out from the file, so it has to be chosen here. Picking the
                wrong one means these people are called by the wrong agent.
              </p>
              {uploadVertical ? (
                <p className="text-xs font-medium text-foreground">
                  These leads will be imported into{" "}
                  <span className={VERTICAL_STYLES[uploadVertical].color}>
                    {VERTICAL_LABELS[uploadVertical]}
                  </span>
                  . The same person can also exist in another business line — that is not a
                  duplicate, and it will import normally.
                </p>
              ) : null}
            </div>
            <div className="rounded-lg border border-border/70 bg-muted/30 p-3 text-xs text-muted-foreground">
              CSV, XLSX or XLS. Any column layout — the importer matches common headings for name,
              phone, email and city, and rejects rows with no usable phone number.
            </div>
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" onClick={() => setUploadOpen(false)}>
              Cancel
            </Button>
            <Button
              // Locked until a business line is chosen — the import cannot fall
              // back to a default, because a wrong default is invisible.
              disabled={isUploading || !uploadVertical}
              onClick={() => {
                setUploadOpen(false)
                fileInputRef.current?.click()
              }}
            >
              {uploadVertical ? `Choose file for ${VERTICAL_LABELS[uploadVertical]} →` : "Choose a business line first"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Start Campaign Dialog */}
      <Dialog open={campaignOpen} onOpenChange={setCampaignOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Start AI Campaign</DialogTitle>
            <DialogDescription>Configure your AI agent to start calling leads.</DialogDescription>
          </DialogHeader>
          <DialogBody className="grid gap-5">
            <div className="grid gap-2">
              <label className="text-sm font-medium" htmlFor="campaign-vertical">
                Business line
              </label>
              {/* First field in the dialog because it decides both which leads
                  are eligible and which agent does the talking. A campaign can
                  only ever target one business line - "All" is a way to view
                  leads, not a thing that can be dialled. */}
              <Select value={campaignVertical} onValueChange={(v) => setCampaignVertical(v as Vertical)}>
                <SelectTrigger id="campaign-vertical">
                  <SelectValue />
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
                Only {VERTICAL_LABELS[campaignVertical]} leads will be called, by the{" "}
                {VERTICAL_LABELS[campaignVertical]} agent.
              </p>
            </div>

            <div className="grid gap-2">
              <label className="text-sm font-medium" htmlFor="campaign-segment">
                Who to call
              </label>
              {/* Grounded in the real leads.status/follow_up_date columns, not a
                  guessed list - see lib/lead-segments.ts. The count next to each
                  option is live and is exactly what the launch will claim. */}
              <select
                id="campaign-segment"
                className="flex h-11 w-full rounded-md border border-input bg-background px-3 py-2 text-sm sm:h-10"
                value={newCampaignSegment}
                onChange={(e) => setNewCampaignSegment(e.target.value as LeadSegment)}
              >
                {leadSegments.length === 0 ? (
                  <option value="new">New</option>
                ) : (
                  leadSegments.map((s) => (
                    <option key={s.value} value={s.value}>
                      {s.label} ({s.count})
                    </option>
                  ))
                )}
              </select>
              <p className="text-xs text-muted-foreground">
                {leadSegments.find((s) => s.value === newCampaignSegment)?.description ?? ""}
              </p>
              {/* Said before the launch, not discovered afterwards on the bill.
                  Most segments stop at the Max retries ceiling; these three do
                  not, because they exist to reach people that ceiling has
                  already excluded. That is the point of them and also the one
                  thing about them that can cost real money unexpectedly. */}
              {(() => {
                const seg = leadSegments.find((s) => s.value === newCampaignSegment)
                if (!seg?.bypassesRetryCap) return null
                const over = seg.overCapCount ?? 0
                return (
                  <p className="rounded-md border border-amber-500/40 bg-amber-500/5 px-2.5 py-2 text-xs text-amber-700 dark:text-amber-500">
                    {over > 0 ? (
                      <>
                        <span className="font-medium">
                          {over.toLocaleString("en-IN")} of these {seg.count.toLocaleString("en-IN")}
                        </span>{" "}
                        have already had their full <span className="font-medium">Max retries</span>. Launching this
                        rings them again anyway — that is what this segment is for, but they are real, billed calls to
                        people the automatic rotation had stopped dialling.
                      </>
                    ) : (
                      <>
                        This segment ignores the <span className="font-medium">Max retries</span> ceiling, so it can
                        dial people the automatic rotation had stopped calling.
                      </>
                    )}
                  </p>
                )
              })()}
              {/* A callback scheduled for next week is not "due" today, so this
                  segment is correctly 0 - but a bare 0 next to a sidebar badge
                  showing 1 looks broken. Say where the difference went. */}
              {newCampaignSegment === "follow_up" &&
                (leadSegments.find((s) => s.value === "follow_up")?.count ?? 0) === 0 &&
                followUpUpcoming > 0 && (
                  <p className="text-xs text-muted-foreground">
                    {followUpUpcoming} follow-up{followUpUpcoming === 1 ? " is" : "s are"} scheduled for later
                    {followUpNextDue ? ` — the next one on ${formatDate(followUpNextDue)}` : ""}. They become
                    callable on the day the customer asked for.
                  </p>
                )}
            </div>

            <div className="rounded-lg border bg-muted/30 p-3">
              <p className="text-sm font-medium">Available in this segment</p>
              <p className="mt-1 text-2xl font-semibold">
                {leadSegments.find((s) => s.value === newCampaignSegment)?.count ?? 0}{" "}
                <span className="text-sm font-normal text-muted-foreground">leads ready to call</span>
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                {leadStats?.total ?? 0} total leads · {leadStats?.queued ?? 0} currently queued in other campaigns
              </p>
            </div>
            {(leadSegments.find((s) => s.value === newCampaignSegment)?.count ?? 0) === 0 && (
              <div className="rounded-lg border border-destructive/50 bg-destructive/5 p-3">
                <p className="text-sm font-medium text-destructive">No leads in this segment</p>
                <p className="mt-1 text-xs text-muted-foreground">
                  {newCampaignSegment === "new"
                    ? "Upload a CSV file first to import leads, then create a campaign."
                    : "Pick a different segment, or check back after more calls complete."}
                </p>
              </div>
            )}
            {health?.services?.dograh && health.services.dograh.state !== "connected" && (
              <div className="rounded-lg border border-destructive/50 bg-destructive/5 p-3">
                <p className="text-sm font-medium text-destructive">Calling provider not ready</p>
                <p className="mt-1 text-xs text-muted-foreground">{health.services.dograh.detail}</p>
              </div>
            )}
            <div className="grid gap-2">
              <label className="text-sm font-medium" htmlFor="campaign-name">
                Campaign name
              </label>
              <Input
                id="campaign-name"
                placeholder="e.g., Weekend loan outreach"
                value={newCampaignName}
                onChange={(e) => setNewCampaignName(e.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <label className="text-sm font-medium" htmlFor="campaign-count">
                Number of leads to call
              </label>
              <Input
                id="campaign-count"
                type="number"
                min={1}
                max={leadSegments.find((s) => s.value === newCampaignSegment)?.count ?? 100}
                value={newCampaignLeadCount}
                onChange={(e) => setNewCampaignLeadCount(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                Max: {leadSegments.find((s) => s.value === newCampaignSegment)?.count ?? 0} leads in this segment
              </p>
            </div>

            <button
              type="button"
              // -my-1.5 py-1.5 buys a 36px tap height without opening a gap in
              // the form: as a bare text row this was 20px tall.
              className="-my-1.5 flex min-h-9 items-center gap-1 py-1.5 text-left text-sm font-medium text-muted-foreground hover:text-foreground"
              onClick={() => setShowAdvanced((v) => !v)}
            >
              <ChevronDown className={cn("size-4 transition-transform", showAdvanced && "rotate-180")} />
              Advanced settings
            </button>
            {showAdvanced && (
              <div className="grid gap-4 rounded-lg border bg-muted/20 p-3 md:grid-cols-2">
                <div className="grid gap-2">
                  <label className="text-sm font-medium" htmlFor="adv-concurrency">
                    Concurrency
                  </label>
                  <Input
                    id="adv-concurrency"
                    type="number"
                    min={1}
                    max={100}
                    value={advanced.concurrency}
                    onChange={(e) => setAdvanced({ ...advanced, concurrency: e.target.value })}
                  />
                  <p className="text-xs text-muted-foreground">How many calls run at once.</p>
                </div>
                {/*
                  There used to be a second "Retries within this campaign" box
                  here. It was independent of the "Max retries" setting on the AI
                  Agent page, and the two multiplied: 2 here meant three calls in
                  this campaign, and the setting then allowed the lead into a
                  later campaign for three more. Six real calls for two settings
                  that both read "2". There is now one number, in one place.
                */}
                <div className="grid gap-2">
                  <span className="text-sm font-medium">Calls per person</span>
                  <p className="text-sm">
                    Capped by <span className="font-medium">Max retries</span> on the AI Agent page.
                  </p>
                  <p className="text-xs text-muted-foreground">
                    Nobody is dialled more than that many times in total, counting every campaign they appear in.
                  </p>
                </div>
                <div className="grid gap-2">
                  <label className="text-sm font-medium" htmlFor="adv-retry-delay">
                    Retry delay (seconds)
                  </label>
                  <Input
                    id="adv-retry-delay"
                    type="number"
                    min={30}
                    max={3600}
                    value={advanced.retryDelaySeconds}
                    onChange={(e) => setAdvanced({ ...advanced, retryDelaySeconds: e.target.value })}
                  />
                </div>
                <div className="grid gap-2">
                  <span className="text-sm font-medium">Retry on</span>
                  <div className="flex flex-col gap-1.5">
                    {[
                      { key: "retryOnBusy" as const, label: "Busy" },
                      { key: "retryOnNoAnswer" as const, label: "No answer" },
                      { key: "retryOnVoicemail" as const, label: "Voicemail" },
                    ].map((opt) => (
                      <label key={opt.key} className="flex items-center gap-2 text-sm">
                        <input
                          type="checkbox"
                          checked={advanced[opt.key]}
                          onChange={(e) => setAdvanced({ ...advanced, [opt.key]: e.target.checked })}
                        />
                        {opt.label}
                      </label>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCampaignOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleLaunchCampaign}
              disabled={
                !newCampaignName.trim() ||
                isLaunching ||
                (leadSegments.find((s) => s.value === newCampaignSegment)?.count ?? 0) === 0
              }
            >
              {isLaunching
                ? "Creating…"
                : `Launch Campaign (${Math.min(
                    parseInt(newCampaignLeadCount, 10) || 0,
                    leadSegments.find((s) => s.value === newCampaignSegment)?.count ?? 0,
                  )} leads)`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
