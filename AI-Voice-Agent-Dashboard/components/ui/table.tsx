"use client"

import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * The container still scrolls horizontally as a last resort, but it should now
 * almost never need to. See the note on TableCell: cells used to be
 * `whitespace-nowrap` unconditionally, which set the table's minimum width to
 * the sum of its longest cell in every column and guaranteed the scroll.
 */
function Table({ className, ...props }: React.ComponentProps<"table">) {
  return (
    <div
      data-slot="table-container"
      className="relative w-full overflow-x-auto"
    >
      <table
        data-slot="table"
        className={cn("w-full caption-bottom text-sm", className)}
        {...props}
      />
    </div>
  )
}

function TableHeader({ className, ...props }: React.ComponentProps<"thead">) {
  return (
    <thead
      data-slot="table-header"
      className={cn("[&_tr]:border-b", className)}
      {...props}
    />
  )
}

function TableBody({ className, ...props }: React.ComponentProps<"tbody">) {
  return (
    <tbody
      data-slot="table-body"
      className={cn("[&_tr:last-child]:border-0", className)}
      {...props}
    />
  )
}

function TableFooter({ className, ...props }: React.ComponentProps<"tfoot">) {
  return (
    <tfoot
      data-slot="table-footer"
      className={cn(
        "border-t bg-muted/50 font-medium [&>tr]:last:border-b-0",
        className
      )}
      {...props}
    />
  )
}

function TableRow({ className, ...props }: React.ComponentProps<"tr">) {
  return (
    <tr
      data-slot="table-row"
      className={cn(
        "border-b border-border/60 transition-colors duration-200 hover:bg-accent/40 has-aria-expanded:bg-accent/40 data-[state=selected]:bg-accent/60",
        className
      )}
      {...props}
    />
  )
}

function TableHead({ className, ...props }: React.ComponentProps<"th">) {
  return (
    <th
      data-slot="table-head"
      className={cn(
        "h-11 px-3 text-left align-middle text-[11px] font-semibold uppercase tracking-[0.08em] whitespace-nowrap text-muted-foreground [&:has([role=checkbox])]:pr-0",
        className
      )}
      {...props}
    />
  )
}

/**
 * Cells WRAP by default. They used to be `whitespace-nowrap` unconditionally,
 * and that one word was the single biggest layout bug in the app.
 *
 * A no-wrap cell cannot be narrower than its longest unbroken content, so the
 * table's minimum width became the sum of the widest cell in every column, and
 * the container had no choice but to scroll. Measured before this change:
 * Follow-ups rendered a 2,429px table inside a 1,044px laptop column — 1,400px
 * of sideways scrolling — because its Notes column carried whole call logs. That
 * column even had `line-clamp-1` on it, which could never take effect while the
 * cell refused to wrap.
 *
 * Individual cells that genuinely must stay on one line (timestamps, phone
 * numbers, currency, a row of badges) opt back in with `whitespace-nowrap`,
 * which is the exception it should always have been.
 */
function TableCell({ className, ...props }: React.ComponentProps<"td">) {
  return (
    <td
      data-slot="table-cell"
      className={cn(
        "px-3 py-3.5 align-middle [&:has([role=checkbox])]:pr-0",
        className
      )}
      {...props}
    />
  )
}

function TableCaption({
  className,
  ...props
}: React.ComponentProps<"caption">) {
  return (
    <caption
      data-slot="table-caption"
      className={cn("mt-4 text-sm text-muted-foreground", className)}
      {...props}
    />
  )
}

export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableHead,
  TableRow,
  TableCell,
  TableCaption,
}
