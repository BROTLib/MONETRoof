# MONET Roof

TwinCAT 3 control application for the roof of the MONET telescopes
(Georg-August-Universität Göttingen).

The roof consists of two independently driven roof halves, each moved by two
brushed DC motors. The application provides automatic and manual roof control,
position tracking via inductive-sensor counters and limit switches, velocity
ramping with slowdown near the travel limits, monitoring of the two drives of
a roof half (synchronisation, direction, travel limits, drive faults), MQTT
telemetry and logging, and a TwinSAFE safety concept with emergency-stop and
external device monitoring.

The application is built on the **BROTLib** library (`I_Roof`, `I_Comm`,
`FB_Comm_MQTT_Influx`, `FB_EventLog`, `E_RoofState`, ...).

---

## Repository layout

```
MONETRoof/
├── MonetRoof.sln                  # TwinCAT solution
├── MonetRoof/
│   ├── MonetRoof.tsproj           # TwinCAT system project (I/O, NC, tasks, mappings)
│   ├── MONETroof/                 # PLC project
│   │   ├── MonetRoof.plcproj
│   │   ├── PlcTask.TcTTO          # PLC task (10 ms, priority 20, calls MAIN)
│   │   ├── POUs/                  # Program and function blocks
│   │   │   ├── MAIN.TcPOU
│   │   │   ├── FB_RoofControl.TcPOU
│   │   │   ├── FB_Roof.TcPOU
│   │   │   ├── FB_RoofMotor.TcPOU
│   │   │   └── FB_Ramp.TcPOU
│   │   ├── VISUs/Roof.TcVIS       # TwinCAT visualization "Roof"
│   │   ├── GlobalTextList.TcGTLO  # Global text list (visu texts, format strings)
│   │   └── _Libraries/            # Resolved library references
│   ├── TwinSAFE/                  # Safety project (TwinSAFE group on EL6910)
│   │   └── TwinSafeGroup1/        # Safety logic + alias devices
│   └── _Boot/                     # Boot project for TwinCAT RT (x64)
└── README.md
```

---

## Hardware / EtherCAT topology

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
switches and drive faults). The NC task additionally provides axes (e.g.
Axis 9 / Axis 10 mapped to the two channels of the EL7342 DC motor terminal)
for further motion applications.

---

## PLC application architecture

The PLC task (`PlcTask`, 10 ms, priority 20) calls `MAIN`, which instantiates
the communication function block and the roof control function block:

```
MAIN
├── comm        : FB_Comm_MQTT_Influx     (MQTT + Influx telemetry, BROTLib)
└── RoofControl : FB_RoofControl          (implements I_Roof)
    ├── roofs[1] : FB_Roof                (roof half 1)
    │   ├── motors[1] : FB_RoofMotor      (drive 1)
    │   └── motors[2] : FB_RoofMotor      (drive 2)
    └── roofs[2] : FB_Roof                (roof half 2)
        ├── motors[1] : FB_RoofMotor
        └── motors[2] : FB_RoofMotor
```

### MAIN

`MAIN` wires the roof control parameters (speed, acceleration, position
limits, synchronisation tolerance), starts the MQTT communication, and mirrors
the aggregated roof state (`closed`, `opened`, `stopped`, `opening`, `closing`,
`error`) into dedicated boolean outputs. It also provides the safety
handshake outputs `running`, `restart` and `errack` for the TwinSAFE group.

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
  - `sync_error` — the two drives' positions differ by more than
    `max_position_diff`;
  - `direction_error` — both drives move at the same time in opposite
    directions;
  - `limit_error` — a drive has moved more than `max_position_diff` since
    leaving its limit switch without the opposite limit switch being reached;
  - `drive_error` — any drive reports a fault.
  Any of these triggers an immediate stop and puts the roof half into the
  error state; a reset is required to resume.
- **Events**: hint events when the roof half becomes fully open or fully
  closed; error events for synchronisation, direction, limit and drive faults
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
| `unknown` | Undefined |

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
telemetry in Influx line protocol.

Configured in `MAIN`:

| Parameter | Value |
|---|---|
| Broker host | `10.129.129.76` |
| Port | `1883` |
| Keep-alive | `60 s` |
| Subscribe topic | `MONETN` |
| Publish topic (telemetry) | `MONETN/Telemetry` |
| Log topic | `MONETN/Log` |

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

## Safety (TwinSAFE)

A TwinSAFE safety application runs on the EL6910 safety PLC (FSoE network
with EL1904 safety inputs and EL2904 safety outputs):

- **Emergency stop**: `FBEstop1` (`safeEstop`) monitors the emergency-stop
  chain with configurable input filtering and a restart delay; the E-stop
  output drives the safety relay output (EL2904).
- **External device monitoring**: `FBEdm1` (`safeEdm`) monitors the
  contactor/feedback contacts of the switched load with switch-on and
  switch-off monitoring times.
- **Group ports**: the safety group exposes `Restart`, `RunStop` and
  `ErrAck` (error acknowledge) ports as standard alias devices for the
  controller, plus status ports (`FbErr`, `ComErr`, `OutErr`, `OtherErr`,
  `ModuleFault`, `ComStartup`, `FbDeactive`, `FbRun`, `InRun`).
- The PLC application provides the corresponding handshake outputs
  (`running`, `restart`, `errack`).

---

## Visualization

The TwinCAT visualization `Roof` (`VISUs/Roof.TcVIS`) provides an operator
view of the roof: roof halves and their positions, percent open, operating
mode, states and errors. Text resources and format strings are held in the
global text list (`GlobalTextList.TcGTLO`).

---

## Configuration

The roof control parameters are configured in `MAIN`:

| Parameter | Value | Meaning |
|---|---|---|
| `min_speed` | `10000` | Minimum drive speed |
| `max_speed` | `30000` | Maximum drive speed |
| `acceleration` | `150` | Acceleration [speed/call] |
| `max_position_1` | `200` | Maximum position of roof half 1 |
| `max_position_2` | `200` | Maximum position of roof half 2 |
| `max_position_diff` | `2` | Maximum allowed position difference between the two drives of a roof half |
| `limit_slowdown` | `5` | Linear slowdown within the last 5 % of the travel range near the limits |

Counting-filter inputs on `FB_RoofMotor` (function-block defaults; tunable
per installation, currently not overridden by `MAIN`):

| Parameter | Default | Meaning |
|---|---|---|
| `counter_debounce` | `10 ms` | Min. stable-high time of a sensor pulse before an edge is counted — keep below the narrowest legitimate pulse (20 ms) |
| `counter_min_spacing` | `50 ms` | Min. time between accepted counts — keep below the rotation period (600 ms) |

Additional function-block inputs (e.g. `min_position_1/2`,
`fTelemetryInterval`, `slow_open`, `slow_close`, `zero_counter`, `ups_fail`)
can be driven by the application as required.

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
`TwinCAT CE7 (ARMV7)` and `TwinCAT OS (ARMT2)`. A boot project for
`TwinCAT RT (x64)` is included under `_Boot`, so the roof controller can boot
directly into the application.
