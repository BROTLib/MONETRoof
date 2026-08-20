#!/usr/bin/env python3
"""Decode TwinCAT ScopeView .svdx recordings (reverse-engineered format).

Layout (see BROTLib/specs/design/twincat-scopeview-svdx-format.md):
- u64 @0 payload size; u32 @16 channel count; u32 @20 first block offset
- block table @28: per channel [u64 block size][u32 channel idx][u64 block end]
- per channel block: sub-header, preview series, then 92/108-byte segments
  holding 16 samples each at BaseSampleTime (100000 ticks = 10 ms):
  [u64 ts0][u64 16][val0 (1 or 2 bytes)][15 x (u32 delta_ticks, val)]
- embedded ScopeProject XML at 40 + payload_size provides names/types

Usage: python3 decode_svdx.py <file.svdx> [out.csv] [out.png]
"""
import re
import struct
import sys

NAMES_DEFAULT = ['counter[1,1]', 'counter[1,2]', 'counter[2,1]', 'counter[2,2]',
                 'opened[1,1]', 'opened[1,2]', 'opened[2,1]', 'opened[2,2]',
                 'position[1,1]', 'closed[1,1]', 'closed[1,2]', 'position[1,2]',
                 'position[2,1]', 'closed[2,1]', 'position[2,2]', 'closed[2,2]']


def read_header(d):
    payload = struct.unpack('<Q', d[0:8])[0]
    nch = struct.unpack('<I', d[16:20])[0]
    data_start = struct.unpack('<I', d[20:24])[0]
    ends = [struct.unpack('<Q', d[28 + i * 20 + 12: 28 + i * 20 + 20])[0]
            for i in range(nch)]
    return payload, nch, data_start, ends


def channel_meta(d, payload):
    xml = d[d.find(b'<ScopeProject'):].decode('utf-8', errors='replace')
    acqs = re.findall(r'<AdsAcquisition[^>]*>(.*?)</AdsAcquisition>', xml, re.S)
    meta = []
    for a in acqs:
        name = (re.search(r'<Name>(.*?)</Name>', a) or [None, '?'])[1]
        dtype = (re.search(r'<DataType>(.*?)</DataType>', a) or [None, '?'])[1]
        fh = (re.search(r'<FileHandle>(\d+)</FileHandle>', a) or [None, '0'])[1]
        meta.append((int(fh), name, dtype))
    meta.sort()
    return meta


def val_size(dtype):
    return 2 if dtype in ('INT16', 'INT', 'UINT', 'WORD') else 1


def decode_block(blk, vsize):
    """Return list of (timestamp_ticks, value)."""
    start_ts = struct.unpack('<Q', blk[11:19])[0]
    seg = 8 + 8 + vsize + 15 * (4 + vsize)  # bytes per segment
    # find first segment: u64==16 preceded by ts-like u64, followed by plausible value
    seg_start = None
    i = 100
    while i + 8 + vsize + 5 < len(blk):
        if struct.unpack('<Q', blk[i:i + 8])[0] == 16:
            ts = struct.unpack('<Q', blk[i - 8:i])[0]
            if start_ts <= ts < start_ts + 72000000000:
                seg_start = i - 8
                break
        i += 1
    if seg_start is None:
        return []
    out = []
    i = seg_start
    while i + seg <= len(blk):
        ts0 = struct.unpack('<Q', blk[i:i + 8])[0]
        if not (start_ts <= ts0 < start_ts + 72000000000):
            break
        n = struct.unpack('<Q', blk[i + 8:i + 16])[0]
        out.append((ts0, int.from_bytes(blk[i + 16:i + 16 + vsize], 'little', signed=(vsize == 2))))
        pos = i + 16 + vsize
        for _ in range(1, min(n, 16)):
            delta = struct.unpack('<I', blk[pos:pos + 4])[0]
            out.append((ts0 + delta,
                        int.from_bytes(blk[pos + 4:pos + 4 + vsize], 'little', signed=(vsize == 2))))
            pos += 4 + vsize
        i += seg
    return out


def decode_file(path):
    d = open(path, 'rb').read()
    payload, nch, data_start, ends = read_header(d)
    meta = channel_meta(d, payload)
    names = [m[1] for m in meta] or NAMES_DEFAULT[:nch]
    sizes = [val_size(m[2]) for m in meta] if meta else [1] * nch
    starts = [data_start] + ends[:-1]
    chans = []
    for k in range(nch):
        blk = d[starts[k]:ends[k]]
        recs = decode_block(blk, sizes[k])
        t0 = recs[0][0] if recs else 0
        chans.append((names[k], [(ts - t0) / 1e7 for ts, _ in recs], [v for _, v in recs]))
    return chans, names


def to_csv(chans, out):
    import csv as _csv
    n = len(chans[0][1])
    with open(out, 'w', newline='') as f:
        w = _csv.writer(f)
        w.writerow(['t_s'] + [c[0] for c in chans])
        for k in range(n):
            w.writerow([f'{chans[0][1][k]:.4f}'] + [c[2][k] for c in chans])
    print(f'wrote {out} ({n} samples, {len(chans)} channels)')


def plot(chans, title, out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    n = len(chans)
    fig, axes = plt.subplots(n, 1, figsize=(14, max(6, n * 1.3)), sharex=True)
    if n == 1:
        axes = [axes]
    for ax, (name, times, vals) in zip(axes, chans):
        ax.plot(times, vals, lw=0.9)
        ax.set_ylabel(name, fontsize=8)
        ax.grid(alpha=0.3)
    axes[-1].set_xlabel('time (s)')
    axes[0].set_title(title)
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    print('wrote', out)


if __name__ == '__main__':
    path = sys.argv[1]
    chans, names = decode_file(path)
    base = path.rsplit('/', 1)[-1].rsplit('.', 1)[0]
    if len(sys.argv) > 2:
        to_csv(chans, sys.argv[2])
    else:
        to_csv(chans, f'{base}_decoded.csv')
    if len(sys.argv) > 3:
        plot(chans, f'{path} — {len(chans)} channels @ 10 ms', sys.argv[3])
