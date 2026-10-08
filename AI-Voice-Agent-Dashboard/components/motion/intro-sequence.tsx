"use client"

import { useEffect, useState } from "react"
import { AnimatePresence, motion, useReducedMotion } from "motion/react"

const EASE = [0.16, 1, 0.3, 1] as const

/**
 * The one authored moment in the product.
 *
 * Per the motion brief, a dashboard is an "operate and read" surface: routine
 * transitions must stay fast and out of the way, and the user must never be
 * made to wait through page-load choreography. So this earns its place by
 * being strictly bounded — it runs once per browser session, holds for well
 * under a second, and is skipped entirely for anyone who has asked for reduced
 * motion. Returning to the tab later does not replay it.
 *
 * The sequence itself is the wordmark assembling: the two initials arrive on
 * their baselines, the rule draws between them, and the whole lockup lifts away
 * as the dashboard composes underneath. It states the brand once, then gets out.
 */
export function IntroSequence() {
  const reduced = useReducedMotion()
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    // sessionStorage, not localStorage: once per session is the right cadence.
    // Once ever would make the app feel broken on a fresh machine; every load
    // would make it feel slow.
    if (reduced) return
    try {
      if (sessionStorage.getItem("bsw-intro-seen")) return
      sessionStorage.setItem("bsw-intro-seen", "1")
    } catch {
      // Private mode or blocked storage — skip the intro rather than replaying
      // it on every navigation.
      return
    }
    setVisible(true)
    const id = setTimeout(() => setVisible(false), 1750)
    return () => clearTimeout(id)
  }, [reduced])

  return (
    <AnimatePresence>
      {visible && (
        <motion.div
          key="intro"
          className="fixed inset-0 z-[100] flex items-center justify-center bg-background"
          initial={{ opacity: 1 }}
          exit={{ opacity: 0, transition: { duration: 0.45, ease: EASE } }}
          aria-hidden
        >
          <motion.div
            className="flex flex-col"
            exit={{ y: -12, opacity: 0, transition: { duration: 0.4, ease: EASE } }}
          >
            <div className="flex items-center gap-4">
              <motion.span
                className="font-display w-[0.9em] text-6xl font-semibold leading-[0.8] tracking-tight text-primary"
                initial={{ opacity: 0, y: 18 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.6, ease: EASE }}
              >
                B
              </motion.span>
              <motion.span
                className="text-lg font-semibold tracking-[0.3em] uppercase"
                initial={{ opacity: 0, x: -10 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ duration: 0.6, delay: 0.12, ease: EASE }}
              >
                Wealth
              </motion.span>
            </div>

            <motion.span
              className="my-3 h-px origin-left bg-primary/70"
              initial={{ scaleX: 0 }}
              animate={{ scaleX: 1 }}
              transition={{ duration: 0.7, delay: 0.24, ease: EASE }}
            />

            <div className="flex items-center gap-4">
              <motion.span
                className="font-display w-[0.9em] text-6xl font-semibold leading-[0.8] tracking-tight text-primary"
                initial={{ opacity: 0, y: 18 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.6, delay: 0.18, ease: EASE }}
              >
                S
              </motion.span>
              <motion.span
                className="text-lg font-semibold tracking-[0.3em] uppercase"
                initial={{ opacity: 0, x: -10 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ duration: 0.6, delay: 0.3, ease: EASE }}
              >
                Finance
              </motion.span>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
