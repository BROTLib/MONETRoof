# Plan: Fix the roof position counter (pulse rattling / over-counting)

Status: draft — measurement first, then implement the filter. First live
recording (`Roof.svdx`, Becky) decoded 2026-08-20 — measured values in §3;
decoding format documented in
`BROTLib/specs/design/twincat-scopeview-svdx-format.md`.

## 1. Problem

`FB_RoofMotor` counts inductive-sensor pulses with a simple edge counter:

```st
counter_trig(CLK := counter);
IF counter_trig.Q THEN
	IF direction_open THEN position := position + 1;
	ELSE                     position := position - 1;
	END_IF
END_IF
```

When the roof starts moving, the drive **rattles**: the sensor signal bounces
high/low across several PLC cycles, and each real rising edge is counted.
With `max_position_diff := 2` (synchronisation tolerance), a few spurious
pulses on one motor of a roof half trip `sync_error` (or `limit_error`) and
stop the roof.

Observed symptom: roof rattles briefly at start, position counter jumps, roof
stops with a sync/limit error.

### Physical setup (as built)

The counting target is a **small metal block mounted on a metal shaft**, rotating
past the sensor once per rotation. The sensor is confirmed as an **inductive
proximity switch** (Data Sensing AK1/AP-2A, see below); the "hall-sensor"
wording in the code/README should simply be corrected. This changes the
analysis:

- **One pulse per shaft rotation.** With `max_position = 200` counts over the
  full travel, a full open/close is 200 shaft rotations. The rotation rate —
  and with it the legitimate pulse rate — depends on *which* shaft carries the
  block (motor-side shaft spins fast, slow output shaft slow); confirm it and
  measure the full-travel time `T` (§3). Unless the shaft is geared up, the
  legitimate pulse rate is very low (huge filter headroom, §3).
- **Pulse width scales with rotation speed.** The sensor output stays high
  while the block is in front of it:
  `width ≈ (block angular width / 360°) × rotation period`. Pulses are *wide*
  at low speed and *narrow* at high speed — the debounce time must stay below
  the narrowest legitimate pulse (the one at `max_speed`), otherwise real
  pulses get swallowed.
- **The rattle is the block edge oscillating across the trigger threshold.**
  At motor start the drive train vibrates (brushed DC motor, gearbox backlash,
  torsional compliance), so the block edge crosses the sensor threshold several
  times in quick succession — every rising edge is counted. A marginal
  block–sensor gap (mounting distance, wear, temperature drift) makes the
  flutter worse and can even trigger at standstill.
- **The sensor carries no direction information.** The PLC infers the direction
  from the commanded movement, so oscillation pulses accumulate in the
  commanded direction even if the shaft physically reverses briefly (backlash).

### Sensor: Data Sensing AK1/AP-2A (inductive, M18 unshielded)

| Parameter | Value | Consequence |
|---|---|---|
| Output | PNP, NO (active-high) | rising edge = block entering range — matches the `R_TRIG` edge counting |
| Nominal sensing distance | 8 mm (operating 0–6.48 mm) | only the *derated* distance matters: a small target and the material reduce it |
| Standard target | 24 × 24 mm FE360 steel | the block is "small" → reachable distance shrinks; stainless steel (e.g. shaft/block) has factor 0.77 |
| Hysteresis | 1–20 % of Sr | switch-on and switch-off points differ → a block edge dwelling in the band chatters; prime suspect for the rattle |
| Switching frequency | 600 Hz | legitimate pulses can be no closer than ~1.7 ms at the sensor → huge filter headroom (§3/§5) |
| Repeat accuracy / thermal drift | < 5 % / < 10 % | a marginal gap is fragile across temperature — leave mounting margin |
| Availability delay | ≤ 50 ms after power-up | pulses during the first 50 ms after a PLC restart may be missed |

Two practical consequences:

- **Hysteresis dwell at the travel ends**: if a roof stop leaves the block
  inside the sensing zone (block stopped over the sensor), the output sits
  between the switch-on/off points — any vibration then makes it flutter.
  The limit-stop positions should be checked against the sensing zone.
- **The shaft itself must stay out of range**: the sensor is *unshielded*
  (8 mm field) — verify the shaft surface alone never enters the sensing
  range, otherwise the output would be permanently high or marginal at
  closest approach.

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
  - mechanical oscillation of the block edge across the trigger threshold,
  - a marginal block–sensor gap / trigger threshold, or
  - EMI on the wiring.
- Whatever we measure through the terminal is already filtered by the 3 ms
  input filter — keep that in mind when sizing the software filter.

### Sensor-specific checks (AK1/AP-2A)

- **Gap margin**: compare the mounted block–sensor gap against the *derated*
  operating distance — 8 mm nominal only holds for a 24 × 24 mm FE360 target;
  a smaller block and/or stainless steel (factor 0.77) shrink it. With 10 %
  thermal drift and 5 % repeat accuracy, a gap near the edge of range is
  unreliable.
- **Block geometry**: measure the block's angular width and the time of a full
  open/close at `max_speed` (`T`). Together they give the narrowest legitimate
  pulse width (`block angular width fraction × T/200`) that the debounce must
  stay below.
- **Stop positions**: where do the roof halves stop? If a stop leaves the block
  in the sensing zone (hysteresis band), vibration at standstill produces
  counts — this would confirm the "only count while moving" gate as essential.
- **Pulse floor**: the sensor's 600 Hz switching frequency caps legitimate
  pulses at ~1.7 ms apart; the EL1008's 3 ms input filter raises the effective
  floor for what the PLC can see. Both are far below any realistic rotation
  period here, so they only matter as the lower bound for the filter.

### Measurement task (recommended): fast task + timestamps

Add a dedicated task (e.g. **250 µs, priority 10** — above PlcTask priority 20)
that only samples the counter input and timestamps edges with
`F_GetSystemTime` (100 ns resolution, Tc2_System). Sampling at 250 µs resolves
pulses down to ~0.5 ms with ~30 samples on a 3 ms pulse; a 250 µs task is a
negligible load on a CX.

```st
// PRG_MeasureCounter — runs in a 250 µs task
VAR
    counter       AT %I* : BOOL;        // sensor input of the motor in question
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
  - whether rising and falling counts cancel out, or accumulate,
  - whether bursts are position-correlated (only while the block is at the
    sensor) or happen anywhere — position-correlated bursts point to the block
    edge dwelling in the trigger hysteresis band (slow rotation / oscillation),
  - the pulse width vs. rotation speed, to verify
    `width ≈ block angular width / rotation period` and to size the debounce
    against the narrowest pulse at `max_speed`,
  - whether the block–sensor gap is marginal (compare the mounted gap with the
    sensor's specified sensing distance; threshold flutter is the classic
    symptom of a marginal gap).
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

### Measured values (Roof.svdx, live recording from Becky, 2025-08-20)

First recording decoded: **20.47 s at 10 ms** (PLC task sampling — so sub-10 ms
detail is aliased, see above), 8 channels, one move with a velocity ramp-up.
Decoded from the SVDX binary (10 ms series + 160 ms decimation cross-checked
at 128/128 samples):

| Quantity | Measured | Implication |
|---|---|---|
| Rotation period at run speed | **0.60 s** (≈1.67 rev/s), settling after ~1 s ramp | ~100 rpm; a full travel of 200 counts ≈ **120 s** at this speed |
| Pulse width per channel | c[1,1] 40–80 ms, c[1,2] 80–90 ms, c[2,1] 70–80 ms, c[2,2] **20–40 ms** | blocks differ in width and/or sensor gap; narrowest ≈ **20 ms** |
| Pulse cadence | clean single pulses, period 0.59–0.60 s steady | **no rattle/bursts in this recording** — the over-counting does not reproduce on every move |
| `opened` limit switches | never engaged | recording captured mid-travel, not a full cycle |
| Start behavior | c[1,2] + c[2,1] already high at t=0 (blocks parked in the sensing zone), stable for 2.9/3.2 s, then left the zone | the hysteresis-dwell case is real, but no flutter in this run |

Sizing consequences (validated against §5):

- **`counter_debounce` default T#20MS is marginal**: it equals the narrowest
  legitimate pulse measured here (20 ms on c[2,2]). If the move was at
  `max_speed`, a 10 ms debounce is the safer default; if it was a slow-mode
  move, pulses at `max_speed` would be ~3× narrower (≈7 ms) and the debounce
  must be ≤10 ms anyway. → **confirm whether the recording ran at
  `max_speed`, then set `counter_debounce := T#10MS`.**
- **`counter_min_spacing` T#50MS is safe**: the measured legitimate period is
  600 ms, so 50 ms spacing has >10× margin and still kills burst pulses
  (which arrive ≪50 ms apart).
- The differing pulse widths per channel (20 vs 80–90 ms at the same rotation
  period) also hint at **unequal block widths or marginal gaps** — worth
  checking the c[2,2] sensor mounting (§3 sensor checks), since a marginal gap
  is the classic trigger for threshold flutter.

## 4. Decision point (after measurement)

Depending on the measurement results:

1. **Pulses are wide / bursts are long** (≥ several ms, spaced close) →
   proceed with the software filter chain (§5).
2. **Pulses are sub-ms and the EL1008 filter already hides them** → the
   over-counting is *not* sensor chatter; look at the motion profile instead:
   - raise `min_speed` so the roof starts cleanly and leaves the resonant
     speed range quickly,
   - tune `acceleration`,
   - check the block/sensor mounting.
3. **Mechanical oscillation of the block past the sensor** (shaft torsional
   vibration / gearbox backlash at start, or a marginal block–sensor gap) →
   no counter filter fully fixes it; fix the mechanics and/or the sensor
   mounting/gap, and use the filter only as insurance.

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
counter_debounce    : TIME := T#20MS;   // min. on-time of the sensor pulse — keep < narrowest legit pulse (§5)
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

- `counter_debounce` ≥ max measured noise pulse width, **and** below the
  shortest *legitimate* pulse width at `max_speed` — that is
  `block angular width fraction × T/200` (block width as a fraction of one
  rotation, times the rotation period at `max_speed`). The default 20 ms is
  only safe if the block is wide and/or the shaft turns slowly; validate it
  against the measured numbers (§3). Note the EL1008 3 ms filter already
  removes sub-3 ms chatter before the PLC sees it.
- `counter_min_spacing` ≥ max measured burst cadence, **and** below the
  shortest legitimate pulse period at `max_speed` — with one block per
  rotation that is the rotation period at `max_speed` (`T/200`). The sensor's
  600 Hz switching frequency sets an absolute floor of ~1.7 ms, the EL1008
  filter ~3 ms; for any realistic roof travel time the legitimate period is
  far above 50 ms.
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
