# MONET Roof

TwinCAT 3 control application for the roof of the MONET telescopes
(Georg-August-Universität Göttingen).

The roof consists of two independently driven roof halves, each moved by two
brushed DC motors. The application provides automatic and manual roof control,
position tracking via inductive-sensor counters and limit switches, velocity
ramping with slowdown near the travel limits, monitoring of the two drives of
a roof half (synchronisation, direction, travel limits, drive faults) and MQTT
telemetry and logging. The safety logic (TwinSAFE) is not part of this
project.

The application is built on the **BROTLib** library (`I_Roof`, `I_Comm`,
`FB_Comm_MQTT_Influx`, `FB_EventLog`, `E_RoofState`, ...).

This repository holds only the library function blocks: `FB_RoofControl`,
`FB_Roof`, `FB_RoofMotor` and `FB_Ramp`. There is no PLC application here
(no `MAIN`, no PLC task) — MONETN and MONETS each wire `FB_RoofControl` into
their own `MAIN`, with their own I/O mapping, broker and parameters. This
project used to include a standalone test application with hardcoded
parameters and a dummy hardware setup (I/O devices, a TwinSAFE project); both
were removed once they were no longer needed for testing.

---

## Repository layout

```
MONETRoof/
├── MonetRoof.sln                  # TwinCAT solution
├── MonetRoof/
│   ├── MonetRoof.tsproj           # TwinCAT system project (no task, no I/O)
│   ├── MONETroof/                 # PLC project (library only, no MAIN/task)
│   │   ├── MonetRoof.plcproj
│   │   ├── POUs/                  # Function blocks
│   │   │   ├── FB_RoofControl.TcPOU
│   │   │   ├── FB_Roof.TcPOU
│   │   │   ├── FB_RoofMotor.TcPOU
│   │   │   └── FB_Ramp.TcPOU
│   │   ├── VISUs/Roof.TcVIS       # TwinCAT visualization "Roof"
│   │   └── GlobalTextList.TcGTLO  # Global text list (visu texts, format strings)
└── README.md
```

---

## Hardware / EtherCAT topology

This project configures neither I/O nor a safety project any more: both came
from testing with dummy hardware and were removed. The roof I/O is linked in
the projects that use this library (MONETN, MONETS), which map the same PLC
variables in their own EtherCAT trees. The topology and the I/O mapping below
document the roof wiring as it was configured here; check the terminal numbers
against the consuming project.

**Device 2 (EK1100)** — roof drive bus:

| Terminal | Type | Function |
|---|---|---|
| Term 42 | EK1100 | EtherCAT coupler |
| Term 43 | EL2008 | 8-channel digital output (motor direction) |
| Term 44 | EL2008 | 8-channel digital output (drive reset) |
| Term 45 | EL2904 | 4-channel TwinSAFE output |
| Term 46 | EL1904 | 4-channel TwinSAFE input |
| Term 47–49 | EL1008 | 8-channel digital input (counters, limit switches, faults, buttons) |
| Term 50 | EL4004 | 4-channel analog output (motor speed setpoints) |
| Term 26 | EL9011 | End cap |

The roof motors are driven through the Device 2 terminals (digital direction
outputs, analog speed setpoints, digital inputs for inductive counters, limit
switches and drive faults). The NC configuration in the system project (axes
without I/O) is not used by the roof application.

---

## PLC application architecture

Each consuming project's `MAIN` instantiates the communication function block
and the roof control function block (call cycle depends on the consumer's
PLC task):

```
MAIN (in the consuming project, e.g. MONETN, MONETS)
├── comm        : FB_Comm_MQTT_Influx     (MQTT + Influx telemetry, BROTLib)
└── RoofControl : FB_RoofControl          (implements I_Roof)
    ├── roofs[1] : FB_Roof                (roof half 1)
    │   ├── motors[1] : FB_RoofMotor      (drive 1)
    │   └── motors[2] : FB_RoofMotor      (drive 2)
    └── roofs[2] : FB_Roof                (roof half 2)
        ├── motors[1] : FB_RoofMotor
        └── motors[2] : FB_RoofMotor
```

The consumer's `MAIN` wires the roof control parameters (speed, acceleration,
position limits, synchronisation tolerance), starts the MQTT communication
with `Roof := RoofControl` so remote roof commands are routed, and typically
mirrors the aggregated roof state (`closed`, `opened`, `stopped`, `opening`,
`closing`, `error`) into its own variables.

### FB_RoofControl

Orchestrates both roof halves and implements the `I_Roof` interface
(`Open`, `Close`, `Stop`, `Reset`, `State`).

- **Commands**: global commands (`open_roofs`, `close_roofs`, `stop_roofs`,
  `reset_roofs`) and per-roof commands (`open_roof[1..2]`,
  `close_roof[1..2]`, `stop_roof[1..2]`, `reset_roof[1..2]`).
- **Modes**: in *automatic mode* (`automatic` input set) the roof halves are
  operated from the command inputs; otherwise each roof half runs in *manual
  mode* and is operated from its local push-button inputs.
- **State aggregation**: the overall roof state is derived from the two roof
  halves — error takes precedence, followed by opened/closed, opening/closing,
  and stopped.
- **Percent open**: averaged over both roof halves.
- **Telemetry**: publishes the roof telemetry every `fTelemetryInterval`
  (default 5 s) via the communication function block.
- **Reset**: clears errors and re-enables the drives.

### FB_Roof

Controls one roof half with its two motors.

- **Velocity ramp**: the roof velocity is ramped towards the target speed with
  `FB_Ramp` (`acceleration` steps per call); `Stop()` stops immediately,
  `SoftStop()` ramps to standstill.
- **Limit slowdown**: when moving towards a travel limit, the speed is reduced
  linearly over the last `limit_slowdown` % of the travel range down to
  `min_speed`, so the roof approaches the fully open/closed position gently.
- **Limit switches**: the roof is stopped when the fully open (both motors'
  open limit switches) or fully closed position is reached. When both closed
  (or both opened) switches engage while moving in that direction, both
  drives' positions are snapped to `min_position` / `max_position` — the
  symmetric limit re-sync that clears the ±1 phase residue between the
  mechanically connected drives at every full open/close (a one-shot boot
  snap does the same on a warm restart with the roof parked at a limit).
- **Slow mode**: `slow_open` / `slow_close` override normal operation and move
  the roof at `min_speed` (e.g. for maintenance or alignment).
- **Consistency monitoring** (each roof half):
  - `sync_error` — the two drives' positions differ by `max_position_diff`
    or more (with 2, a difference of 1 is tolerated). The two motors of a
    roof half are mechanically coupled by a shaft and always driven at the
    same commanded speed, so `sync_error` is the only check that can catch
    the two drives disagreeing;
  - `limit_error` — a drive has moved `max_position_diff` counts or more
    since its own limit switch engaged, still moving towards it, without the
    other drive's switch engaging too (both are needed to stop the roof);
  - `overtravel_error` — a drive has counted more than `overtravel_margin`
    (default 10 counts) beyond `min_position` / `max_position` while moving in
    that direction, e.g. because limit switches failed. Software end stop.
    Only active while `position_valid` is set (persistent): a limit snap sets
    it, `zero_counter` clears it, and it is lost together with the counters
    (e.g. a cold reset or power loss with the roof halfway open), so the roof
    can still be moved towards the end the counters wrongly claim to be at,
    unprotected until the next limit switch is reached;
  - `stall_error` — a drive has been driven for `stall_timeout` (default
    10 s) without an accepted counter edge (jammed drive, ice, dead sensor).
    Both drives jamming together gives no position difference, so
    `sync_error` cannot catch it;
  - `drive_error` — any drive reports a fault;
  - `config_error` — invalid parameters: `max_speed` above 32767,
    `min_speed` above `max_speed`, `max_position` not above `min_position`,
    `max_position_diff` or `acceleration` not above 0. Unlike the others it
    also stops slow mode, because a wrapped speed can reverse the direction.
  Any of these triggers an immediate stop and puts the roof half into the
  error state; a reset is required to resume.
- **Events**: hint events when the roof half becomes fully open or fully
  closed; error events for synchronisation, direction, limit, over-travel, stall, drive and configuration faults
  (published to the log topic via `FB_EventLog`).

### FB_RoofMotor

Controls one drive of a roof half.

- **Position counting**: an inductive-sensor pulse train (`counter` input) is
  counted up/down according to the movement direction into a persistent
  `position` counter (survives warm restarts). `zero_counter` resets the
  counter of both motors. The counting path is filtered and gated (insurance,
  sized from measured pulses — see
  `specs/plans/2026-08-20-position-counter-fix-plan.md`):
  - `counter_debounce` (default `10 ms`) — the input must be stable high for
    at least this long before an edge counts (below the narrowest legitimate
    pulse, 20 ms);
  - `counter_min_spacing` (default `50 ms`) — minimum time between accepted
    counts (below the 600 ms rotation period);
  - **motion gate** — counts only while the drive is commanded to move
    (`real_speed <> 0`), so wind rattle at standstill cannot add counts;
  - `raw_counts` (unfiltered, ungated edge count) is exposed for
    validation/comparison.
  Counting is deliberately **not** gated on the limit switches: that would
  blind the sync check to a broken connection or a stalled drive.
- **Percent open**: derived from the position relative to
  `min_position` / `max_position`.
- **Limit switches**: the position is captured when the open/closed limit
  switch is engaged and cleared when it disengages; the
  `MovedSinceOpen` / `MovedSinceClosed` properties report the travel since the
  last limit-switch engagement.
- **Drive interface**: `speed` (absolute value of the requested speed),
  `direction_open` / `direction_close` outputs to the drive, and a
  `reset_drive` pulse (200 ms) to acknowledge drive faults.
- **Fault handling**: the drive fault input is active-low; a fault triggers a
  drive error event and (via `FB_Roof`) stops the roof half.

### FB_Ramp

Linear ramp function block: ramps an `INT` value from `start` towards `target`
in steps of `accel` per call.

---

## States and modes

Roof states follow `E_RoofState` (BROTLib):

| State | Meaning |
|---|---|
| `closed` | Roof fully closed (both limit switches engaged) |
| `opened` | Roof fully open (both limit switches engaged) |
| `opening` | Roof moving towards open |
| `closing` | Roof moving towards closed |
| `stopped` | Roof at standstill |
| `error` | Fault detected (sync / direction / limit / drive) |
| `unknown` | Initial value before the first cycle has evaluated the state |

`opened` / `closed` need both limit switches of the roof half; a half with only one switch engaged
reports `stopped`. Opened and closed engaged at the same time is not detected as a sensor fault.

Operating modes:

- **Automatic** (`automatic` input): the roof follows the command inputs
  (global and per-roof open/close/stop/reset) from the control system.
- **Manual**: the roof halves are operated directly from the local
  open/close push-button inputs.
- **Slow mode**: `slow_open` / `slow_close` move the roof at minimum speed,
  ignoring errors and other commands.

---

## I/O mapping

The roof drive signals are mapped between the PLC application and the
Device 2 terminals (per roof half `r1`/`r2` and per drive `m1`/`m2`):

**Drive signals**

| Signal | r1 m1 | r1 m2 | r2 m1 | r2 m2 |
|---|---|---|---|---|
| `speed` (analog out) | Term 50 Ch 1 | Term 50 Ch 4 | Term 50 Ch 2 | Term 50 Ch 3 |
| `direction_open` (out) | Term 43 Ch 1 | Term 43 Ch 6 | Term 43 Ch 5 | Term 43 Ch 2 |
| `direction_close` (out) | Term 43 Ch 3 | Term 43 Ch 8 | Term 43 Ch 7 | Term 43 Ch 4 |
| `reset_drive` (out) | Term 44 Ch 1 | Term 44 Ch 7 | Term 44 Ch 3 | Term 44 Ch 5 |
| `counter` (inductive sensor in) | Term 47 Ch 1 | Term 47 Ch 7 | Term 47 Ch 3 | Term 47 Ch 5 |
| `motor_error` (in) | Term 47 Ch 2 | Term 48 Ch 2 | Term 47 Ch 4 | Term 48 Ch 7 |
| `opened` (limit switch in) | Term 48 Ch 4 | Term 49 Ch 7 | Term 48 Ch 8 | Term 49 Ch 3 |
| `closed` (limit switch in) | Term 48 Ch 6 | Term 49 Ch 2 | Term 49 Ch 1 | Term 49 Ch 5 |

**Control inputs (Device 2)**

| Signal | Terminal / Channel |
|---|---|
| `automatic` (mode) | Term 47 Ch 6 |
| `open_roof[1]` (push button) | Term 47 Ch 8 |
| `open_roof[2]` (push button) | Term 48 Ch 3 |
| `close_roof[1]` (push button) | Term 48 Ch 1 |
| `close_roof[2]` (push button) | Term 48 Ch 5 |

---

## Telemetry and communication

`FB_Comm_MQTT_Influx` (BROTLib) connects to the MQTT broker and publishes
telemetry in Influx line protocol. Broker host, port, keep-alive and topics
are configured by the consuming project's `MAIN` (see e.g. MONETN's or
MONETS's `MAIN.TcPOU`), not by this repository.

For MQTT commands to reach the roof, `MAIN` must pass `Roof := RoofControl`
to `comm`, so `FB_Comm_MQTT_Influx._handleMQTTMessage` routes `dome_open` /
`dome_close` / `dome_stop` to it; otherwise they only log "MQTT not
understood".

Every 5 s the roof telemetry is published (`telescope`/`dome` measurement
domain, following the MONET dome conventions):

| Field | Meaning |
|---|---|
| `AUXILIARY.DOME.REALPOS` | Actual position: `1.0` opened, `0.0` closed, `-1.0` error, else percent open / 100 |
| `AUXILIARY.DOME.TARGETPOS` | Target position: `1.0` opening/opened, `0.0` closing/closed, percent open when stopped |
| `AUXILIARY.DOME.ERROR_STATE` | `1` in error state, else `0` |
| `AUXILIARY.DOME.READY_STATE` | `0.0` closed, `1.0` otherwise, `-1.0` in error state |
| `AUXILIARY.DOME.MOTION_STATE` | `1.0` while opening/closing, else `0.0` |
| `MONET.ROOF.STATE` | Roof status string (`OPENED`, `CLOSED`, `OPENING`, ...) |
| `MONET.ROOF.POS1` / `POS2` | Percent open of roof half 1 / 2 |
| `MONET.ROOF.STATE1` / `STATE2` | Status string of roof half 1 / 2 |
| `MONET.ROOF.ERROR1` / `ERROR2` | Error description of roof half 1 / 2 |

The MQTT command layer parses incoming `command` measurements; roof-related
commands (`dome_open`, `dome_close`, `dome_stop`) are routed to the roof
interface (`I_Roof`). Events and log messages are published to the log topic
(`MONETN/Log`) via `FB_EventLog`.

---

## Visualization

The TwinCAT visualization `Roof` (`VISUs/Roof.TcVIS`) provides an operator
view of the roof: roof halves and their positions, percent open, operating
mode, states and errors. Text resources and format strings are held in the
global text list (`GlobalTextList.TcGTLO`).

---

## Configuration

The roof control parameters (`min_speed`, `max_speed`, `acceleration`,
`max_position_1/2`, `max_position_diff`, `limit_slowdown`) are set by the
consuming project's `MAIN` when it calls `FB_RoofControl`. MONETN and MONETS
currently use `min_speed := 10000`, `max_speed := 30000`,
`acceleration := 150` and `limit_slowdown := 5`, with
`max_position_1/2 := 202/202` (MONETN) or `205/203` (MONETS) and
`max_position_diff := 3` — check each project's `MAIN.TcPOU` for the current
values.

Counting-filter inputs on `FB_RoofMotor` (function-block defaults; tunable
per installation, not currently overridden by any consumer):

| Parameter | Default | Meaning |
|---|---|---|
| `counter_debounce` | `10 ms` | Min. stable-high time of a sensor pulse before an edge is counted — keep below the narrowest legitimate pulse (20 ms) |
| `counter_min_spacing` | `50 ms` | Min. time between accepted counts — keep below the rotation period (600 ms) |

Bounding inputs on `FB_Roof` (function-block defaults, not currently
overridden by any consumer; not yet verified on the real roof):

| Parameter | Default | Meaning |
|---|---|---|
| `stall_timeout` | `10 s` | Max. time a drive is driven without an accepted count. Must exceed the rotation period (0.6 s at `max_speed`, slower at `min_speed`) and the ~3 s until the first count after a start |
| `overtravel_margin` | `10` | Counts beyond `min_position` / `max_position` before the move is stopped |

Additional function-block inputs (e.g. `min_position_1/2`,
`fTelemetryInterval`, `slow_open`, `slow_close`, `zero_counter`)
can be driven by the application as required. `ups_fail` is linked to the UPS
input in the consumer projects but is not evaluated anywhere yet.

---

## Dependencies

- **BROTLib** (library, namespace `BROT`) — roof interface (`I_Roof`),
  communication (`I_Comm`, `FB_Comm_MQTT_Influx`), event logging
  (`FB_EventLog`), roof states (`E_RoofState`).
- **VisuSymbols** — visualization symbol support.
- Beckhoff system libraries (`Tc2_Standard`, `Tc2_System`, `Tc3_Module`) and
  the TwinCAT visualization / IoT (MQTT) runtime libraries.

---

## Building and deployment

The solution is built with TwinCAT 3.1 (Build 4024) in TwinCAT XAE.
Build configurations are provided for `TwinCAT RT (x64)`, `TwinCAT RT (x86)`,
`TwinCAT CE7 (ARMV7)` and `TwinCAT OS (ARMT2)`.
