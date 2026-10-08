"use client"

import { useState } from "react"
import { toast } from "sonner"
import { Star } from "lucide-react"

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
import {
  FEEDBACK_VERTICALS,
  HOURS_SAVED_OPTIONS,
  QUALIFICATION_CHANGE_OPTIONS,
  RATING_QUESTIONS,
} from "@/lib/feedback"
import { cn } from "@/lib/utils"

/**
 * The client feedback form.
 *
 * Only the first question is required. A form that refuses to submit until every
 * field is filled is a form that mostly does not get submitted, and a partial
 * answer about the agent sounding robotic is worth more than a perfect blank.
 *
 * The options come from lib/feedback.ts, which the API validates against, so
 * nothing offered here can be silently rejected on save.
 */

interface FormState {
  vertical: string
  dashboard_rating: number | null
  voice_rating: number | null
  understanding_rating: number | null
  qualification_change: string
  qualified_before_week: string
  qualified_after_week: string
  hours_saved: string
  improvements: string
  recommend_score: number | null
}

const EMPTY: FormState = {
  vertical: "",
  dashboard_rating: null,
  voice_rating: null,
  understanding_rating: null,
  qualification_change: "",
  qualified_before_week: "",
  qualified_after_week: "",
  hours_saved: "",
  improvements: "",
  recommend_score: null,
}

function Stars({ value, onChange, label }: { value: number | null; onChange: (v: number) => void; label: string }) {
  return (
    <div className="flex items-center gap-1" role="group" aria-label={label}>
      {[1, 2, 3, 4, 5].map((n) => (
        <button
          key={n}
          type="button"
          aria-label={`${n} out of 5`}
          aria-pressed={value === n}
          onClick={() => onChange(n)}
          className="rounded p-0.5 transition-transform hover:scale-110"
        >
          <Star
            className={cn(
              "size-6",
              value !== null && n <= value ? "fill-amber-400 text-amber-400" : "text-muted-foreground/40",
            )}
          />
        </button>
      ))}
    </div>
  )
}

function ChoiceRow({
  options,
  value,
  onChange,
}: {
  options: readonly { value: string; label: string }[]
  value: string
  onChange: (v: string) => void
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          onClick={() => onChange(value === opt.value ? "" : opt.value)}
          className={cn(
            "rounded-full border px-3 py-1.5 text-sm transition-colors",
            value === opt.value
              ? "border-primary bg-primary text-primary-foreground"
              : "border-input hover:bg-muted",
          )}
        >
          {opt.label}
        </button>
      ))}
    </div>
  )
}

function Question({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm font-medium">{label}</p>
      {hint && <p className="-mt-1 text-xs text-muted-foreground">{hint}</p>}
      {children}
    </div>
  )
}

export function FeedbackDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const [form, setForm] = useState<FormState>(EMPTY)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }))
  }

  function close(next: boolean) {
    onOpenChange(next)
    // Cleared on close so reopening never shows the last submission's answers
    // back at the client as if they were saved drafts.
    if (!next) {
      setForm(EMPTY)
      setError(null)
    }
  }

  async function submit() {
    if (!form.vertical) {
      setError("Please choose which agent you are rating.")
      return
    }
    setSaving(true)
    setError(null)
    try {
      const res = await fetch("/api/feedback", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          ...form,
          // Empty strings mean "not answered", not zero.
          qualified_before_week: form.qualified_before_week === "" ? null : form.qualified_before_week,
          qualified_after_week: form.qualified_after_week === "" ? null : form.qualified_after_week,
          qualification_change: form.qualification_change || null,
          hours_saved: form.hours_saved || null,
          improvements: form.improvements.trim() || null,
        }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        setError(data.error || "Could not save your feedback.")
        return
      }
      toast.success("Thank you — your feedback has been sent.")
      close(false)
    } catch {
      setError("Could not reach the server.")
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>How is it going?</DialogTitle>
          <DialogDescription>
            Tell us how the dashboard and the AI calling agents are working for you. Answer as much or as little as
            you like.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="flex flex-col gap-6">
          <Question label="Which agent are you rating?">
            <ChoiceRow
              options={FEEDBACK_VERTICALS}
              value={form.vertical}
              onChange={(v) => set("vertical", v)}
            />
          </Question>

          {RATING_QUESTIONS.map((q) => (
            <Question key={q.key} label={q.label}>
              <Stars
                label={q.label}
                value={form[q.key]}
                onChange={(v) => set(q.key, v)}
              />
            </Question>
          ))}

          <Question
            label="Has your lead qualification rate improved since using the agent?"
            hint="The main thing we want to know."
          >
            <ChoiceRow
              options={QUALIFICATION_CHANGE_OPTIONS}
              value={form.qualification_change}
              onChange={(v) => set("qualification_change", v)}
            />
          </Question>

          <Question label="Roughly how many qualified leads per week?" hint="Before the agent, and now.">
            <div className="flex flex-wrap items-center gap-3">
              <div className="flex flex-col gap-1">
                <label htmlFor="fb-before" className="text-xs text-muted-foreground">
                  Before
                </label>
                <Input
                  id="fb-before"
                  className="w-28"
                  inputMode="numeric"
                  value={form.qualified_before_week}
                  onChange={(e) => set("qualified_before_week", e.target.value.replace(/\D/g, ""))}
                />
              </div>
              <div className="flex flex-col gap-1">
                <label htmlFor="fb-after" className="text-xs text-muted-foreground">
                  Now
                </label>
                <Input
                  id="fb-after"
                  className="w-28"
                  inputMode="numeric"
                  value={form.qualified_after_week}
                  onChange={(e) => set("qualified_after_week", e.target.value.replace(/\D/g, ""))}
                />
              </div>
            </div>
          </Question>

          <Question label="How much time does this save your team each week?">
            <ChoiceRow
              options={HOURS_SAVED_OPTIONS}
              value={form.hours_saved}
              onChange={(v) => set("hours_saved", v)}
            />
          </Question>

          <Question label="What should the agent do better?" hint="Anything it gets wrong, or should say differently.">
            <textarea
              id="fb-improvements"
              className="flex min-h-[90px] w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              value={form.improvements}
              onChange={(e) => set("improvements", e.target.value)}
              maxLength={4000}
            />
          </Question>

          <Question label="How likely are you to recommend this to another business?" hint="0 = not at all, 10 = definitely.">
            <div className="flex flex-wrap gap-1.5">
              {Array.from({ length: 11 }, (_, n) => (
                <button
                  key={n}
                  type="button"
                  onClick={() => set("recommend_score", form.recommend_score === n ? null : n)}
                  className={cn(
                    "size-9 rounded-md border text-sm transition-colors",
                    form.recommend_score === n
                      ? "border-primary bg-primary text-primary-foreground"
                      : "border-input hover:bg-muted",
                  )}
                >
                  {n}
                </button>
              ))}
            </div>
          </Question>

          {error && <p className="text-sm text-destructive">{error}</p>}
        </DialogBody>

        <DialogFooter>
          <Button variant="outline" onClick={() => close(false)} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={saving}>
            {saving ? "Sending…" : "Send feedback"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
