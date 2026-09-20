# Code review of MONETRoof (develop @ f315116)

**Status: draft. Review finished, no fixes applied. Nothing here has been run on a PLC.**

Reviewed at `develop` f315116. `origin/main` is 2 commits ahead (TcBuild workflow and runner labels);
those only matter for the CI and release sections.

Scope: the 5 POUs (`MAIN`, `FB_RoofControl`, `FB_Roof`, `FB_RoofMotor`, `FB_Ramp`, about 830 lines of ST
including declarations), `Global_Version`, `PlcTask`, the I/O links in `MonetRoof.tsproj`, the TwinSAFE
`.sal`, the access rights in the visualization, `.github/workflows/release.yml`, `README.md` and the
position-counter plan. BROTLib (`FB_Comm_MQTT*`, `FB_EventLog`, `I_Roof`) and MONETN/MONETS were only
read at the call sites.

Not reviewed: `data/decode_svdx.py` and the recordings, the layout of the visualization, the compiled
`_Boot/` and `_Libraries/` binaries, and whether the TwinSAFE logic is *correct*. The safety project
needs someone qualified for it, I only looked at how the PLC talks to it.

## How this was checked (and what that is worth)

- **Static read** of every POU, then a hand comparison of the README I/O table against the `<Link>`
  entries in `MonetRoof.tsproj` (all drive, counter, limit, fault and button rows match).
- **Two small scripts** in `testing/`, both standalone `python3`, no dependencies:
  `check_ramp_overflow.py` (port of `FB_Ramp` under two assumptions about INT arithmetic) and
  `check_debounce_aliasing.py` (sampling model of the counter debounce).
- **Limit:** these test my port and my model, not the compiled ST. TwinCAT-specific behavior (INT
  overflow, `TON` resolution, persistent data, EtherCAT input handling) is not covered. Findings are
  marked **verified (port)**, **verified (model)**, **read from code** or **unsure**.

## Findings, worst first

### High

**H1. `FB_Ramp` can turn a direction reversal into an instant full-speed reversal.**
`FB_Ramp.TcPOU:19` computes `target - value` on two INTs. A reversal from -30000 to +30000 needs 60000,
which does not fit in 16 bit. *Verified (port), if TwinCAT truncates to 16 bit:* the wrapped value is
negative, the code takes the "decrease" branch, `value <= target` is true at once, and speed jumps from
-30000 to +30000 in one 10 ms cycle (same the other way round). It happens for any reversal at more than
about 2767 counts of speed (9 % of `max_speed`), so the ramp only works for stop-then-go. If TwinCAT uses
wider intermediates the ramp is fine. **I cannot tell which from the source, unsure.** Test on the PLC in
a few minutes: set `speed` to -30000 and `target_speed` to 30000 on a `FB_Ramp` instance and step one
cycle.
Who triggers it: `Open()` while closing in automatic mode, the open button pressed while the roof is
still closing in manual mode, and the MONETS MQTT watchdog (`MONETS/MONETS/MONETSRuntime/POUs/MAIN.TcPOU:178-187`
calls `RoofControl.Close()` every cycle after a connection loss, which can arrive while the roof is opening). Effect if real: a
brushed DC drive and the roof gearing take a full-speed reversal with no ramp. Fix: do the arithmetic in
`DINT` (`TO_DINT(target) - TO_DINT(value)`), and reject a reversal until speed has ramped through zero.

**H2. Nothing bounds a move: no time-out, no stall check, no over-travel limit.** *Read from code.*
While commanded to move, the roof half stops only on: a limit switch pair (`FB_Roof.TcPOU:115`), a
`sync_error`, `direction_error` or `limit_error`, or the drive fault input. Gaps:
- Both drives jammed (ice, snow, obstruction): no counter edges, so no position difference, so no error.
  The roof pushes at the commanded speed until the drive protects itself.
- No software end stop. Past `max_position` the code just keeps counting (`percent_open` above 100, speed
  held at `min_speed`, `FB_Roof.TcPOU:102-105`). `limit_error` needs one limit switch to engage first, so
  two failed switches (or a mis-set switch on a half) are not caught.
- The drive fault input is active-low (`FB_RoofMotor.TcPOU:71`), so a lost 24 V supply on that terminal
  does stop the roof. That covers one common cause, not a jam.
Fix: a "moving but no count for N seconds" watchdog per drive, and a stop when
`position > max_position + margin` (or below `min_position - margin`). Size N from the 0.6 s rotation
period in the plan.

### Medium

**M1. The boot-time position snap reads inputs that are not set yet.** *Read from code.*
`FB_Roof.TcPOU:121-129` snaps both drives to `motors[i].min_position` / `max_position` on the first cycle.
Those are `VAR_INPUT`s of `FB_RoofMotor`, assigned only when the motors are called at line 152, after the
snap. On the first cycle they are 0. Parked closed works by luck (`min_position` is 0 anyway). Parked
**open**, `position` is set to 0 instead of 200 (and it overwrites a correct persisted value).
Consequences: 0 % open shown for a fully open roof, and the next close runs at `min_speed` the whole way
(percent below `limit_slowdown`, `FB_Roof.TcPOU:91-101`) until the closed switches re-sync. Fix: use the
`FB_Roof` inputs `min_position` / `max_position` (already valid on the first cycle). Check on the PLC:
restart with the roof parked open and read `percent_open`. The 2026-08-21 validation in the plan covered
a full close and a full open, not this case.

**M2. The standalone `MAIN` never gives `RoofControl` to `comm`, so remote roof commands do nothing.**
*Read from code.* `FB_Comm_MQTT_Influx._handleMQTTMessage` routes `dome_open`, `dome_close`, `dome_stop`
to `Roof.Open()` etc. only if its `Roof` input is set. `MAIN.TcPOU:37-44` does not set it (MONETN and MONETS
do: `Roof := RoofControl`). The result is a "MQTT not understood" warning. The README section
"Telemetry and communication" says the commands are routed. Either this application is only a bench/test
target (then say so in the README) or `Roof := RoofControl` is missing. Owner to confirm which controller
runs this application. It also has no comm-loss handling, which MONETS has.

**M3. `direction_error` cannot fire.** *Read from code.* `FB_Roof.TcPOU:175` compares
`motors[1].real_speed * motors[2].real_speed < 0`, but both motors are always called with the same `speed`
(line 157), so the product is never negative. It checks commanded values, so it also cannot see a drive
that physically turns the wrong way (one counter per drive carries no direction). The product of two INTs
at 30000 also overflows 16 bit, which is fragile in itself. Either remove it and its README entry, or
detect it from something measured. The README lists it as an active safeguard.

**M4. The counter debounce equals the task cycle, which leaves no margin.** *Verified (model), unsure on
the PLC.* `counter_debounce = 10 ms` on a 10 ms task means "two consecutive high samples", and `ET >= PT`
is evaluated exactly on its boundary. In the model, pulses of 20 ms or more are all counted with a perfect
10.000 ms cycle, but with 50 us of cycle jitter a 20 ms pulse is counted 51 % of the time and a 25 ms
pulse 76 %. Whether `TON` sees such jitter is **unsure** (depends on how `TON` reads time). The plan
sizes the debounce as "2x margin below 20 ms", but the 20 ms comes from ScopeView at 10 ms sampling,
which the plan itself calls aliased (section 3): two high samples means a true width between 10 and 30 ms.
Lost counts are hidden by the limit re-sync at every full open/close, and show up as sync errors on
partial moves. The open step 5 of the plan (compare `raw_counts` with `position` over a full cycle) would
show it. A safer value is 3 to 5 ms (above the EL1008 3 ms input filter, below one cycle).

**M5. The E-stop restart and error acknowledge are wired to PLC outputs that nothing drives.**
*Read from code, partly unsure.* In `TwinSafeGroup1.sal` the `Restart` input of `FBEstop1` comes from the
standard alias device `Restart` (sds 4), and the group `ErrAck` from alias sds 1. `MAIN.restart` and
`MAIN.errack` (`MAIN.TcPOU:19-20`) are never written, and the visualization does not reference them.
`running := TRUE` is set unconditionally (line 25). So after an E-stop the safety group cannot restart
from the PLC side unless someone forces `MAIN.restart` in XAE. Also **unsure:** the committed
`MonetRoof.tsproj` has no `<Link>` for `MAIN.running`, `MAIN.restart`, `MAIN.errack` or
`RoofControl.ups_fail`. Either the safety alias links are stored elsewhere or they are not committed.
Check in XAE. The README says the PLC "provides the corresponding handshake outputs".

**M6. Nothing checks the EtherCAT state.** *Read from code, behavior unsure.* No `WcState` or
`InputToggle` variable is used by the PLC (the `tsproj` comments at lines 66-67 describe them; only the
NC drive/encoder entries link them). If a terminal drops out and TwinCAT keeps the last input image, the
roof sees stale limit switches, counters and `motor_error`. I did not verify what the master does with
inputs of a lost slave, check the Beckhoff Information System (EtherCAT, WcState) before relying on either
reading. Fix: map the `WcState` of the roof bus and treat "not valid" like a drive error.

**M7. `position` is the only state that survives a restart, and nothing validates it.**
*Read from code, persistence behavior unsure.* `position` is `VAR_OUTPUT PERSISTENT`
(`FB_RoofMotor.TcPOU:18`). When TwinCAT writes persistent data (only on a controlled shutdown, or on
change) I did not check, so after a power loss the stored value may be stale, and a change to the FB
layout on download can reset it. `RoofControl.ups_fail` (`FB_RoofControl.TcPOU:30`) exists but is never
read. A stale position means `limit_slowdown` acts at the wrong place, so the roof can reach a limit at
full speed and hit `Stop()` (which zeroes speed with no ramp, `FB_Roof.TcPOU` `Stop` method). Options: do
not trust `position` after a restart unless a limit switch is engaged, or run the first move after boot
at `min_speed`.

**M8. Releases are inconsistent, and the next one would fail half way.**
*Verified with `git rev-list` and `git show`.*
- Tags `v1.1.0` and `v1.1.1` both contain `<ProjectVersion>1.0.0</ProjectVersion>`, and
  `Global_Version` says 1.0.0 on `develop`. The tag names do not match what is built in.
- `origin/main` has 2 commits that `origin/develop` lacks, and `develop` has 1 that `main` lacks
  (same shape as the AstroBROT M8 that was resolved on 2026-09-20). `release.yml` pushes the bump to
  `develop`, then runs `git merge --ff-only develop` on `main`. With the divergence the merge fails after
  the bump is already on `develop`, so no tag. Merge `origin/main` into `develop` first.
- MONETN consumes this as a library with `MONETRoof, *` (no version pin, `MONETN/MONETN/MONETNRuntime/MONETNRuntime.plcproj:130-132`), and
  no `.library` is committed here. What MONETN builds against is whatever is in each dev PC's library
  repository. This is the same mechanism that the plan's "stale `Becky` clone" note describes. See the
  CI section.

**M9. No automated tests; validation was one close and one open.** The plan's step 5 is still open
(`raw_counts` against `position` over a full cycle, long-term burst watching). Nothing tests `FB_Ramp`,
the sync/limit logic or the state machine. These blocks are plain logic and would suit a simulation
program with unlinked `AT%I*` variables (unlinked ones are ordinary memory, so a test can write them) or
TcUnit. H1, M1 and M3 are exactly what such tests catch.

### Low

- **L1. Errors act one cycle late, and slow mode depends on statement order.** The motors are called at
  `FB_Roof.TcPOU:152`, the error `Stop()` is at line 184, so a fault takes effect on the next cycle.
  Worse, a level command (manual button held, or `open_roofs` set) re-arms the ramp every cycle:
  `vel_ramp` runs from 0 to 150 before the error stop, so the drive gets `real_speed = 150` each cycle
  while the error persists. That is 0.5 % of `max_speed`, probably below what the drive reacts to
  (*unsure*). The documented "slow mode ignores all errors" also only works because the motor call sits
  before the error stop. A refactor that moves it would silently change that.
- **L2. `limit_error` can fire once after a limit snap.** `SetPosition` does not refresh `position_open` /
  `position_close`. If the first drive engaged its switch at 198, the snap to 200 gives
  `MovedSinceOpen = 2`, which is `>= max_position_diff` (`FB_Roof.TcPOU:177`), and `is_opening` is still
  the value from the top of the cycle. Needs the switch position to be off by 2 or more, so it depends on
  the repeatability the plan says was validated once. *Read from code.*
- **L3. `FB_Roof.Error` omits `drive_error`** (`FB_Roof.TcPOU:291-298`). Telemetry shows state `ERROR`
  with an empty `MONET.ROOF.ERROR1` when a drive fails.
- **L4. `fTelemetryInterval` is only read at initialization** (`FB_RoofControl.TcPOU:53`,
  `tonComm : TON := (PT:=fTelemetryInterval)`, and the call never passes `PT`). Changing it at runtime
  does nothing. *Unsure* whether the initializer even picks up a non-default value.
- **L5. State reporting is looser than the limit logic.** `state := opened` when *either* motor's open
  switch is engaged (`FB_Roof.TcPOU:204`), while `is_open` needs both. A half stopped between the two
  switches reports OPENED. `opened` and `closed` engaged together is not treated as a sensor fault. The
  `ELSE` at line 201 and the last `ELSE` at line 210 are unreachable. The initial state is `closed`
  (enum value 0 in `E_RoofState`), not `unknown`.
- **L6. Unvalidated inputs.** `max_speed` above 32767 wraps in `TO_INT`, `min_speed > max_speed` wraps the
  UINT subtraction at line 96/107, `max_position = min_position` divides by zero at
  `FB_RoofMotor.TcPOU:88`, `max_position_diff = 0` gives a permanent error, and `position` is an
  unbounded INT (wraps after 32767 counts, which would show as a sync error). `acceleration` is per call, so
  ramp times change with the task cycle time.
- **L7. `percent_open` is not clamped.** Telemetry `REALPOS` can leave [0, 1] after a counter error, and
  `-1.0` is used as the error marker.
- **L8. Dead or duplicated code.** `RoofControl.ups_fail`, `FB_Roof.stop_roof`, `FB_RoofControl.j`, the
  `VAR_INPUT`s `is_open` / `is_closed` (overwritten inside), `percent_open` assigned twice
  (`FB_Roof.TcPOU:71` and `:166`), `Status` implemented twice, the commented-out `is_open` lines.
- **L9. Events are one-shot and telemetry ignores return values.** `FB_EventLog` publishes on the edge with
  QoS 0, so an event raised while MQTT is down (for example at boot) is lost. `_SendTelemetry` sends 12
  values in one cycle through `Publish` with `bQueue := FALSE` and does not look at `published`. Both are in
  BROTLib. *Unsure* whether the client drops a message when busy.
- **L10. README and plan do not match the code.** README says `sync_error` triggers when the drives differ
  "by more than" `max_position_diff`, the code uses `>=` (`FB_Roof.TcPOU:176`, so with 2 the tolerance is
  1). README says `limit_error` counts travel "since leaving its limit switch", the code counts from
  *engaging* it. `ups_fail` is listed as an input "the application can drive", it is not read anywhere.
  The `MAIN` state flags are plain variables, not "dedicated boolean outputs". The plan dates its
  measurements "2025-08-20" in two places (the plan is 2026-08-20). README also lists build configurations
  for x86, CE7 and ARM that I did not check.
- **L11. Repo housekeeping.** `MonetRoof.tsproj.bak` is tracked and differs from the current `tsproj`
  (about 40 lines). The `tsproj` carries stale links with `RestoreInfo="ANotFound"` (old names such as
  `open_roof1`). `FB_Ramp.TcPOU` was saved by TwinCAT 3.1.4026.8, everything else by 4024. The remote branch
  `feature/samespeed` is merged into `develop`, and the local `docs/plan-validation` has no remote.
  `TwinSAFE/TargetSystemConfig.xml` points at AmsNetId `5.146.183.126.2.1` while the `tsproj` targets
  `5.131.251.159.1.1`. Two different controllers, *unsure* what that means; before a safety download check
  serial number and project CRC against the physical EL6910.

## Security

Nothing here is exploitable from the internet by itself. The repo is **public** (`gh repo view`).

- **S1 (Medium). The roof command path is plaintext and unauthenticated.** `FB_Comm_MQTT` connects with host,
  port, client id and keep-alive only: no user, password or TLS. Anyone who can publish to the subscribe
  topic can send `command ... dome_open` or `dome_close` (in automatic mode; in manual mode a remote open
  is overwritten on the next cycle by `Manual()`, a remote stop is not). Whether the broker restricts
  clients (ACL, network segment) is not visible from here. Beckhoff's TF6701 client has fields for
  credentials and TLS (*from memory, check the TF6701 documentation*). Ask what the broker enforces.
- **S2 (Low-Medium). The visualization has no access rights.** All 99 `UserManagementAccessRights` lists in
  `Roof.TcVIS` are empty, and it exposes `zero_counter`, `slow_open`, `slow_close` and reset. I did not
  check whether the visualization is served over the network.
- **S3 (Low). Script injection in `release.yml`.** The "Validate version format" step puts
  `${{ inputs.version }}` straight into the shell, so `$(...)` runs before the regex check. Only someone who
  can already dispatch the workflow (`contents: write`) can use it. Pass it through `env:`. Same as
  AstroBROT S1. Also no build or test gate before the tag, and `actions/checkout@v4` is pinned by tag.
- **S4 (Low). Internal details in a public repo.** Broker `10.129.129.76:1883` and topic names
  (`MAIN.TcPOU`, README), AMS Net IDs in `tsproj` and `TargetSystemConfig.xml`, `<Worker>husser</Worker>`,
  and `MonetRoof/TrialLicense.tclrs` with a `SystemId` and license key (expired 2024-11-09, so it does
  nothing, but it is still a hardware-bound file that does not need to be in git). About 47 MB of binaries
  are tracked (`_Boot/`, `_Libraries/`); I did not open the `.tpzip` / `.occ` archives, which can hold
  settings.

## Design assessment

1. **The split is sound.** Control, roof half and drive are separate blocks, commands are methods, and the
   mapping table in the README is accurate. The problems are in the seams (H1, M1, M3, L1), not in the
   structure.
2. **Protection watches commanded values, not measured ones.** Sync, direction and limit checks all derive
   from one counter per drive. There is no independent plausibility check (time, count rate, current).
   That is why H2 and M3 exist.
3. **Two command styles are mixed.** `Automatic()` and `Manual()` read *level* inputs, `Open()` and
   `Close()` latch a target, `Stop()` and `SoftStop()` differ, and the error stop runs after the outputs
   are written. A single "requested speed" path with one safety stage right before the outputs would make L1
   and M7 disappear and make the ordering explicit.
4. **Application and library are the same project.** MONETN and MONETS use `FB_RoofControl` as a library
   (`MONETRoof, *`), but the project is a PLC application (`Released false`, `MAIN`, hard-coded broker and
   topic). `AT%I*` inside the blocks ties the terminal mapping to instance paths (`MAIN.RoofControl...`),
   which is why the `tsproj` has stale links and why each consumer re-maps 40 signals. Beckhoff's
   `{attribute 'TcLinkTo'}` can link a variable to a terminal at build time (*from memory, check the
   Infosys page*), which would keep the mapping with the code.
5. **Timing is tied to the 10 ms task** in three places: ramp per call, the debounce (M4) and the 50 ms
   count spacing. Fine while the cycle stays 10 ms, but it is not written down next to the parameters.
6. **What is good:** the position-counter fix is documented with the measurements behind it, the
   motion gate and the limit re-sync are reasoned in comments, the drive fault input is active-low, and the
   slowdown and limit-stop logic is simple enough to read in one pass.

## CI

- **Build:** `tcbuild-test.yml` exists on `origin/main` only (`workflow_dispatch`, self-hosted runner with
  labels `self-hosted, twincat, windows`, `TcBuild build MonetRoof.sln`). Last run succeeded 2026-09-16,
  1m30s (`gh run list`). `develop` does not have it (M8). Same tool and runner as the AstroBROT section;
  `TcBuild` does not run tests. I did not re-check its licensing questions here.
- **Library artifact:** because MONETN resolves `MONETRoof, *` and nothing is committed, a `TcBuild install`
  step that uploads the `.library` as a workflow artifact would give consumers something versioned to
  compare against, and would settle "is that controller running the fixed code" without visiting the PC.
- **Tests:** would need a TwinCAT runtime, same open questions as AstroBROT (licensing, unattended
  runs). A PLC-side simulation program (M9) can be run by hand first.

## Suggested order of work

1. H1 test on the PLC (5 minutes). If real, fix `FB_Ramp` first.
2. M1 (one-line change plus a restart test), M8 (merge `main` into `develop`, fix the version numbers).
3. H2 (count watchdog and over-travel stop), M3 (drop or replace `direction_error`).
4. M5 and M6: settle how the E-stop restarts and add the `WcState` check.
5. M4 after the `raw_counts` vs `position` comparison from plan step 5, then M7.
6. S1 with whoever runs the broker, then S2 to S4 and the Low items with any of the above.
