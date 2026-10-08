"use client"

import { useCallback, useEffect, useState } from "react"
import { toast } from "sonner"
import { Megaphone, PhoneCall, RefreshCw, RotateCcw, Trash2, Users } from "lucide-react"
import { cascadedPhrase } from "@/components/bulk-delete"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { cn } from "@/lib/utils"

/**
 * The Recycle Bin.
 *
 * Lists delete ACTIONS rather than deleted rows: "4,318 unqualified leads,
 * deleted 2 hours ago" restores in one click. Listing the rows themselves would
 * recreate exactly the volume problem the delete feature exists to solve.
 */

interface TrashBatch {
  id: string
  entity: "lead" | "call_log" | "campaign"
  filter_label: string
  row_count: number
  cascaded_count: number
  deleted_by: string | null
  created_at: string
  purge_after: string
  restored_at: string | null
  /** Up to three names from a lead batch, so a row can say WHO. */
  sample?: string[] | null
  /** Rows in this entry that are NOT in the live tables. For a restored entry
   *  this is normally 0; anything higher means the restore only partly worked
   *  and this archive is the last copy of the difference. */
  outstanding?: number
}

/**
 * What a row actually says, in the client's words.
 *
 * The server's `filter_label` describes how the delete was MADE ("1 hand-picked
 * lead"), which tells the reader nothing about what is in it. When the API
 * could name the people, name them.
 */
function describeBatch(batch: TrashBatch): string {
  if (batch.sample?.length) {
    const shown = batch.sample.join(", ")
    const rest = batch.row_count - batch.sample.length
    return rest > 0 ? `${shown} and ${rest.toLocaleString("en-IN")} more` : shown
  }
  return batch.filter_label
}

const ENTITY_META = {
  lead: { icon: Users, label: "Leads" },
  call_log: { icon: PhoneCall, label: "Call logs" },
  campaign: { icon: Megaphone, label: "Campaigns" },
} as const

function formatWhen(value: string) {
  const then = new Date(value).getTime()
  const mins = Math.round((Date.now() - then) / 60_000)
  if (mins < 1) return "just now"
  if (mins < 60) return `${mins} min ago`
  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`
  return new Date(value).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })
}

/** How long this entry has left, so the client knows when it stops being safe. */
function timeLeft(purgeAfter: string) {
  const ms = new Date(purgeAfter).getTime() - Date.now()
  if (ms <= 0) return "wiping shortly"
  const days = Math.floor(ms / 86_400_000)
  if (days >= 1) return `${days} day${days === 1 ? "" : "s"} left`
  const hours = Math.max(1, Math.round(ms / 3_600_000))
  return `${hours} hour${hours === 1 ? "" : "s"} left`
}

export function RecycleBinPage() {
  const [batches, setBatches] = useState<TrashBatch[]>([])
  const [filter, setFilter] = useState("all")
  const [isLoading, setIsLoading] = useState(true)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [retentionDays, setRetentionDays] = useState(7)

  const load = useCallback(async () => {
    setIsLoading(true)
    try {
      const params = new URLSearchParams()
      if (filter !== "all") params.set("entity", filter)
      const res = await fetch(`/api/trash?${params.toString()}`)
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        setError(data.error || "Could not load the Recycle bin.")
        setBatches([])
        return
      }
      setError(null)
      setBatches(data.batches ?? [])
      if (data.retentionDays) setRetentionDays(data.retentionDays)
    } catch {
      setError("Could not reach the server.")
    } finally {
      setIsLoading(false)
    }
  }, [filter])

  useEffect(() => {
    load()
  }, [load])

  async function restore(batch: TrashBatch) {
    setBusyId(batch.id)
    try {
      const res = await fetch(`/api/trash/${batch.id}`, { method: "POST" })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        toast.error(data.error || "Could not restore that entry.")
        return
      }

      // A restore is never guaranteed - a phone number re-uploaded since the
      // delete cannot come back without colliding with the new lead. Reporting
      // the shortfall is the difference between a trustworthy feature and one
      // that quietly loses rows.
      const parts = [`Restored ${(data.restored ?? 0).toLocaleString("en-IN")}`]
      if (data.restoredCalls) parts.push(`${data.restoredCalls.toLocaleString("en-IN")} call logs`)
      if (data.skipped) parts.push(`${data.skipped.toLocaleString("en-IN")} could not come back (${data.skippedReason})`)
      toast.success(parts.join(" · "))
      load()
    } finally {
      setBusyId(null)
    }
  }

  async function purge(batch: TrashBatch) {
    /*
     * Three different actions wearing one label, and getting this wrong destroys
     * data. `restored_at` alone is NOT permission to reassure: a restore is
     * per-row and may partly fail, and the rows that failed exist only in this
     * archive. Saying "already put back and not affected" over those is a lie
     * that ends in permanent loss, so the wording follows `outstanding` - the
     * server's count of what is genuinely not in the live tables.
     */
    const outstanding = batch.outstanding ?? (batch.restored_at ? 0 : batch.row_count)
    const message =
      outstanding === 0
        ? "Remove this entry from the Recycle bin?\n\nEvery record in it is already back in your live data, so nothing is lost."
        : batch.restored_at
          ? `${outstanding.toLocaleString("en-IN")} record(s) in this entry never made it back into your live data. ` +
            `This is the ONLY copy of them.\n\nDelete permanently? This cannot be undone.`
          : `Permanently delete ${batch.row_count.toLocaleString("en-IN")} rows? This cannot be undone.`
    if (!window.confirm(message)) return

    setBusyId(batch.id)
    try {
      // force only after the client has been shown the real number above.
      const res = await fetch(`/api/trash/${batch.id}?force=1`, { method: "DELETE" })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        toast.error(data.error || "Could not empty that entry.")
        return
      }
      toast.success(outstanding === 0 ? "Entry removed from the bin." : "Permanently deleted.")
      load()
    } finally {
      setBusyId(null)
    }
  }

  async function emptyAll() {
    if (!window.confirm("Permanently delete everything in the Recycle bin? This cannot be undone.")) return

    // First attempt is UNFORCED. The server counts what is missing from the live
    // tables and refuses with a 409 rather than trusting this list, which may
    // have been loaded minutes ago - the browser is the wrong place to decide
    // whether something is the last copy of a record.
    let res = await fetch("/api/trash?confirm=empty", { method: "DELETE" })
    let data = await res.json().catch(() => ({}))

    if (res.status === 409 && data.needsForce) {
      if (!window.confirm(`${data.error}\n\nEmptying the bin destroys them permanently. Continue?`)) return
      res = await fetch("/api/trash?confirm=empty&force=1", { method: "DELETE" })
      data = await res.json().catch(() => ({}))
    }

    if (!res.ok) {
      toast.error(data.error || "Could not empty the Recycle bin.")
      return
    }
    toast.success("Recycle bin emptied.")
    load()
  }

  const restorable = batches.filter((b) => !b.restored_at)

  return (
    <>
      <section className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
        <div>
          <h1 className="font-display text-3xl font-semibold tracking-tight md:text-4xl">Recycle bin</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Anything you delete is kept here for {retentionDays} days, then wiped automatically.
          </p>
        </div>
        {/* Four tabs plus two buttons in a non-shrinking row measured 417px on a
            390px phone, which pushed the WHOLE PAGE into a horizontal scroll.
            Two rows that each scroll on their own instead. */}
        <div className="flex flex-col gap-2">
          {/* overflow-visible above md, or the scroll container keeps a
              scrollbar gutter beside the tabs on a desktop that has room. */}
          <div className="-mx-4 overflow-x-auto px-4 pb-1 md:mx-0 md:overflow-visible md:px-0 md:pb-0">
            <Tabs value={filter} onValueChange={setFilter}>
              <TabsList className="w-max">
                <TabsTrigger value="all">All</TabsTrigger>
                <TabsTrigger value="lead">Leads</TabsTrigger>
                <TabsTrigger value="call_log">Calls</TabsTrigger>
                <TabsTrigger value="campaign">Campaigns</TabsTrigger>
              </TabsList>
            </Tabs>
          </div>
          <div className="flex items-center gap-2 md:justify-end">
            <Button variant="outline" size="sm" onClick={load} className="shrink-0">
              <RefreshCw data-icon="inline-start" className={isLoading ? "animate-spin" : undefined} />
              Refresh
            </Button>
            {batches.length > 0 && (
              <Button
                variant="ghost"
                size="sm"
                className="shrink-0 text-destructive hover:bg-destructive/10 hover:text-destructive"
                onClick={emptyAll}
              >
                Empty bin
              </Button>
            )}
          </div>
        </div>
      </section>

      {error && (
        <Card>
          <CardContent className="p-4">
            <p className="text-sm text-destructive">{error}</p>
          </CardContent>
        </Card>
      )}

      {/* Phone layout. Restore and Delete forever are decisions, not icons, so
          they get full-width buttons instead of the last column of a six-column
          table that measured 851px inside a 356px card. */}
      <Card className="min-w-0 md:hidden">
        <CardContent className="p-0">
          {batches.length === 0 ? (
            <p className="p-6 text-center text-sm text-muted-foreground">
              {isLoading ? "Loading…" : "The Recycle bin is empty. Nothing has been deleted recently."}
            </p>
          ) : (
            <div className="flex flex-col">
              {batches.map((batch) => {
                const meta = ENTITY_META[batch.entity]
                const Icon = meta?.icon ?? Users
                return (
                  <div key={batch.id} className="flex flex-col gap-3 border-b border-border/70 p-4 last:border-b-0">
                    <div className="flex items-start gap-3">
                      <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                      <div className="min-w-0 flex-1">
                        <p className="font-medium">{describeBatch(batch)}</p>
                        <p className="text-xs text-muted-foreground">
                          {batch.row_count.toLocaleString("en-IN")} {meta?.label.toLowerCase() ?? "rows"}
                          {batch.cascaded_count > 0
                            ? ` · ${cascadedPhrase(batch.cascaded_count, batch.row_count)}`
                            : ""}
                        </p>
                      </div>
                      {batch.restored_at ? (
                        <Badge
                          variant="outline"
                          className={cn(
                            "shrink-0 border-transparent",
                            (batch.outstanding ?? 0) > 0
                              ? "bg-amber-500/10 text-amber-600"
                              : "bg-emerald-500/10 text-emerald-600",
                          )}
                        >
                          {(batch.outstanding ?? 0) > 0 ? "Partly put back" : "Put back"}
                        </Badge>
                      ) : null}
                    </div>

                    {/* Said outright. A restored row that still sits in a list
                        headed "Recycle bin" reads as deleted, and the client has
                        no way to tell that the data is already back. */}
                    {batch.restored_at &&
                      ((batch.outstanding ?? 0) > 0 ? (
                        <p className="text-xs font-medium text-amber-600">
                          {batch.outstanding!.toLocaleString("en-IN")} of these never made it back into your{" "}
                          {meta?.label.toLowerCase() ?? "records"}. This is the only copy — do not delete it.
                        </p>
                      ) : (
                        <p className="text-xs text-muted-foreground">
                          Already back in your {meta?.label.toLowerCase() ?? "records"} — nothing here is deleted.
                          This is just the leftover copy.
                        </p>
                      ))}

                    <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                      <span className="font-mono">{batch.row_count.toLocaleString("en-IN")} rows</span>
                      <span>{formatWhen(batch.created_at)}</span>
                      {batch.deleted_by && <span className="truncate">{batch.deleted_by}</span>}
                      {!batch.restored_at && <span>{timeLeft(batch.purge_after)}</span>}
                    </div>

                    <div className={cn("grid gap-2", batch.restored_at ? "grid-cols-1" : "grid-cols-2")}>
                      {!batch.restored_at && (
                        <Button variant="outline" size="sm" disabled={busyId === batch.id} onClick={() => restore(batch)}>
                          <RotateCcw data-icon="inline-start" />
                          Restore
                        </Button>
                      )}
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                        disabled={busyId === batch.id}
                        onClick={() => purge(batch)}
                      >
                        <Trash2 data-icon="inline-start" />
                        {batch.restored_at && (batch.outstanding ?? 0) === 0 ? "Remove this entry" : "Delete forever"}
                      </Button>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="hidden min-w-0 md:block">
        <CardContent className="overflow-x-auto p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>What was deleted</TableHead>
                <TableHead>Rows</TableHead>
                <TableHead>Deleted</TableHead>
                <TableHead>By</TableHead>
                {/* Was "Kept until", which this column contradicted the moment a
                    row was restored — it then showed a status badge, not a date. */}
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {batches.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} className="py-8 text-center text-muted-foreground">
                    {isLoading ? "Loading…" : "The Recycle bin is empty. Nothing has been deleted recently."}
                  </TableCell>
                </TableRow>
              ) : (
                batches.map((batch) => {
                  const meta = ENTITY_META[batch.entity]
                  const Icon = meta?.icon ?? Users
                  return (
                    <TableRow key={batch.id}>
                      <TableCell>
                        <div className="flex items-center gap-3">
                          <Icon className="size-4 shrink-0 text-muted-foreground" />
                          <div className="min-w-0">
                            <p className="font-medium">{describeBatch(batch)}</p>
                            <p className="text-xs text-muted-foreground">
                              {batch.sample?.length ? `${batch.filter_label}` : meta?.label}
                              {batch.cascaded_count > 0
                                ? ` · ${cascadedPhrase(batch.cascaded_count, batch.row_count)}`
                                : ""}
                            </p>
                          </div>
                        </div>
                      </TableCell>
                      <TableCell className="font-mono text-sm">{batch.row_count.toLocaleString("en-IN")}</TableCell>
                      <TableCell className="text-sm text-muted-foreground">{formatWhen(batch.created_at)}</TableCell>
                      <TableCell className="text-sm text-muted-foreground">{batch.deleted_by || "—"}</TableCell>
                      <TableCell>
                        {batch.restored_at ? (
                          <div className="flex flex-col gap-0.5">
                            {/* "Put back" is only the whole truth when NOTHING is
                                outstanding. A restore is per-row and can partly
                                fail; the rows that failed are still only in this
                                archive, and calling that "already in your list"
                                is what makes deleting it feel safe when it is
                                the opposite. */}
                            <Badge
                              variant="outline"
                              className={cn(
                                "w-fit border-transparent",
                                (batch.outstanding ?? 0) > 0
                                  ? "bg-amber-500/10 text-amber-600"
                                  : "bg-emerald-500/10 text-emerald-600",
                              )}
                            >
                              {(batch.outstanding ?? 0) > 0 ? "Partly put back" : "Put back"}
                            </Badge>
                            <span className="text-xs text-muted-foreground">
                              {(batch.outstanding ?? 0) > 0
                                ? `${batch.outstanding!.toLocaleString("en-IN")} never came back — only copy`
                                : "Already in your list"}
                            </span>
                          </div>
                        ) : (
                          <span className="text-sm text-muted-foreground">{timeLeft(batch.purge_after)}</span>
                        )}
                      </TableCell>
                      <TableCell className="text-right">
                        <div className="flex items-center justify-end gap-2">
                          {!batch.restored_at && (
                            <Button
                              variant="outline"
                              size="sm"
                              disabled={busyId === batch.id}
                              onClick={() => restore(batch)}
                            >
                              <RotateCcw data-icon="inline-start" />
                              Restore
                            </Button>
                          )}
                          <Button
                            variant="ghost"
                            size="sm"
                            className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                            disabled={busyId === batch.id}
                            onClick={() => purge(batch)}
                          >
                            <Trash2 data-icon="inline-start" />
                            {batch.restored_at && (batch.outstanding ?? 0) === 0 ? "Remove this entry" : "Delete forever"}
                          </Button>
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

      {restorable.length > 0 && (
        <p className="text-sm text-muted-foreground">
          {restorable.length} entr{restorable.length === 1 ? "y" : "ies"} can still be restored.
        </p>
      )}
    </>
  )
}
