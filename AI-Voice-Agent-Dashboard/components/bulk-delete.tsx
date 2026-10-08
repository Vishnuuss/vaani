"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import { createPortal } from "react-dom"
import { toast } from "sonner"
import { AlertTriangle, CheckSquare, ListChecks, Trash2, X } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { cn } from "@/lib/utils"

/**
 * Bulk delete, shared by the Leads, Call History and Campaigns pages.
 *
 * The three pages differ only in their endpoint and their noun, so the
 * selection state, the toolbar, the confirmation and the type-the-number guard
 * live here once. Three copies of a destructive dialog is three chances for one
 * of them to lose its confirmation step.
 *
 * Nothing here decides what gets deleted. The server measures the filter and
 * enforces the confirmation; this component's job is to make sure the client
 * cannot be surprised by the number.
 */

export type BulkDeleteEntity = "leads" | "calls" | "campaigns"

const ENDPOINTS: Record<BulkDeleteEntity, string> = {
  leads: "/api/leads/bulk-delete",
  calls: "/api/calls/bulk-delete",
  campaigns: "/api/campaigns/bulk-delete",
}

const NOUNS: Record<BulkDeleteEntity, { one: string; many: string }> = {
  leads: { one: "lead", many: "leads" },
  calls: { one: "call log", many: "call logs" },
  campaigns: { one: "campaign", many: "campaigns" },
}

type Mode = "ids" | "filter" | "all"

/**
 * "1 call log went with it" / "12 call logs went with them".
 *
 * Both halves have to agree with their own count: the noun with how many call
 * logs were swept, and "it"/"them" with how many parents were deleted. Written
 * out once here because the same sentence appears in the delete toast and twice
 * in the Recycle bin, and it read "1 call logs went with them" in all three.
 */
export function cascadedPhrase(cascaded: number, parents: number): string {
  return (
    `${cascaded.toLocaleString("en-IN")} call log${cascaded === 1 ? "" : "s"} ` +
    `went with ${parents === 1 ? "it" : "them"}`
  )
}

interface Preview {
  count: number
  filterLabel: string
  capped: boolean
  maxPerDelete: number
  note?: string
}

// ─── Selection state ─────────────────────────────────────────────────────────

/**
 * Tracks ticked rows across pages, and whether the client is selecting at all.
 *
 * Selections deliberately SURVIVE paging - a client narrowing down 40,000 rows
 * ticks a few on page 1 and a few on page 3 - but are cleared whenever the
 * filter changes, because a tick made under one filter means nothing under the
 * next one and silently carrying it forward would delete a row the client can
 * no longer see.
 *
 * `active` is the selection MODE, and it is off by default. Tick boxes used to
 * be rendered on every row permanently, which put a column of empty checkboxes
 * in front of the client on every visit and, on a phone, ate the left edge of
 * the only two columns that fit. Selecting is now something you enter on
 * purpose: the boxes exist while you are choosing what to delete and at no
 * other time. Leaving the mode drops the ticks with it, so a forgotten
 * selection cannot be acted on later by accident.
 */
export function useRowSelection(resetKey: unknown) {
  const [selected, setSelected] = useState<string[]>([])
  const [active, setActive] = useState(false)

  useEffect(() => {
    // Changing the filter leaves selection mode entirely rather than just
    // emptying it. An empty selection bar hanging over a freshly-filtered table
    // reads as "something is still selected" when nothing is.
    setSelected([])
    setActive(false)
  }, [resetKey])

  const toggle = useCallback((id: string) => {
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
  }, [])

  const setPage = useCallback((pageIds: string[], checked: boolean) => {
    setSelected((prev) =>
      checked ? [...new Set([...prev, ...pageIds])] : prev.filter((id) => !pageIds.includes(id)),
    )
  }, [])

  const clear = useCallback(() => setSelected([]), [])

  const enter = useCallback(() => setActive(true), [])

  const exit = useCallback(() => {
    setActive(false)
    setSelected([])
  }, [])

  return { selected, toggle, setPage, clear, active, enter, exit }
}

/** Enters selection mode. Sits in the filter row beside the other controls. */
export function SelectButton({
  active,
  onEnter,
  onExit,
  className,
}: {
  active: boolean
  onEnter: () => void
  onExit: () => void
  className?: string
}) {
  return (
    <Button
      variant={active ? "default" : "outline"}
      size="sm"
      onClick={active ? onExit : onEnter}
      className={className}
    >
      {active ? (
        <>
          <X data-icon="inline-start" />
          Done
        </>
      ) : (
        <>
          <CheckSquare data-icon="inline-start" />
          Select
        </>
      )}
    </Button>
  )
}

/** The tick box used in both the header and the rows, styled to match. */
export function RowCheckbox({
  checked,
  indeterminate = false,
  onChange,
  label,
}: {
  checked: boolean
  indeterminate?: boolean
  onChange: (checked: boolean) => void
  label: string
}) {
  return (
    <input
      type="checkbox"
      aria-label={label}
      checked={checked}
      ref={(el) => {
        // Native indeterminate is a DOM property, not an attribute, so React
        // cannot set it declaratively. Without it "some rows on this page are
        // ticked" looks identical to "none are".
        if (el) el.indeterminate = indeterminate && !checked
      }}
      onChange={(e) => onChange(e.target.checked)}
      // The row itself opens a detail panel on click; a tick must not do that.
      onClick={(e) => e.stopPropagation()}
      className="size-4 cursor-pointer accent-primary"
    />
  )
}

// ─── The toolbar ─────────────────────────────────────────────────────────────

/**
 * The bar that appears while selecting, and only then.
 *
 * It is FIXED to the bottom of the viewport rather than sitting above the table.
 * On a phone the table is the whole screen and is scrolled through; a delete bar
 * parked at the top of the page is out of sight by the time the client has found
 * the third row they wanted, which is the concrete reason "delete" appeared not
 * to work — the control existed but had scrolled away, and nothing on screen
 * said where it went. Pinned to the bottom it is always in reach of a thumb, and
 * it is the only thing on the page that can start a delete.
 *
 * "Delete all" is deliberately NOT here as a standing button any more. It used
 * to sit on the page permanently in destructive red, one tap from wiping the
 * table, visible to a client who had not asked to delete anything. It is now
 * reached only by entering selection mode and choosing the everything option,
 * behind the same typed-number confirmation as before.
 */
export function BulkDeleteBar({
  entity,
  active,
  selected,
  onClearSelection,
  onExitSelection,
  onSelectAllOnPage,
  pageCount,
  allOnPageSelected,
  filters,
  filterIsActive,
  matchingCount,
  onDeleted,
}: {
  entity: BulkDeleteEntity
  /** Selection mode. When false this renders nothing at all. */
  active: boolean
  selected: string[]
  onClearSelection: () => void
  onExitSelection: () => void
  onSelectAllOnPage: (checked: boolean) => void
  /** How many rows are on the page right now, for the "select page" control. */
  pageCount: number
  allOnPageSelected: boolean
  /** The filters currently on screen, in the API's query-parameter names. */
  filters: Record<string, string | number | boolean | undefined | null>
  /** False when the page is showing everything, which changes the wording. */
  filterIsActive: boolean
  /** Total rows behind the current filter, from the list endpoint. */
  matchingCount: number
  onDeleted: () => void
}) {
  const [pending, setPending] = useState<Mode | null>(null)
  const noun = NOUNS[entity]

  const hasSelection = selected.length > 0

  // The bar is `position: fixed`, so it is out of flow and would sit on top of
  // the last rows of the list. The reserved space has to be added at the BOTTOM
  // of the document, but this component is mounted ABOVE the table — an in-flow
  // spacer here opens a gap between the filters and the first row instead. So
  // the room is taken on <body>, where "the end of the page" actually is, and
  // given back on exit.
  useEffect(() => {
    if (!active) return
    const previous = document.body.style.paddingBottom
    document.body.style.paddingBottom = "5.5rem"
    return () => {
      document.body.style.paddingBottom = previous
    }
  }, [active])

  // Mounted flag so the portal is only built in the browser: document does not
  // exist during the server render.
  const [mounted, setMounted] = useState(false)
  useEffect(() => setMounted(true), [])

  if (!active) {
    // Not selecting: no bar, no tick boxes, no destructive button anywhere on
    // the page. The dialog is mounted with mode=null so nothing is in flight.
    return null
  }

  /*
   * PORTALLED TO <body> ON PURPOSE, and this is not a style choice.
   *
   * The page content sits inside `.motion-rise`, whose entrance animation is
   * declared `animation-fill-mode: both`. That leaves a `transform` on the
   * element permanently, and a transformed ancestor becomes the containing block
   * for any `position: fixed` descendant. Rendered in place, this bar therefore
   * pinned itself to the bottom of the PAGE rather than the bottom of the
   * SCREEN: measured at y=3129 in a viewport 844px tall, i.e. three screens
   * below the fold. That is the concrete reason the client reported that delete
   * "doesn't work" — the button existed, reported as present in the DOM, and was
   * unreachable without scrolling to the very end of the list.
   *
   * A portal to <body> escapes the transformed ancestor entirely.
   */
  const bar = (
    <>
      <div
        className={cn(
          "fixed inset-x-0 z-40 border-t bg-background/95 backdrop-blur-xl transition-colors",
          // Clears the mobile tab bar, which is 4rem tall plus the safe area.
          "bottom-16 lg:bottom-0",
          hasSelection ? "border-destructive/40" : "border-border",
        )}
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
      >
        <div className="mx-auto flex max-w-screen-2xl flex-wrap items-center gap-2 px-3 py-2.5 md:px-8">
          <div className="flex min-w-0 flex-1 items-center gap-2">
            {/* Two wordings, not one truncated one. The long form was being cut
                to "2 leads se…" on a phone, which spends the width without
                delivering the noun OR the verb. The short form drops the noun,
                which the page heading above already states. */}
            <span className={cn("truncate text-sm font-medium", !hasSelection && "text-muted-foreground")}>
              {hasSelection ? (
                <>
                  <span className="sm:hidden">{selected.length.toLocaleString("en-IN")} selected</span>
                  <span className="hidden sm:inline">
                    {selected.length.toLocaleString("en-IN")}{" "}
                    {selected.length === 1 ? noun.one : noun.many} selected
                  </span>
                </>
              ) : (
                <>
                  <span className="sm:hidden">Tap to select</span>
                  <span className="hidden sm:inline">Tap {noun.many} to select</span>
                </>
              )}
            </span>
          </div>

          <div className="flex shrink-0 items-center gap-2">
            {pageCount > 0 && (
              <Button variant="ghost" size="sm" onClick={() => onSelectAllOnPage(!allOnPageSelected)}>
                <ListChecks data-icon="inline-start" />
                <span className="hidden sm:inline">
                  {allOnPageSelected ? "Unselect page" : "Select page"}
                </span>
                <span className="sm:hidden">{allOnPageSelected ? "None" : "Page"}</span>
              </Button>
            )}

            {/* Only offered once it means something different from the ticks. */}
            {hasSelection && matchingCount > selected.length && (
              <Button variant="outline" size="sm" onClick={() => setPending("filter")}>
                <span className="hidden sm:inline">
                  All {matchingCount.toLocaleString("en-IN")} {filterIsActive ? "matching" : ""}
                </span>
                <span className="sm:hidden">All {matchingCount.toLocaleString("en-IN")}</span>
              </Button>
            )}

            {/* Reachable only from inside selection mode. */}
            {!hasSelection && (
              <Button
                variant="ghost"
                size="sm"
                className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                onClick={() => setPending(filterIsActive ? "filter" : "all")}
              >
                {filterIsActive
                  ? `Delete ${matchingCount.toLocaleString("en-IN")} matching`
                  : `Delete all ${noun.many}`}
              </Button>
            )}

            <Button variant="destructive" size="sm" disabled={!hasSelection} onClick={() => setPending("ids")}>
              <Trash2 data-icon="inline-start" />
              Delete
            </Button>

            <Button variant="ghost" size="sm" onClick={onExitSelection} aria-label="Leave selection mode">
              <X data-icon="inline-start" />
              <span className="hidden sm:inline">Done</span>
            </Button>
          </div>
        </div>
      </div>
    </>
  )

  return (
    <>
      {mounted ? createPortal(bar, document.body) : null}

      {/* The dialog is NOT portalled: it is already rendered through the Dialog
          primitive's own portal, so it is unaffected by the transformed ancestor. */}
      <BulkDeleteDialog
        entity={entity}
        mode={pending}
        ids={selected}
        filters={filters}
        onClose={() => setPending(null)}
        onDeleted={() => {
          onClearSelection()
          onExitSelection()
          onDeleted()
        }}
      />
    </>
  )
}

// ─── The confirmation ────────────────────────────────────────────────────────

function BulkDeleteDialog({
  entity,
  mode,
  ids,
  filters,
  onClose,
  onDeleted,
}: {
  entity: BulkDeleteEntity
  mode: Mode | null
  ids: string[]
  filters: Record<string, string | number | boolean | undefined | null>
  onClose: () => void
  onDeleted: () => void
}) {
  const [preview, setPreview] = useState<Preview | null>(null)
  const [loading, setLoading] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [typed, setTyped] = useState("")
  const [error, setError] = useState<string | null>(null)
  const noun = NOUNS[entity]

  const body = useMemo(
    () => ({ mode, ids: mode === "ids" ? ids : [], filters: mode === "all" ? {} : filters }),
    [mode, ids, filters],
  )

  // The count is measured by the SERVER, running the same filter the delete will
  // run. Counting the rows on screen instead would promise "20" and delete
  // 4,318, because the table only ever holds one page.
  useEffect(() => {
    if (!mode) {
      setPreview(null)
      setTyped("")
      setError(null)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    fetch(ENDPOINTS[entity], {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ...body, preview: true }),
    })
      .then(async (res) => {
        const data = await res.json().catch(() => ({}))
        if (cancelled) return
        if (!res.ok) setError(data.error || "Could not work out how many rows match.")
        else setPreview(data as Preview)
      })
      .catch(() => !cancelled && setError("Could not reach the server."))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [mode, entity, body])

  const count = preview?.count ?? 0
  // Mirrors TYPE_TO_CONFIRM_THRESHOLD on the server. The server is the one that
  // enforces it; this only decides whether to show the box.
  const needsTyping = mode === "all" || count >= 500
  const canConfirm = !loading && !deleting && count > 0 && (!needsTyping || typed.trim() === String(count))

  async function confirm() {
    setDeleting(true)
    setError(null)
    try {
      const res = await fetch(ENDPOINTS[entity], {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ...body, confirmCount: count }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        setError(data.error || "The delete failed.")
        return
      }

      const deletedCount = data.deleted ?? 0
      const parts = [
        `${deletedCount.toLocaleString("en-IN")} ${deletedCount === 1 ? noun.one : noun.many} moved to the Recycle Bin`,
      ]
      if (data.cascaded) parts.push(cascadedPhrase(data.cascaded, deletedCount))
      if (data.skipped) parts.push(`${data.skipped.toLocaleString("en-IN")} skipped (${data.skippedReason})`)
      if (data.capped) parts.push(`stopped at ${preview?.maxPerDelete?.toLocaleString("en-IN")} — run it again for the rest`)

      toast.success(parts.join(" · "), { description: "Restore them from Recycle bin within 7 days." })
      onDeleted()
      onClose()
    } catch {
      setError("Could not reach the server.")
    } finally {
      setDeleting(false)
    }
  }

  return (
    <Dialog open={mode !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AlertTriangle className="size-5 text-destructive" />
            {mode === "all" ? `Delete all ${noun.many}?` : `Delete ${noun.many}?`}
          </DialogTitle>
          <DialogDescription>
            Everything deleted here goes to the Recycle bin and can be restored for 7 days.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="flex flex-col gap-4">
          {loading ? (
            <p className="text-sm text-muted-foreground">Counting what matches…</p>
          ) : (
            <>
              <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-4">
                <p className="text-2xl font-semibold">
                  {count.toLocaleString("en-IN")} {count === 1 ? noun.one : noun.many}
                </p>
                <p className="mt-1 text-sm text-muted-foreground">{preview?.filterLabel}</p>
              </div>

              {/* Said plainly, because the database does this silently and the
                  client would otherwise discover it only when a recording they
                  wanted is gone. */}
              {entity === "leads" && (
                <p className="text-sm text-muted-foreground">
                  Each lead&apos;s call history, recordings and transcripts are deleted with it — and restored
                  with it. Leads that are being dialled right now are skipped.
                </p>
              )}
              {entity === "campaigns" && (
                <p className="text-sm text-muted-foreground">
                  Only finished campaigns are deleted. Running, paused and queued campaigns are always kept.
                </p>
              )}
              {entity === "calls" && (
                <p className="text-sm text-muted-foreground">
                  This removes call records only. The leads themselves, and their scores, are untouched.
                </p>
              )}

              {preview?.capped && (
                <p className="text-sm text-amber-600 dark:text-amber-500">
                  More than {preview.maxPerDelete.toLocaleString("en-IN")} rows match. This will delete the first{" "}
                  {preview.maxPerDelete.toLocaleString("en-IN")}; run it again to clear the rest.
                </p>
              )}

              {needsTyping && count > 0 && (
                <div className="flex flex-col gap-2">
                  <label htmlFor="confirm-count" className="text-sm font-medium">
                    Type <span className="font-mono font-semibold">{count}</span> to confirm
                  </label>
                  <Input
                    id="confirm-count"
                    value={typed}
                    onChange={(e) => setTyped(e.target.value)}
                    placeholder={String(count)}
                    inputMode="numeric"
                    autoComplete="off"
                  />
                </div>
              )}

              {count === 0 && <p className="text-sm text-muted-foreground">Nothing matches — there is nothing to delete.</p>}
              {error && <p className="text-sm text-destructive">{error}</p>}
            </>
          )}
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={deleting}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={confirm} disabled={!canConfirm}>
            {deleting ? "Deleting…" : count > 0 ? `Delete ${count.toLocaleString("en-IN")}` : "Delete"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
