#!/usr/bin/env python3
"""Phase 0 design-review calculations (clock plan, sampling error, decimation filters).

This is review evidence, not product code. It runs on any laptop:

    python3 -m venv .venv && .venv/bin/pip install numpy scipy matplotlib
    .venv/bin/python docs/phase0/analysis/phase0_analysis.py

It prints a text report and writes figures to docs/phase0/figures/.
Nothing here touches hardware. The sigma-delta modulator below is a generic
2nd-order stand-in: the SPH0641LU4H-1 datasheet does not publish the mic's
modulator, so modulator-dependent numbers are illustrative only.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np
from scipy import signal

FIG_DIR = Path(__file__).resolve().parents[1] / "figures"

# ---- Datasheet limits used below (document, section/page) -------------------
XOSC_HZ = 12_000_000  # RP2350 datasheet 8.6.3 / 8.6.4: 12 MHz crystal, SDK default
VCO_MIN, VCO_MAX = 750e6, 1600e6  # RP2350 datasheet 8.6.3
FBDIV_RANGE = range(16, 321)  # RP2350 datasheet 8.6.3
POSTDIV_RANGE = range(1, 8)  # RP2350 datasheet 8.6.3
SYS_MAX_HZ = 150e6  # RP2350 datasheet 8.6.3
MIC_STD = (1.024e6, 2.475e6)  # SPH0641LU4H-1 Rev B, sheet 2
MIC_ULTRA = (3.072e6, 4.8e6)  # SPH0641LU4H-1 Rev B, sheet 2
MIC_FS_DBSPL = 120.0  # 94 dB SPL = -26 dBFS (sheet 3) -> 0 dBFS = 120 dB SPL
MIC_SNR_DBA = 64.3  # sheet 3


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# ---- 1. PLL and clock plan ---------------------------------------------------
def pll_solutions(f_out_hz: float):
    """All exact (fbdiv, pd1, pd2) for f_out from the 12 MHz crystal, REFDIV = 1."""
    out = []
    for fbdiv in FBDIV_RANGE:
        vco = XOSC_HZ * fbdiv
        if not VCO_MIN <= vco <= VCO_MAX:
            continue
        for pd1 in POSTDIV_RANGE:
            for pd2 in POSTDIV_RANGE:
                if pd2 > pd1:
                    continue  # datasheet tip: larger value on POSTDIV1
                if vco == f_out_hz * pd1 * pd2:
                    out.append((vco, fbdiv, pd1, pd2))
    return sorted(out, reverse=True)  # highest VCO first = lowest jitter (8.6.3.1)


def pdm_clocks(sys_hz: float, band):
    """PDM clocks = sys / n with n even (exact 50 % duty from an integer PIO divider)."""
    res = []
    for n in range(2, 400, 2):
        f = sys_hz / n
        if band[0] <= f <= band[1]:
            res.append((n, f))
    return res


def clock_plan() -> None:
    section("1. RP2350 clock plan (integer dividers only)")
    print("f_pdm = f_sys / n, n even -> PIO clkdiv = n/2 (integer), 2 PIO cycles per bit.")
    for sys_mhz in (96, 120, 128, 144, 150):
        sys_hz = sys_mhz * 1e6
        sols = pll_solutions(sys_hz)
        if not sols:
            print(f"\nf_sys {sys_mhz} MHz: no exact PLL solution")
            continue
        vco, fbdiv, pd1, pd2 = sols[0]
        print(f"\nf_sys {sys_mhz} MHz: 12 MHz x {fbdiv} = {vco / 1e6:.0f} MHz VCO / {pd1} / {pd2}")
        ultra = pdm_clocks(sys_hz, MIC_ULTRA)
        std = [(n, f) for n, f in pdm_clocks(sys_hz, MIC_STD) if f >= 1.9e6]
        print("  ultrasonic-mode clocks: " + ", ".join(f"/{n}={f / 1e6:.4g} MHz" for n, f in ultra))
        print("  standard-mode clocks (>= 1.9 MHz shown): " + ", ".join(f"/{n}={f / 1e6:.4g} MHz" for n, f in std))
        for n, f in ultra:
            rates = [(d, f / d) for d in (16, 20, 24, 32, 40, 48) if abs(f / d - round(f / d)) < 1e-6
                     and (f / d) in (100e3, 200e3)]
            if rates:
                print(f"    {f / 1e6:.4g} MHz -> " + ", ".join(f"/{d} = {r / 1e3:.0f} kHz" for d, r in rates))

    print("\nChosen: f_sys = 128 MHz (12 MHz x 128 = 1536 MHz VCO, POSTDIV1 = 6, POSTDIV2 = 2)")
    for name, n in (("wake-up (standard mode)", 64), ("ultrasonic", 40)):
        f = 128e6 / n
        print(f"  {name}: 128 MHz / {n} = {f / 1e6:.3f} MHz, clkdiv = {n // 2}, "
              f"high = low = {n // 2} sys cycles = {n / 2 / 128e6 * 1e9:.2f} ns")
    f_pdm = 3.2e6
    print(f"  tach sampler: 3.2 MHz / 16 = {f_pdm / 16 / 1e3:.0f} kHz, clkdiv = {40 * 16} (1 PIO cycle per sample)")
    for ppm in (30, 50):
        print(f"  crystal error {ppm} ppm -> f_pdm error {f_pdm * ppm * 1e-6:.0f} Hz "
              f"(still inside {MIC_ULTRA[0] / 1e6}-{MIC_ULTRA[1] / 1e6} MHz)")
    # Mic data timing at 3.2 MHz with SELECT = GND (data asserted after falling edge)
    t_half = 0.5 / f_pdm * 1e9
    t_sys = 1e9 / 128e6
    t_dd_max, t_dz_min = 40.0, 3.0  # ns, SPH0641LU4H-1 sheet 4
    sample_before_rise = 2 * t_sys  # 2-flipflop synchroniser, RP2350 datasheet 11.5.6.3
    print(f"  half period {t_half:.2f} ns; PIO samples the pin {sample_before_rise:.2f} ns before the rising edge")
    print(f"  setup margin = {t_half:.2f} - {sample_before_rise:.2f} - tDD(max) {t_dd_max:.0f} = "
          f"{t_half - sample_before_rise - t_dd_max:.1f} ns")
    print(f"  hold margin  = {sample_before_rise:.2f} + tDZ(min) {t_dz_min:.0f} = "
          f"{sample_before_rise + t_dz_min:.1f} ns (pad and cable delay not included)")


# ---- 2. Peak under-read from sampling ---------------------------------------
def peak_error(fs: float, f: float) -> float:
    """Worst-case under-read of a sine peak when sampled at fs: 1 - cos(pi / N)."""
    return 1.0 - math.cos(math.pi * f / fs)


def peak_error_table() -> None:
    section("2. Worst-case sine peak under-read, 1 - cos(pi f / fs)")
    freqs = (10e3, 20e3, 23e3, 25e3, 40e3)
    print("fs (kHz)   " + "".join(f"{f / 1e3:>9.0f}k" for f in freqs))
    for fs in (100e3, 200e3, 400e3, 800e3):
        row = "".join(f"{100 * peak_error(fs, f):>9.2f}%" for f in freqs)
        note = {400e3: "  (= 100 kHz data interpolated x4, or 200 kHz x2)",
                800e3: "  (= 200 kHz data interpolated x4)"}.get(fs, "")
        print(f"{fs / 1e3:>7.0f}   {row}{note}")
    need = math.pi / math.acos(0.75)
    print(f"\n-25 % limit needs N >= {need:.3f} samples per cycle:")
    for f in (23e3, 25e3):
        print(f"  {f / 1e3:.0f} kHz -> fs >= {need * f / 1e3:.1f} kHz")


# ---- 3. Decimation filters ---------------------------------------------------
def design(fs_in: float, f_pass: float, f_stop: float, atten_db: float) -> np.ndarray:
    """Linear-phase Kaiser low-pass, -6 dB point midway between f_pass and f_stop."""
    width = (f_stop - f_pass) / (fs_in / 2)
    numtaps, beta = signal.kaiserord(atten_db, width)
    numtaps |= 1  # odd length -> integer group delay
    h = signal.firwin(numtaps, (f_pass + f_stop) / 2, window=("kaiser", beta), fs=fs_in)
    return h / h.sum()


def gain_db(h: np.ndarray, fs: float, freqs) -> np.ndarray:
    w = 2 * np.pi * np.asarray(freqs, dtype=float) / fs
    _, resp = signal.freqz(h, worN=w)
    return 20 * np.log10(np.maximum(np.abs(resp), 1e-12))


def worst_stopband_db(h: np.ndarray, fs: float, f_stop: float) -> float:
    f = np.linspace(f_stop, fs / 2, 20001)
    return float(gain_db(h, fs, f).max())


def step_overshoot(h: np.ndarray) -> float:
    s = np.cumsum(h)
    return float(s.max() - 1.0)


FS_PDM = 3.2e6
OPTIONS = {
    # name: (decimation, passband edge, stopband edge, stopband attenuation dB)
    "A: /16 -> 200 kHz (alias-free to 80 kHz)": (16, 40e3, 120e3, 100.0),
    "B: /32 -> 100 kHz (alias-free to 40 kHz)": (32, 40e3, 60e3, 100.0),
}


def filter_report():
    section("3. Decimation filter options from a 3.2 MHz 1-bit stream")
    designs = {}
    for name, (dec, fp, fstop, att) in OPTIONS.items():
        h = design(FS_PDM, fp, fstop, att)
        fs_out = FS_PDM / dec
        designs[name] = (h, dec)
        print(f"\n{name}")
        print(f"  taps {len(h)} at 3.2 MHz = {math.ceil(len(h) / dec)} multiplies per output sample; "
              f"group delay {(len(h) - 1) / 2 / FS_PDM * 1e6:.1f} us; span {len(h) / FS_PDM * 1e6:.0f} us")
        g = gain_db(h, FS_PDM, [1e3, 20e3, 23e3, 25e3, 40e3])
        print("  gain at 1/20/23/25/40 kHz: " + ", ".join(f"{x:+.4f} dB" for x in g))
        print(f"  worst stopband gain from {fstop / 1e3:.0f} kHz up: {worst_stopband_db(h, FS_PDM, fstop):.1f} dB")
        print(f"  first alias band into 0-{fp / 1e3:.0f} kHz starts at {(fs_out - fp) / 1e3:.0f} kHz "
              f"(gain there {gain_db(h, FS_PDM, [fs_out - fp])[0]:.1f} dB)")
        print(f"  step-response overshoot: {100 * step_overshoot(h):.1f} %")
    noise_floor_dbfs = -26.0 - MIC_SNR_DBA
    print(f"\nAlias bound that does not depend on the mic's modulator: a 1-bit stream has total power")
    print(f"0 dBFS, so everything folded back is below the stopband gain (about -100 dBFS).")
    print(f"Mic noise floor (A-weighted, audio band): -26 dBFS - {MIC_SNR_DBA} dB = {noise_floor_dbfs:.1f} dBFS.")
    return designs


# ---- 4. Simulation with a generic sigma-delta stream -------------------------
def sigma_delta_2nd(x: np.ndarray) -> np.ndarray:
    """Generic 2nd-order 1-bit modulator (stand-in; the mic's own is not published)."""
    y = np.empty(len(x), dtype=np.int8)
    i1 = i2 = 0.0
    for n, xn in enumerate(x):
        v = 1.0 if i2 >= 0.0 else -1.0
        y[n] = 1 if v > 0 else 0
        i1 += xn - v
        i2 += i1 - v
    return y


def decimate(bits: np.ndarray, h: np.ndarray, dec: int) -> np.ndarray:
    x = bits.astype(np.float32) * 2.0 - 1.0
    return signal.upfirdn(h.astype(np.float32), x, up=1, down=dec)


def cycle_peaks(x: np.ndarray, fs: float, f: float) -> np.ndarray:
    n = int(round(fs / f))  # samples per cycle (rounded)
    usable = (len(x) // n) * n
    return np.abs(x[:usable]).reshape(-1, n).max(axis=1)


def simulation(designs) -> dict:
    section("4. Simulated capture: tone burst through a generic 2nd-order modulator")
    dur = 0.25
    n = int(FS_PDM * dur)
    t = np.arange(n) / FS_PDM
    amp = 0.5
    results = {}
    for f_tone in (23_007.0, 25_013.0):  # off-grid so the sampling phase walks
        bits = sigma_delta_2nd(amp * np.sin(2 * np.pi * f_tone * t))
        for name, (h, dec) in designs.items():
            fs_out = FS_PDM / dec
            y = decimate(bits, h, dec)
            y = y[len(h) // dec + 50: -(len(h) // dec + 50)]  # drop filter edges
            raw = cycle_peaks(y, fs_out, f_tone)
            yi = signal.resample_poly(y, 4, 1)
            yi = yi[400:-400]
            interp = cycle_peaks(yi, fs_out * 4, f_tone)
            results[(name, f_tone)] = (raw.min() / amp - 1, raw.mean() / amp - 1, interp.min() / amp - 1)
            print(f"{f_tone / 1e3:6.3f} kHz | {name[:22]:<22} | raw worst {100 * (raw.min() / amp - 1):+6.1f} %"
                  f"  raw mean {100 * (raw.mean() / amp - 1):+6.1f} %"
                  f"  x4 interpolated worst {100 * (interp.min() / amp - 1):+6.2f} %")
            if f_tone > 25e3:
                results[("spec", name)] = (y, fs_out)
        if f_tone > 25e3:
            results["bits"] = bits
    print("(negative = under-read; positive values are modulator noise riding on the peak)")
    return results


# ---- 5. Throughput, storage, CPU --------------------------------------------
def budgets(designs) -> None:
    section("5. Link throughput and storage")
    pdm_bps = FS_PDM / 8
    tach_bps = 200e3 / 8
    block = 4096
    overhead = (24 + 4) * (pdm_bps / block + tach_bps / 512)
    total = pdm_bps + tach_bps + overhead
    usb_fs_max = 19 * 64 * 1000  # USB 2.0 spec Table 5-9 (bulk, full speed) [not re-opened]
    print(f"raw PDM {pdm_bps / 1e3:.0f} kB/s + tach bits {tach_bps / 1e3:.0f} kB/s + framing {overhead / 1e3:.1f} kB/s"
          f" = {total / 1e3:.0f} kB/s = {100 * total / usb_fs_max:.0f} % of the {usb_fs_max / 1e3:.0f} kB/s"
          f" full-speed bulk ceiling")
    print("\nPer 10 s capture and for 10 parts x 100 calibrated samples (1000 recordings):")
    for label, bps in (("raw PDM 3.2 Mbit/s", 400e3), ("200 kHz int16 PCM", 400e3),
                       ("100 kHz int16 PCM", 200e3), ("200 kHz float32 PCM", 800e3)):
        print(f"  {label:<22} {bps * 10 / 1e6:5.1f} MB each   {bps * 10 * 1000 / 1e9:5.1f} GB total")

    section("6. Decimation cost on THIS machine (not a CM5/CM4 measurement)")
    rng = np.random.default_rng(1)
    packed = rng.integers(0, 256, size=int(FS_PDM * 10 / 8), dtype=np.uint8)
    for name, (h, dec) in designs.items():
        t0 = time.perf_counter()
        bits = np.unpackbits(packed, bitorder="little")
        y = decimate(bits, h, dec)
        dt = time.perf_counter() - t0
        print(f"  {name[:22]:<22} 10 s of PDM -> {len(y)} samples in {dt:.2f} s")


# ---- 6. Figures ---------------------------------------------------------------
def figures(designs, results) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    FIG_DIR.mkdir(parents=True, exist_ok=True)

    # Filter responses with alias bands
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2), sharey=True)
    f = np.linspace(0, 600e3, 6001)
    for ax, (name, (h, dec)) in zip(axes, designs.items()):
        fs_out = FS_PDM / dec
        ax.plot(f / 1e3, gain_db(h, FS_PDM, f), color="#1f4e79", lw=1.4)
        for k in range(1, int(600e3 // fs_out) + 1):
            ax.axvspan((k * fs_out - 40e3) / 1e3, (k * fs_out + 40e3) / 1e3, color="#c0392b", alpha=0.15)
        ax.axvspan(0, 40, color="#27ae60", alpha=0.15)
        ax.set_title(name, fontsize=10)
        ax.set_xlabel("Frequency at the 3.2 MHz input (kHz)")
        ax.set_ylim(-140, 5)
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("Gain (dB)")
    fig.suptitle("Decimation filters: green = 0-40 kHz passband, red = bands that fold onto it", fontsize=10)
    fig.tight_layout()
    fig.savefig(FIG_DIR / "decimation_filters.png", dpi=130)
    plt.close(fig)

    # Spectra: PDM stream and decimated outputs (generic modulator)
    bits = results["bits"]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2))
    x = bits.astype(np.float32) * 2 - 1
    fw, pxx = signal.welch(x, fs=FS_PDM, nperseg=1 << 15, window="blackmanharris", scaling="spectrum")
    axes[0].semilogx(fw[1:] / 1e3, 10 * np.log10(pxx[1:] * 2), color="#555", lw=0.8)
    axes[0].set_title("Simulated 1-bit stream at 3.2 MHz (generic 2nd-order modulator)", fontsize=10)
    axes[0].set_xlabel("Frequency (kHz)")
    axes[0].set_ylabel("dBFS per bin")
    axes[0].grid(alpha=0.3, which="both")
    for (name, (h, dec)), color in zip(designs.items(), ("#1f4e79", "#c0392b")):
        y, fs_out = results[("spec", name)]
        fw, pxx = signal.welch(y, fs=fs_out, nperseg=1 << 13, window="blackmanharris", scaling="spectrum")
        axes[1].plot(fw / 1e3, 10 * np.log10(np.maximum(pxx * 2, 1e-20)), lw=0.9, color=color, label=name[:22])
    axes[1].set_title("After decimation: 25.013 kHz tone at -6 dBFS", fontsize=10)
    axes[1].set_xlabel("Frequency (kHz)")
    axes[1].legend(fontsize=8)
    axes[1].grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(FIG_DIR / "spectra.png", dpi=130)
    plt.close(fig)

    # Peak under-read vs frequency
    fig, ax = plt.subplots(figsize=(7, 4.2))
    fr = np.linspace(1e3, 45e3, 400)
    for fs, style, label in ((100e3, "-", "100 kHz, raw samples"), (200e3, "-", "200 kHz, raw samples"),
                             (400e3, "--", "100 kHz interpolated x4"), (800e3, "--", "200 kHz interpolated x4")):
        ax.plot(fr / 1e3, [100 * peak_error(fs, x) for x in fr], style, label=label)
    ax.axhline(25, color="k", lw=0.8, ls=":")
    ax.text(1.5, 26, "-25 % limit", fontsize=8)
    for fx in (23, 25):
        ax.axvline(fx, color="k", lw=0.5, ls=":")
    ax.set_xlabel("Tone frequency (kHz)")
    ax.set_ylabel("Worst-case peak under-read (%)")
    ax.set_ylim(0, 45)
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(FIG_DIR / "peak_under_read.png", dpi=130)
    plt.close(fig)
    print("\nFigures written to docs/phase0/figures")


def main() -> None:
    clock_plan()
    peak_error_table()
    designs = filter_report()
    results = simulation(designs)
    budgets(designs)
    figures(designs, results)


if __name__ == "__main__":
    main()
