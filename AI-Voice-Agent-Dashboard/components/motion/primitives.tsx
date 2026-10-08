"use client"

import { motion, useReducedMotion, type Variants } from "motion/react"
import { cn } from "@/lib/utils"

/**
 * Shared motion vocabulary.
 *
 * One easing curve and one small set of durations across the whole product.
 * Mixed curves are the clearest tell that several people built a thing; a
 * single deceleration curve is what makes unrelated surfaces feel like one
 * piece of software.
 *
 * Durations follow the brief: 100-150ms for immediate feedback, 150-300ms for
 * routine state change, 300-500ms for overlays and view transitions. Exits run
 * shorter than entrances so dismissing never feels like waiting.
 */
export const EASE = [0.16, 1, 0.3, 1] as const

export const DURATION = {
  feedback: 0.12,
  state: 0.24,
  view: 0.4,
  focal: 0.6,
} as const

/**
 * Page-level transition. Deliberately restrained — this fires on every
 * navigation, so it explains "the view changed" and nothing more. Anything
 * showier here would become latency the user feels on their tenth click.
 */
export function PageTransition({
  children,
  routeKey,
  className,
}: {
  children: React.ReactNode
  routeKey: string
  className?: string
}) {
  const reduced = useReducedMotion()
  return (
    <motion.div
      key={routeKey}
      className={className}
      initial={reduced ? false : { opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: DURATION.view, ease: EASE }}
    >
      {children}
    </motion.div>
  )
}

/**
 * Container that cascades its children in. Appropriate when a group genuinely
 * reads as a list or grid; the total delay is capped so a long list never
 * turns the stagger into a wait.
 */
export function Stagger({
  children,
  className,
  delay = 0,
}: {
  children: React.ReactNode
  className?: string
  delay?: number
}) {
  const reduced = useReducedMotion()
  const variants: Variants = {
    hidden: {},
    show: {
      transition: { staggerChildren: 0.055, delayChildren: delay },
    },
  }
  return (
    <motion.div
      className={className}
      variants={variants}
      initial={reduced ? false : "hidden"}
      animate="show"
    >
      {children}
    </motion.div>
  )
}

export const staggerItem: Variants = {
  hidden: { opacity: 0, y: 12 },
  show: { opacity: 1, y: 0, transition: { duration: 0.45, ease: EASE } },
}

/** A child of <Stagger>. */
export function StaggerItem({
  children,
  className,
}: {
  children: React.ReactNode
  className?: string
}) {
  return (
    <motion.div variants={staggerItem} className={className}>
      {children}
    </motion.div>
  )
}

/**
 * Skeleton placeholder shaped like the content it stands in for, rather than a
 * spinner. A spinner says "something is happening"; a skeleton says "this is
 * what is arriving and where", which is what stops the layout jumping when it
 * lands.
 */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        "relative overflow-hidden rounded-md bg-muted",
        "after:absolute after:inset-0 after:-translate-x-full after:animate-[shimmer_1.6s_infinite]",
        "after:bg-gradient-to-r after:from-transparent after:via-black/[0.045] after:to-transparent",
        className,
      )}
      aria-hidden
    />
  )
}

/** Skeleton matching the stat-card grid, so the swap is a fade not a reflow. */
export function StatSkeletonRow({ count = 4 }: { count?: number }) {
  return (
    <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="shadow-paper rounded-xl border border-border/70 bg-card p-5">
          <Skeleton className="size-10 rounded-lg" />
          <Skeleton className="mt-5 h-8 w-20" />
          <Skeleton className="mt-2 h-3 w-24" />
        </div>
      ))}
    </div>
  )
}

/** Skeleton rows for a table body. */
export function TableSkeleton({ rows = 5, cols = 5 }: { rows?: number; cols?: number }) {
  return (
    <div className="divide-y divide-border/60">
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex items-center gap-4 px-3 py-3.5">
          {Array.from({ length: cols }).map((_, c) => (
            <Skeleton
              key={c}
              className={cn("h-4", c === 0 ? "w-40" : "flex-1")}
              />
          ))}
        </div>
      ))}
    </div>
  )
}
