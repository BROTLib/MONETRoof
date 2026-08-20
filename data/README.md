# MONET roof recording data

Decoded TwinCAT ScopeView recordings of the MONET roof drive signals (Becky),
together with the decoder script. Working/analysis data for the position
counter investigation — see
`specs/plans/2026-08-20-position-counter-fix-plan.md`.

## Recordings (binary, as recorded on the controller)

| File | Content |
|---|---|
| `roof.tcscopex` | ScopeView project: channel definitions (16 channels), ADS addresses, sampling config |
| `Roof_counter_orig.svdx` | *(not preserved as binary — overwritten on the controller)* — the original clean 20.47 s move, 8 channels (counters + `opened`). Decoded data kept in `roof_counter_decoded.csv` |
| `Roof2.svdx` | Reproduced failure #1: 12.47 s, 8 channels — half 1 stalled at move start, c[1,1] block dwelling in the sensor zone |
| `Roof_new16ch.svdx` | 16-channel open/stop/close test (positions + `closed` added): 70.4 s, two cycles, no error — shows the expected ±1 phase difference |
| `Roof2_error16ch.svdx` | **The error event**: 127.4 s, 16 channels — `sync_error` present from t = 3.39 s; no pulse over-counting, error is un-cleared ±1 residue + phase oscillation |

## Decoded data (CSV, 10 ms samples, one row per sample)

| File | Source | Samples |
|---|---|---|
| `roof_counter_decoded.csv` | original clean move (binary overwritten) | 2048 |
| `roof2_decoded.csv` | `Roof2.svdx` | 1248 |
| `roof_new16ch_decoded.csv` | `Roof_new16ch.svdx` | 7040 |
| `roof2_error16ch_decoded.csv` | `Roof2_error16ch.svdx` | 12736 |

Plots: `roof_counter_plot.png`, `roof2_plot.png`, `roof_new16ch_plot.png`.

## The decoder

`decode_svdx.py` decodes the (reverse-engineered) TwinCAT ScopeView `.svdx`
binary format:

- Header: u64 payload size at offset 0, u32 channel count at offset 16,
  u32 first-block offset at offset 20, and a per-channel block table at
  offset 28 (block size, channel index, block end).
- Per channel: a sub-header, preview series, then 92/108-byte segments with
  16 samples each at a 10 ms base sample time (`[u64 ts0][u64 16][val]`
  followed by 15 × `(u32 delta_ticks, val)`).
- An embedded `ScopeProject` XML (at `40 + payload_size`) provides the channel
  names and data types.
- Full format documented in
  `BROTLib/specs/design/twincat-scopeview-svdx-format.md`.

```bash
python3 decode_svdx.py <file.svdx> [out.csv] [out.png]
```

## Caveat

`Roof2.svdx`, `Roof_new16ch.svdx` and `Roof2_error16ch.svdx` are the preserved
originals of files that were later overwritten on the controller
(`/home/husser/sync/Becky/`) — the copies here are the authoritative record.
