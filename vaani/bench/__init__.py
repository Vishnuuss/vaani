"""Phase 0 benchmark harness.

Measures the three vendors on the realtime path before any pipeline code is
written around them. The decisive number is Groq's time-to-first-FINAL-token
(not raw TTFT) -- see Finding 3 in the plan.
"""
