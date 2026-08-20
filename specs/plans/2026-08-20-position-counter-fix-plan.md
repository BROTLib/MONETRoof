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

- **The recorded move ran at `max_speed` (confirmed)** — so the measured 0.60 s
  rotation period and 20 ms narrowest pulse *are* the worst (fastest) case, not
  a slow-mode snapshot. Full travel at `max_speed`: `200 counts × 0.60 s ≈
  **120 s** (the move in the recording was partial — 28 of 200 counts — so T
  should still be verified once, e.g. from the MQTT percent-open telemetry over
  a full open/close).
- **`counter_debounce := T#10MS`** — below the narrowest legitimate pulse
  (20 ms on c[2,2]) with 2× margin, above the EL1008's 3 ms input filter, so
  all legitimate pulses count and sub-10 ms chatter does not.
- **`counter_min_spacing` T#50MS is safe**: the measured legitimate period is
  600 ms, so 50 ms spacing has 12× margin and still kills burst pulses (which
  arrive ≪50 ms apart).
- The differing pulse widths per channel (20 vs 80–90 ms at the same rotation
  period) also hint at **unequal block widths or marginal gaps** — worth
  checking the c[2,2] sensor mounting (§3 sensor checks), since a marginal gap
  is the classic trigger for threshold flutter.
- **A rattle was later reproduced** — see the second recording below
  (`Roof2.svdx`); it shows the failure mechanism and changes the fix emphasis
  (§3.1).

### Second recording (Roof2.svdx, reproduced failure, 2025-08-20)

**12.47 s at 10 ms**, same channel mapping. Decode cross-checked (160 ms
series 78/78, sparse 9/9). Operator report: *roof half 1 failed right at the
beginning with a sync error — the two counters differed too much*.

| Channel | Counts | Behavior |
|---|---|---|
| counter[1,1] | **+1** | block crosses **into** the sensing zone at 3.91 s and **dwells there for 8.5 s** (rest of recording) |
| counter[1,2] | +0 | block parked in zone at t=0, crosses out at 3.09 s, never returns |
| counter[2,1] | +11 | clean pulses, 0.6 s cadence, 4.5–11.0 s |
| counter[2,2] | +11 | clean pulses, 0.6 s cadence, 4.47–10.8 s |
| opened[1..2,1..2] | 0 | never engaged |

Interpretation:

- **Half 2 moved normally** (11 pulses/drive, same cadence as the clean
  recording). **Half 1 never rotated**: the drives energised at ~3 s, the
  shafts jittered a fraction of a turn (blocks crossing their sensor
  thresholds at 3.09 / 3.91 s) and stalled — the mechanical "rattle".
- The visible count difference is **exactly one edge** (c[1,1] @3.91 s — the
  block crossing *into* the zone and staying), which is within the expected
  ±1 phase behaviour (§3.2). `sync_error := ABS(pos1 − pos2) >=
  max_position_diff (2)` therefore implies **the two counters already
  differed by ≥ 1 before this move** — residues accumulated over earlier
  partial moves (never fully reaching a limit), with this event pushing the
  difference over the threshold. (Confirm: were the counters reset before the
  test?)
- **There is no pulse burst in the 10 ms data.** The 250 µs measurement task
  remains the only way to see whether the *invisible* part of the rattle
  (sub-10 ms crossings, hidden by the 3 ms EL1008 filter and 10 ms sampling)
  produces additional counts.

Consequences for the fix (updates §5/§6):

- The **limit-switch position snap (§5 safety net) becomes the primary
  protection**: it re-zeros the counters at every full open/close, so ±1
  residues can no longer accumulate into a drift that trips `sync_error` at a
  later move. Re-enabling it is the highest-value change.
- **Root-cause remains open**: the recordings show clean pulses; whether the
  ≥2 difference comes from sub-ms bursts (unseen at 10 ms) or from residues
  stacking across partial moves is decided by (a) the counter reset question
  above and (b) the 250 µs watchdog (§6).
- Keep the debounce/spacing defaults (10 ms / 50 ms) — they cost nothing and
  cover fast bursts if they occur, but they are not what this recording

### Fourth recording (error event, 2025-08-20)

The recording where `sync_error` actually fired: **127.4 s at 10 ms**, 16
channels, repeated partial open/stop/close cycles with the error present for
~60 % of the time (|dp1| ≥ 2 from t = 3.39 s onward).

| Finding | Detail |
|---|---|
| **The counters count correctly** | all four channels: min edge interval 0.590 s, zero bursts, zero close pairs — every edge is a legitimate rotation pulse (c[1,1] 146, c[1,2] 144, c[2,1] 178, c[2,2] 179 edges) |
| **The error is a phase-offset + residue effect** | c[1,1]'s block leads c[1,2] by ~one rotation, so during motion \|dp1\| **oscillates 1↔2 every ~1.5 s** (p11 counts → 2, p12 counts → 1) and crosses the `max_position_diff = 2` threshold routinely |
| **The recording starts with residue 1** | p11 = 1, p12 = 0 at the closed position (from earlier moves, never re-zeroed) |
| **The residue survives full closes** | at the closed limit the counters read p11 = 1, p12 = 0 (half 2: 0/1) instead of 0/0 — the limit-switch position snap is disabled, so the residue carries into every cycle |
| Error onset | t = 3.39 s: p11 counts its 2nd edge while p12 is still 0 → \|dp1\| = 2 → `sync_error` |

**Conclusion**: this is exactly the operator-expected behaviour ("one drive may
count one more if it hits the limit first") made harmful by the disabled
re-sync. The two counters legitimately sit ±1 apart (phase-offset blocks on
mechanically connected drives); without the limit-switch position snap the
residue survives each cycle and the phase oscillation trips `sync_error`.
There is **no over-counting to filter** — the fix is the re-sync (§5 safety
net), not the debounce/spacing layers.
  needed.

### Third recording (16-channel, open/stop/close cycles, 2025-08-20)

Recording with the **absolute `position` counters added** (all 4 drives) plus
the `closed` limit switches: **70.4 s at 10 ms**, two partial open → stop →
close cycles, no sync error occurred.

| Finding | Detail |
|---|---|
| **c[1,1] runs +1 vs c[1,2] during moves** | c[1,1] logged 72 rising edges vs 68 for the other three drives (15 vs 14 up, 15 vs 14 down per cycle); `position[1,1]` is 1 ahead of `position[1,2]` during motion and they equalise at stops (max \|dp1\| = 1) |
| **The +1 is expected, not a fault** | the drives are mechanically connected; the two blocks sit at different angles, so pulses are phase-offset and a stop can legitimately leave one counter one ahead (operator-confirmed: moving to a limit, one drive may count one more if it hits the limit first). The sync tolerance (`max_position_diff = 2`) is exactly the headroom for this |
| First c[1,1] pulse per move is wide | 160–190 ms vs the 20–90 ms steady pulses (the roof is still accelerating) — not a fault signature |
| No bursts | no window with >1 edge per 200 ms anywhere |
| Full cycles self-cancel | both counters return to 0 at full close; the ±1 residue does not accumulate across full cycles |

**Consequence**: a counter difference of 1 is normal; `sync_error` (difference
≥ 2) is the fault case. Its mechanism is still not directly observed at 10 ms
resolution — candidates: (a) genuine over-counting (jitter bursts faster than
the 3 ms EL1008 filter / 10 ms sampling can show), or (b) accumulated ±1
residues across *partial* moves that never reach a limit (each stop leaves a
±1 residue that can stack to 2). The recordings so far only show clean
single pulses; the 250 µs watchdog (§6) exists to catch (a) if it occurs.

## 4. Decision point (after measurement)

**Decided by the recordings (§3.3):** there is no pulse over-counting — all
edges are clean rotation pulses at 0.6 s. The `sync_error` is the expected
±1 counter difference (phase-offset blocks on mechanically connected drives,
"one drive counts one more if it hits the limit first") made harmful by the
**disabled limit-switch re-sync**: the residue survives every cycle, the
phase oscillation pushes \|dp\| to 2 during moves, and the error trips. The
debounce/spacing filters do not address this. Fix: re-enable the position
snap (§5 safety net). The 250 µs watchdog remains only to rule out sub-10 ms
bursts in other failure modes, none observed so far.

## 5. Fix design: filter chain in `FB_RoofMotor`

**Key constraint (operator-confirmed): the two drives of a roof half are
mechanically connected, so they physically always rotate the same number of
times.** The two blocks sit at different angles, so the sensor pulses are
phase-offset and a stop can legitimately leave one counter one ahead of the
other — this is expected, and `max_position_diff = 2` is the headroom for it.
A difference of ≥ 2 is therefore the fault case: either genuine over-counting
(bursts) or residues that stacked across partial moves.

**Primary fix — the limit-switch position snap (safety net below).** The
recordings (§3.2, §3.3) show the counters count correctly and the error is
the un-cleared ±1 residue crossing the threshold via the phase oscillation.
Re-enabling the snap makes the counters agree after every full open/close,
which eliminates the failure. The debounce/spacing layers below are cheap
insurance only (no bursts have ever been observed).

Filter layers (insurance):

1. **Level debounce** — count only if the input stays high for a minimum
   time (kills short glitches). Must stay below the narrowest legitimate
   pulse (20 ms, §3).
2. **Rate limit** — ignore an edge that arrives within a minimum spacing
   after the last *accepted* count (kills the rest of a burst).

A "only count while moving" / limit-switch gate is **deliberately not used**:
during a move to a limit the roof legitimately counts up to the last rotation
(one drive may count one more as it hits the limit first), so gating on the
limit switches or on `real_speed` would swallow legitimate counts.

Sketch (replaces the counter block in `FB_RoofMotor`):

```st
// -- declaration additions --
counter_debounce    : TIME := T#10MS;   // min. on-time of the sensor pulse — keep < narrowest legit pulse (§3: 20 ms)
counter_min_spacing : TIME := T#50MS;   // min. time between accepted counts — keep < 600 ms rotation period (§3)
tonDebounce : TON;
tonSpacing  : TON;

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

**Design (symmetric, both ends, "both sensors" condition):** when **both**
closed limit switches are engaged → both motors' `position := min_position`;
when **both** opened limit switches are engaged → both motors'
`position := max_position`. Snapping both drives together (rather than
per-motor on each switch) avoids a transient `|diff| = 1` right at the limit.
`FB_Roof` already computes roof-level limit states from both motors
(`roof_limit_closed` / `roof_limit_open`), so the snap belongs there, setting
both motors' positions:

```st
// in FB_Roof (both motors of a half):
IF direction_close AND roof_limit_closed THEN
    motors[1].position := motors[1].min_position;
    motors[2].position := motors[2].min_position;
END_IF
IF direction_open AND roof_limit_open THEN
    motors[1].position := motors[1].max_position;
    motors[2].position := motors[2].max_position;
END_IF
```

(Equivalently, keep it in `FB_RoofMotor` per motor but driven by the
roof-level `roof_limit_*` signal instead of the per-motor switch.)

Rationale:

- **Symmetric correction**: the ±1 residue appears on both move directions;
  correcting only on close leaves drift uncorrected during runs that never
  fully close (partial moves, repeated opens), so `sync_error` could still
  trip mid-run.
- **Correct `percent_open` at full open** (exactly 100, and `0` at closed).
- **Counters agree at every limit** — `|diff| = 0` at both travel ends.

Caveats:

- Keep the direction gate (`direction_open`/`direction_close`) so a
  misadjusted or bouncing switch cannot corrupt the counter while the roof is
  not moving toward that limit.
- **Boot / warm-restart snap**: the limit may already be engaged at startup
  (no rising edge). On init, if `roof_limit_closed`/`roof_limit_open` is
  true, set the positions immediately.
- **Verify the opened limit repeatability** once: no recording so far reached
  full open (max 61/200), so the `opened` switches have never engaged in the
  data — confirm they trigger at a repeatable position before relying on the
  open snap.

The counter then becomes **self-correcting** at both travel ends: even if
counts slip through, every full open and every full close re-zeroes both
positions. This is the fix for the measured failure (§3.3).

### Rules of thumb for tuning

- `counter_debounce` ≥ max measured noise pulse width, **and** below the
  shortest *legitimate* pulse width at `max_speed` — that is
  `block angular width fraction × T/200` (block width as a fraction of one
  rotation, times the rotation period at `max_speed`). Measured at `max_speed`:
  narrowest legitimate pulse = **20 ms** (c[2,2]) → `counter_debounce = 10 ms`
  (2× margin). Note the EL1008 3 ms filter already removes sub-3 ms chatter
  before the PLC sees it.
- `counter_min_spacing` ≥ max measured burst cadence, **and** below the
  shortest legitimate pulse period at `max_speed` — with one block per
  rotation that is the rotation period at `max_speed` (measured **600 ms**).
  The sensor's 600 Hz switching frequency sets an absolute floor of ~1.7 ms,
  the EL1008 filter ~3 ms; 50 ms sits comfortably between the (unmeasured)
  burst cadence and the 600 ms legitimate period.
- Start with the measured defaults (debounce 10 ms, spacing 50 ms), then
  tighten only if validation shows legitimate counts being lost or bursts
  slipping through.

## 6. Implementation steps

- [ ] **Step 1 — Measurement**: done — four recordings (§3, §3.1, §3.2, §3.3)
      give the legitimate pulse width (20–90 ms), cadence (600 ms) at
      `max_speed`, the expected ±1 phase behaviour, and the error mechanism
      (un-cleared residue + phase oscillation, §3.3). **The 10 ms PLC cadence
      is sufficient**: no sub-10 ms burst has ever been observed, the
      narrowest legitimate pulse (20 ms) is resolved with ≥2 samples, and the
      EL1008 3 ms input filter caps anything faster regardless of task speed.
      The `PRG_MeasureCounter` fast task (250 µs, priority 10) is optional —
      useful only as a one-off pulse-width diagnostic, not part of the fix.
- [ ] **Step 2 — Decide**: decided — no over-counting; the fix is the re-sync
      (§4, §5).
- [ ] **Step 3 — Filter (optional insurance)**: implement the
      debounce/spacing layers in `FB_RoofMotor.TcPOU` with `VAR_INPUT` tuning
      parameters; keep the raw counter path available for comparison (e.g. a
      debug output `raw_counts`).
- [ ] **Step 4 — Re-sync (highest value)**: implement the symmetric limit
      snap in `FB_Roof` (both motors snap to `min_position`/`max_position`
      when *both* closed / opened switches engage, §5 safety net) — clears any
      residue at every full open and close, and (given the mechanical
      connection) guarantees the two counters of a half agree after every full
      cycle.
- [ ] **Step 5 — Validate**: on the live system, do N open/stop/close cycles
      (the test that reproduces the failure); verify `position` at the limits
      equals `min_position`/`max_position` exactly, that the two counters of
      each half agree after every cycle, that no spurious `sync_error` /
      `limit_error` occurs at startup, and that no *legitimate* counts are
      lost (compare position against the raw counter over a full cycle).
      Also watch the MQTT telemetry / measurement task over normal operations
      for any first naturally occurring sub-10 ms burst.
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
