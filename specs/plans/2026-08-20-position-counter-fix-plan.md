# Plan: Fix the roof position counter (pulse rattling / over-counting)

Status: draft — measurement first, then implement the filter.

## 1. Problem

`FB_RoofMotor` counts hall-sensor pulses with a simple edge counter:

```st
counter_trig(CLK := counter);
IF counter_trig.Q THEN
	IF direction_open THEN position := position + 1;
	ELSE                     position := position - 1;
	END_IF
END_IF
```

When the roof starts moving, the drive **rattles**: the hall signal bounces
high/low across several PLC cycles, and each real rising edge is counted.
With `max_position_diff := 2` (synchronisation tolerance), a few spurious
pulses on one motor of a roof half trip `sync_error` (or `limit_error`) and
stop the roof.

Observed symptom: roof rattles briefly at start, position counter jumps, roof
stops with a sync/limit error.

## 2. Why "just debounce" is not enough

A simple TON level-debounce (input must be stable high for X ms) removes short
glitches, but the **first pulse of a startup burst is indistinguishable from a
legitimate pulse** — it still gets counted. The property that separates rattle
from real motion is **rate**: legitimate pulses cannot arrive faster than the
pulse rate at `max_speed`, while rattle pulses are much faster.

Therefore the fix is a small filter chain (see §5), and — before any of that —
we need real numbers about the pulse shape (§3 and §4).

## 3. First step: measure pulse width and cadence in the live system

### Why the 10 ms task can't measure anything

The PLC task samples at 10 ms. A short pulse is only *seen* if it overlaps a
task cycle, so what we observe is **aliased** — the counts we see say almost
nothing about the true pulse width or cadence. Measurement must sample much
faster than the pulse.

### Caveat before investing: hardware ground truth

The EL1008 digital input terminal has a **fixed ~3 ms input filter** (per its
type spec: "8Ch. Dig. Input 24V, 3ms"). Anything the PLC can count is therefore
≥ ~3 ms wide at the terminal; sub-ms chatter never reaches the PLC at all.

- If possible, probe the sensor output directly (oscilloscope / logic
  analyzer) to see the **unfiltered** truth — this settles immediately whether
  the rattle is:
  - mechanical oscillation of the magnet past the sensor,
  - a weak magnet / marginal trigger threshold, or
  - EMI on the wiring.
- Whatever we measure through the terminal is already filtered by the 3 ms
  input filter — keep that in mind when sizing the software filter.

### Measurement task (recommended): fast task + timestamps

Add a dedicated task (e.g. **250 µs, priority 10** — above PlcTask priority 20)
that only samples the counter input and timestamps edges with
`F_GetSystemTime` (100 ns resolution, Tc2_System). Sampling at 250 µs resolves
pulses down to ~0.5 ms with ~30 samples on a 3 ms pulse; a 250 µs task is a
negligible load on a CX.

```st
// PRG_MeasureCounter — runs in a 250 µs task
VAR
    counter       AT %I* : BOOL;        // hall input of the motor in question
    rTrig  : R_TRIG;  fTrig : F_TRIG;
    tNow   : LARGE_INTEGER;
    tRise  : LARGE_INTEGER;  tLastRise : LARGE_INTEGER;
    nPulses : UDINT;
    aWidth  : ARRAY[1..200] OF LREAL;   // ms — ring buffer
    aPeriod : ARRAY[1..200] OF LREAL;   // ms — ring buffer
    // outputs: nPulses, min/max/mean width, min/max/mean period
END_VAR

F_GetSystemTime(pSystemTime := ADR(tNow));          // 100 ns ticks
rTrig(CLK := counter);
IF rTrig.Q THEN
    tLastRise := tRise;
    tRise     := tNow;
    nPulses   := nPulses + 1;
    IF nPulses > 1 THEN
        aPeriod[(nPulses - 1) MOD 200 + 1] := TO_LREAL(tNow - tLastRise) / 10000.0;  // ms
    END_IF
END_IF
fTrig(CLK := counter);
IF fTrig.Q THEN
    aWidth[nPulses MOD 200 + 1] := TO_LREAL(tNow - tRise) / 10000.0;  // ms
END_IF
```

Notes:

- Compute min/max/mean of the ring buffers in the 10 ms task (or a 1 s loop)
  and expose them, e.g. publish over the existing MQTT telemetry
  (`MONET.ROOF.*` fields) so they can be watched remotely.
- Record `real_speed` / direction alongside the pulses, so we can see:
  - whether the rattle bursts happen at standstill or at low speed,
  - whether rising and falling counts cancel out, or accumulate.
- One motor first (e.g. `roofs[1].motors[1]`); extend to all four after the
  first results.

### Alternative: TwinCAT ScopeView (zero code, but same limitation)

ScopeView (ships with XAE) can record and measure width/period with cursors —
**but** the input image is only refreshed at the cycle of the task that reads
it (currently 10 ms). ScopeView only becomes useful together with the fast
measurement task above: record the fast task's boolean sample instead of the
raw I/O symbol.

### What the numbers will tell us

The measured burst width and cadence directly size the filter:

| Quantity | Sizes |
|---|---|
| max noise pulse width | debounce time (must be ≥ max measured width) |
| max burst cadence (gap between noise pulses) | min spacing between accepted counts |
| shortest legitimate pulse period at `max_speed` | upper bound for both (spacing must stay below it) |

With only `max_position = 200` counts over the full travel, the legitimate
pulse rate is very low — huge headroom for the filter.

## 4. Decision point (after measurement)

Depending on the measurement results:

1. **Pulses are wide / bursts are long** (≥ several ms, spaced close) →
   proceed with the software filter chain (§5).
2. **Pulses are sub-ms and the EL1008 filter already hides them** → the
   over-counting is *not* sensor chatter; look at the motion profile instead:
   - raise `min_speed` so the roof starts cleanly and leaves the resonant
     speed range quickly,
   - tune `acceleration`,
   - check the magnet/sensor mounting.
3. **Mechanical oscillation of the magnet past the sensor** → no counter
   filter fully fixes it; fix the mechanics, use the filter only as insurance.

## 5. Fix design: filter chain in `FB_RoofMotor`

Three layers, all cheap:

1. **Level debounce** — count only if the input stays high for a minimum
   time (kills short glitches).
2. **Rate limit** — ignore an edge that arrives within a minimum spacing
   after the last *accepted* count (kills the rest of the burst).
3. **Only count while moving** — gate on `real_speed <> 0` so standstill
   vibration never counts at all.

Sketch (replaces the counter block in `FB_RoofMotor`):

```st
// -- declaration additions --
counter_debounce    : TIME := T#20MS;   // min. on-time of the hall pulse
counter_min_spacing : TIME := T#50MS;   // min. time between accepted counts
tonDebounce : TON;
tonSpacing  : TON;
bCounting   : BOOL;

// -- implementation --
tonDebounce(IN := counter, PT := counter_debounce);   // stable-high filter
counter_trig(CLK := tonDebounce.Q);                   // edge of the *filtered* signal

IF counter_trig.Q THEN
    // rate limit: allow a new count only after the spacing timer expired
    IF NOT tonSpacing.Q THEN
        tonSpacing(IN := TRUE, PT := counter_min_spacing);
        IF real_speed > 0 THEN
            position := position + 1;
        ELSIF real_speed < 0 THEN
            position := position - 1;
        END_IF
    ELSE
        tonSpacing(IN := FALSE);   // re-arm for the next burst
    END_IF
END_IF
```

Make `counter_debounce` and `counter_min_spacing` **`VAR_INPUT`** (with the
defaults above) so they can be tuned per installation without recompiling.

### Safety net: re-sync at the limit switches

`FB_RoofMotor` already contains a commented-out block that snaps `position` to
`min_position` / `max_position` when the closed/open limit switch is hit while
moving in that direction:

```st
closed_trigger(CLK := closed);
IF direction_close AND closed_trigger.Q THEN position := min_position; END_IF
opened_trigger(CLK := opened);
IF direction_open AND opened_trigger.Q THEN position := max_position; END_IF
```

Re-enable it (currently commented out in the POU). The counter then becomes
**self-correcting**: even if a few spurious counts slip through, the next full
close/open re-zeros the position. This is the strongest protection against
counter drift.

### Rules of thumb for tuning

- `counter_debounce` ≥ max measured noise pulse width.
- `counter_min_spacing` ≥ max measured burst cadence, **and** below the
  shortest legitimate pulse period at `max_speed`.
  - If a full open at `max_speed` takes `T` seconds and the travel is 200
    counts, the spacing must be `< T/200`. For any realistic roof travel
    time this is far above 50 ms.
- Start conservative (debounce 20 ms, spacing 50 ms), then tighten based on
  the measured numbers.

## 6. Implementation steps

- [ ] **Step 1 — Measurement**: add `PRG_MeasureCounter` + fast task
      (250 µs, priority 10) in the tsproj; log/observe pulse width & cadence
      for one motor; record burst behaviour at start, at low speed, at
      standstill.
- [ ] **Step 2 — Decide**: classify the rattle per §4 (electrical chatter vs
      mechanics vs motion profile) and set target filter values.
- [ ] **Step 3 — Filter**: implement the three-layer filter in
      `FB_RoofMotor.TcPOU` with `VAR_INPUT` tuning parameters; keep the raw
      counter path available for comparison (e.g. a debug output
      `raw_counts`).
- [ ] **Step 4 — Re-sync**: re-enable the limit-switch position snap in
      `FB_RoofMotor`.
- [ ] **Step 5 — Validate**: on the live system, do N open/close cycles;
      verify `position` at the limits equals `min_position`/`max_position`
      exactly, and that no spurious `sync_error` / `limit_error` occurs at
      startup; verify no *legitimate* counts are lost (compare position
      against the raw counter over a full cycle).
- [ ] **Step 6 — Cleanup**: remove or disable the measurement task once the
      filter is validated; document the tuned values in the README
      (`Configuration` table) and in the code comments.

## 7. Files to touch

| File | Change |
|---|---|
| `MonetRoof/MONETroof/POUs/FB_RoofMotor.TcPOU` | filter chain + re-sync + tunable inputs |
| `MonetRoof/MONETroof/POUs/PRG_MeasureCounter.TcPOU` (new) | pulse measurement (temporary) |
| `MonetRoof/MONETroof/PlcTask.TcTTO` or `MonetRoof.tsproj` | fast measurement task |
| `MonetRoof/MONETroof/POUs/MAIN.TcPOU` | optionally publish measurement results via `comm` |
| `README.md` | document new inputs + tuned values |

## 8. Acceptance criteria

- No spurious `sync_error` / `limit_error` at roof start across repeated
  open/close cycles.
- `position` equals `min_position` / `max_position` exactly at the closed /
  open limit switches after each full cycle (self-correction works).
- No legitimate counts lost: total position change per full cycle matches the
  raw (unfiltered) counter within the expected tolerance.
- Filter parameters documented and tunable without code changes.
