"""Line-by-line port of FB_Ramp, run under two assumptions about INT arithmetic.

FB_Ramp computes `target - value` on two INTs. If TwinCAT truncates that subtraction to 16 bit
(wrap16=True), a reversal at more than about 2767 counts of speed overflows and the ramp snaps to the
new target in one cycle. If TwinCAT uses wider intermediates (wrap16=False), the ramp is fine.
This script cannot tell which one the compiler does. Check on the PLC (see review finding H1).

Run: python3 check_ramp_overflow.py
"""


def w16(x):
    x &= 0xFFFF
    return x - 0x10000 if x & 0x8000 else x


def ramp(start, target, accel, wrap16):
    f = w16 if wrap16 else (lambda x: x)
    value = start
    if f(target - value) > 0:
        value = w16(value + accel)
        if value >= target:
            value = target
    else:
        value = w16(value - accel)
        if value <= target:
            value = target
    return value


def run(start, target, accel=150, wrap16=True, cycles=6):
    v, out = start, [start]
    for _ in range(cycles):
        v = ramp(v, target, accel, wrap16)
        out.append(v)
    return out


CASES = [
    ("closing at -30000, then open", -30000, 30000),
    ("opening at +30000, then close", 30000, -30000),
    ("closing at -3000, then open", -3000, 30000),
    ("closing at -2000, then open", -2000, 30000),
    ("stopped, then open", 0, 30000),
]

if __name__ == "__main__":
    for label, start, target in CASES:
        for wrap16 in (True, False):
            print(f"{label:32s} wrap16={wrap16!s:5}", run(start, target, wrap16=wrap16))
        print()
    print("Overflow needs |target - value| > 32767, i.e. a reversal while moving faster than",
          32767 - 30000, "counts (target +-30000).")
