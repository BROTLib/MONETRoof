"""Model of the FB_RoofMotor level debounce (TON, PT = 10 ms) on a 10 ms PLC task.

A pulse of true width w is sampled every 10 ms at a random phase. Q is set when the input has been
high for at least PT at a sample instant, so with PT equal to the cycle time the filter needs two
consecutive high samples and the comparison `ET >= PT` sits exactly on its boundary. Timing is in
integer microseconds to avoid float-compare artifacts. "jitter" is Gaussian noise on each cycle
interval (an assumption, not a measurement of the target).

Why it matters: the ScopeView recordings also sample at 10 ms, so a pulse read as "20 ms" (two high
samples) has a true width between 10 and 30 ms (see review finding M4).

Run: python3 check_debounce_aliasing.py
"""
import random

T_US = 10_000   # task cycle
PT_US = 10_000  # counter_debounce
N = 20_000


def counted(width_us, jitter_us, rng):
    t = rng.randint(0, T_US - 1)  # time of first sample after the rising edge
    first = None
    while t < width_us:
        if first is None:
            first = t
        elif t - first >= PT_US:
            return True
        t += T_US + round(rng.gauss(0, jitter_us))
    return False


if __name__ == "__main__":
    rng = random.Random(1)
    for jitter_us in (0, 50):
        print(f"cycle jitter sigma {jitter_us} us")
        for w_ms in (8, 10, 12, 15, 18, 20, 22, 25, 30, 40):
            p = sum(counted(w_ms * 1000, jitter_us, rng) for _ in range(N)) / N
            print(f"  true width {w_ms:>3} ms -> counted {p * 100:5.1f} %")
