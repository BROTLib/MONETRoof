# MonetRoofTests

TcUnit tests for MONETRoof. A separate PLC project, so no test code ends up in the shipped library. It
references the *installed* MONETRoof (`MONET Roof, *`) and BROTLib (for `I_Comm` and `E_RoofState`), exactly
like MONETN or MONETS do.

## What is covered

| Suite | What it checks |
|---|---|
| `FB_Ramp_Tests` | `FB_Ramp`: single ramp steps up and down, clamping at the target without overshoot/undershoot, `accel = 0`, and a regression test for code review finding H1 (a large reversal, e.g. -30000 -> +30000, ramping through zero instead of jumping straight to the target — the INT-overflow bug fixed by computing the direction test in DINT) |
| `FB_Roof_Consistency_Tests` | `FB_Roof`'s `sync_error` (code review finding M1) and `config_error` (invalid speed/position parameters, and that it holds the roof at zero speed even mid-move); `overtravel_error`'s negative case (stays clear while `position_valid` is FALSE, its default — code review finding M7) |
| `FB_Roof_State_Tests` | The state machine (code review finding L5): the `unknown` default before the first call, `opening`/`closing` driven by `Open()`/`Close()`, `stopped` after `SoftStop()`, and `Reset()` recovering from a cleared error |

18 test cases in total.

### Deliberately not covered

- **`opened`/`closed` roof states**, and anything that needs `roof_limit_open`/`roof_limit_closed`. Those come
  from `motors[i].opened`/`.closed`, which are `AT%I*` (hardware-linked) on `FB_RoofMotor`, and this project
  configures no I/O (same as `MonetRoof.tsproj` itself, since the removal of the dummy hardware). Validate those
  on the real roof instead — this is exactly the plan's still-open step 5 (`raw_counts` vs `position` over a
  full cycle).
- **`limit_error`'s positive case** (a drive that overran its own switch). It needs `position_open`/
  `position_close` set to something other than their -1000 default, and those are only ever set by the
  `opened`/`closed` edge — `AT%I*`, same limitation. There is no public setter, and TwinCAT does not allow
  writing a `VAR_OUTPUT` from outside the instance that owns it (see "Things to know" below), so this can't be
  faked either.
- **`overtravel_error`'s positive case** (needs `position_valid = TRUE`), for the same reason: nothing external
  can set it, only the internal limit-snap logic can, which needs the same switches.
- **`stall_error`**, which needs a `TON` to time out (`stall_timeout`, default 10 s) — cheap to add as a
  multi-cycle test (see `Timing_Follows_Mode`-style tests in `BROTLibTests`) but not done yet.
- **`drive_error`**, which needs `motor_error` (also `AT%I*`).
- Anything about the counting/debounce path itself (`FB_RoofMotor`'s `counter`, `counter_debounce`,
  `counter_min_spacing`) — that is the `counter` input, `AT%I*`, same limitation. `testing/check_debounce_aliasing.py`
  in the parent repo already models that path offline, in Python, against the same question (code review
  finding M4).

`FB_Roof_Consistency_Tests` and `FB_Roof_State_Tests` sidestep the `AT%I*` inputs for what they can:
`FB_RoofMotor.SetPosition()` is a public **method**, called on `FB_Roof`'s private `motors` array element, and
that works (it is how `FB_Roof` itself sets a position on a limit snap) — but that's as far as it goes. See
"Things to know" for why, and where else the same wall shows up.

## Running the tests

TcBuild only compiles. Running needs a TwinCAT runtime that executes the PLC, plus a (trial) license for it.

**Windows 11 note.** The TwinCAT 3.1 Build 4024 *real-time* runtime does not run on Windows 11
([Beckhoff system requirements](https://infosys.beckhoff.com/content/1033/tc3_overview/6162419083.html)); Run mode
fails with `Init4\RTime: Start Interrupt: Ticker started >> AdsError: 6 (port 200)`. XAE and TcBuild are fine.
Use the beta **user-mode runtime** that ships with TwinCAT instead (`C:\TwinCAT\3.1\Runtimes\UmRT_Default`, see
the `Readme.txt` there for its terms of use). It has no real-time guarantees.

One-time setup on a machine:

1. Install TcUnit into the local library repository. This starts a hidden XAE instance:
   ```powershell
   .\MonetRoofTests\tools\Install-TcUnit.ps1
   ```
2. Make sure the current MONETRoof (and BROTLib) is installed too (the tests use the *installed* copy, not the
   source tree):
   ```powershell
   & "C:\Program Files\Industrial Brains B.V\TcBuild\TcBuild.exe" install ..\MonetRoof.sln -x MONETroof -p MonetRoof -l MonetRoof.library
   ```

Every run:

1. Start the user-mode runtime **from its own folder** (`Start.bat` uses the current directory for its config):
   ```powershell
   cd C:\TwinCAT\3.1\Runtimes\UmRT_Default; .\Start.bat
   ```
2. Build, deploy and run. Exit code 0 = all passed, 1 = a test failed, 2 = no result within the timeout:
   ```powershell
   & "C:\Program Files\Industrial Brains B.V\TcBuild\TcBuild.exe" build MonetRoofTests.sln
   .\MonetRoofTests\tools\Run-Tests.ps1
   ```
   It overwrites whatever boot project is on the target runtime (default `192.168.4.1.1.1`, override with
   `-TargetNetId`). The counters are printed; the individual failing assertions are in the TwinCAT ADS log
   (XAE error list) when you open the project in XAE and connect to the target.

To debug interactively instead: open `MonetRoofTests.sln` in XAE, choose the user-mode runtime as target,
activate the configuration, log in and start the PLC. TcUnit prints every result to the error list.

## Things to know

- **Trial license.** The PLC trial license lasts 7 days and is renewed by hand (captcha). This is the open point
  for running this unattended in CI, same as `BROTLibTests`.
- **TcUnit sizing.** TcUnit's defaults (1000 suites x 100 tests x 1000 assertions) allocate about 78 MB of PLC
  data, which the user-mode runtime cannot start. The project overrides them to 32 / 32 / 256 in the
  `TcUnit` reference (`Parameters` in `MonetRoofTests.plcproj`).
- **Every test method is called every PLC cycle.** `FB_Ramp_Tests` and the consistency/state tests all finish
  within a single call each (no `TON`s involved), so this doesn't matter for them yet, but a future
  `stall_error` test (see above) would need to guard its own state like `BROTLibTests`'s timing test does.
- **`FB_NullComm`.** `FB_Roof`/`FB_RoofMotor` take a `BROTLib.I_Comm` and call `Publish`/`PublishLog` on it
  (via `FB_EventLog`) on error/state edges. An unassigned (null) interface reference there would fault the PLC
  the first time an edge fires, so every `FB_Roof` test instance is given an `FB_NullComm` (a no-op
  implementation with call counters, in case a future test wants to assert an event fired) rather than a real
  MQTT connection.
- **Only `VAR_INPUT` is writable from outside an instance.** Learned the hard way while writing these: a
  `VAR_OUTPUT` (even with no `AT` clause) is read-only from outside in TwinCAT ST — `fbRoof.position_valid :=
  TRUE;` from a test fails to compile ("'position_valid' is no input of 'FB_Roof'"), same for `position_open`/
  `position_close`. A plain `VAR` (no `AT`/`INPUT`/`OUTPUT`, like `FB_Roof`'s private `motors` array) can't be
  reached to read or write a field at all — but calling a public **method** on one of its elements (like
  `motors[i].SetPosition(x)`) is fine, since that's not a field access. This is what actually bounds
  `FB_Roof_Consistency_Tests`/`FB_Roof_State_Tests` — see "Deliberately not covered" above for where it bites.
- **The build prints a `System_VisuElemEventTable` Library Manager error and exits 2, but the PLC part still
  compiles ("0 errors, 0 warnings: ready for download!") and produces real `_Boot`/`.tmc` output.** Referencing
  the "MONET Roof" library pulls in its own Visualization Manager's dependencies (`Roof.TcVIS`), which this
  project doesn't declare and doesn't need. Confirmed harmless by checking the build artifacts exist, not
  something fixed here.
- `.tmc`, `_Boot/`, `_CompileInfo/` and `_Libraries/` are generated and ignored by git.
