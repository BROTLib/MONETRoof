# Plans

Dated `YYYY-MM-DD-<slug>.md` investigation and work logs, one per unit of work.

- [2026-08-20-position-counter-fix-plan.md](2026-08-20-position-counter-fix-plan.md) —
  fix the roof position counter (hall/inductive pulse rattling and over-counting).
  **implemented, merged, tagged `v1.1.0`/`v1.1.1`** — confirmed present in `origin/main` (corrected 2026-09-16); the
  2026-09-15 "contradicted" re-check was accurate only for one stale local clone (likely MONET/N's `Becky`, not
  pulled since ~Jan 2026) — worth checking whether MONET/N's roof controller is still running pre-fix code
- [2026-09-20-code-review.md](2026-09-20-code-review.md): full code review of MONETRoof
  (bugs, security, design, CI). **draft**, review finished; #4 and #5 fixed on `develop`. Two Python checks in `testing/`, nothing
  run on a PLC or the roof.
