"""The analysis catalogue: what is measured on every capture.

Each analysis turns the 200 kHz samples of one capture into a few named numbers
("features"). Analyses are plug-ins: a function registered with @analysis. A customer
configuration can add more without touching the rest.

Two paths, scored separately and then fused (spec, Analysis):
  periodic  rotating elements, gears, bearings, electrical whine, combustion: tones,
            harmonics, orders, modulation
  random    buzz, squeak, squeal, rattle, chatter, loose parts, air leaks: impulsiveness,
            spectral shape, transients
  both      used by both paths
Integrity features (gain, overall level) are never pruned.

Every feature is a plain float, or NaN when it does not apply (for example the order
analysis without a tach). The models replace NaN by the training median.

CATALOGUE_VERSION changes whenever any analysis changes, so features computed by an
older catalogue are recomputed, never mixed.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
from scipy import signal
from scipy.fft import next_fast_len

CATALOGUE_VERSION = "eol-catalogue-1"  # raise when any analysis changes its results
EPS = 1e-20


@dataclass
class Context:
    """What an analysis may use besides the samples. Expensive intermediate results are shared."""
    fs: float
    peak_threshold: float = 0.7
    tach_s: np.ndarray | None = None  # times of tach pulses, seconds from the first sample
    ppr: int = 1
    orders: dict[str, float] = field(default_factory=dict)  # part's order table: name -> order
    cache: dict = field(default_factory=dict)

    def get(self, key: str, make: Callable):
        if key not in self.cache:
            self.cache[key] = make()
        return self.cache[key]


@dataclass(frozen=True)
class Analysis:
    name: str
    path: str  # periodic, random, both, integrity
    fn: Callable[[np.ndarray, Context], dict[str, float]]
    needs_tach: bool = False
    description: str = ""


REGISTRY: dict[str, Analysis] = {}


def analysis(name: str, path: str, needs_tach: bool = False):
    def wrap(fn):
        REGISTRY[name] = Analysis(name, path, fn, needs_tach, (fn.__doc__ or "").strip().split("\n")[0])
        return fn
    return wrap


def db(x: float) -> float:
    return 10.0 * math.log10(max(x, EPS))


# ---- shared intermediate results ----

def welch_psd(x: np.ndarray, ctx: Context):
    """One-sided power spectral density, 8192-point Hann windows (24 Hz resolution at 200 kHz)."""
    return ctx.get("psd", lambda: signal.welch(x, fs=ctx.fs, nperseg=min(8192, len(x)), noverlap=None, detrend=False))


def envelope(x: np.ndarray, ctx: Context):
    """Hilbert envelope of the 2 to 40 kHz band, reduced to 10 kHz."""
    def make():
        sos = signal.butter(4, [2_000, min(40_000, ctx.fs * 0.45)], btype="band", fs=ctx.fs, output="sos")
        band = signal.sosfiltfilt(sos, x)
        n = len(band)
        env = np.abs(signal.hilbert(band, N=next_fast_len(n)))[:n]
        q = int(ctx.fs // 10_000)
        # Polyphase resampling: the same low-pass-and-keep-every-qth as decimate(), far fewer operations.
        return np.maximum(signal.resample_poly(env, 1, q), 0.0), ctx.fs / q
    return ctx.get("env", make)


def _autocorrelation(e: np.ndarray) -> np.ndarray:
    """Normalised autocorrelation, lag 0 first."""
    n = 1 << int(math.ceil(math.log2(2 * len(e))))
    r = np.fft.irfft(np.abs(np.fft.rfft(e, n)) ** 2, n)[: len(e)]
    return r / (r[0] + EPS)


def band_power(f: np.ndarray, p: np.ndarray, lo: float, hi: float) -> float:
    m = (f >= lo) & (f < hi)
    return float(p[m].sum() * (f[1] - f[0])) if m.any() else 0.0


def fractional_octave_bands(fraction: int, lo: float, hi: float) -> list[tuple[float, float, float]]:
    """(lower edge, centre, upper edge), base-10 centres as in IEC 61260."""
    g = 10 ** (3 / 10)
    out = []
    for k in range(-200, 200):
        centre = 1000 * g ** (k / fraction)
        if lo <= centre <= hi:
            out.append((centre * g ** (-1 / (2 * fraction)), centre, centre * g ** (1 / (2 * fraction))))
    return out


# ---- the analyses ----

@analysis("level", "integrity")
def level(x, ctx):
    """Overall level of the capture (after the golden gain)."""
    rms = float(np.sqrt(np.mean(x * x)))
    return {"rms_dbfs": db(2 * rms * rms), "peak_dbfs": 20 * math.log10(max(float(np.max(np.abs(x))), 1e-10))}


@analysis("time", "random")
def time_stats(x, ctx):
    """Time-domain statistics: crest factor, kurtosis, skewness, short-term level variation."""
    rms = float(np.sqrt(np.mean(x * x))) + 1e-15
    xc = x - x.mean()
    m2 = float(np.mean(xc ** 2)) + EPS
    n = int(ctx.fs * 0.01)  # 10 ms blocks
    blocks = x[: len(x) // n * n].reshape(-1, n)
    brms = np.sqrt(np.mean(blocks ** 2, axis=1)) + 1e-15
    bdb = 20 * np.log10(brms)
    return {"crest": float(np.max(np.abs(x))) / rms, "kurtosis": float(np.mean(xc ** 4)) / m2 ** 2 - 3.0,
            "skewness": float(np.mean(xc ** 3)) / m2 ** 1.5, "level_spread_db": float(np.percentile(bdb, 95) - np.percentile(bdb, 5)),
            "level_max_over_median_db": float(bdb.max() - np.median(bdb))}


NOMINAL_R10 = (1.0, 1.25, 1.6, 2.0, 2.5, 3.15, 4.0, 5.0, 6.3, 8.0)


def nominal_name(centre_hz: float) -> str:
    """The usual name of a 1/3-octave band: 25k for 25.12 kHz, 0.125k for 125.9 Hz."""
    decade = 10 ** math.floor(math.log10(centre_hz) + 1e-9)
    mantissa = min(NOMINAL_R10, key=lambda m: abs(math.log10(m * decade / centre_hz)))
    return f"{mantissa * decade / 1000:g}k"


@analysis("third_octave", "both")
def third_octave(x, ctx):
    """1/3-octave band levels, 100 Hz to 80 kHz (named by their nominal centre)."""
    f, p = welch_psd(x, ctx)
    return {f"{nominal_name(c)}_db": db(band_power(f, p, lo, hi)) for lo, c, hi in fractional_octave_bands(3, 100, 80_000)}


@analysis("twelfth_octave", "periodic")
def twelfth_octave(x, ctx):
    """1/12-octave band levels, 1 kHz to 80 kHz (narrow bands for tones, whine and squeal)."""
    f, p = welch_psd(x, ctx)
    return {f"{c / 1000:.4g}k_db": db(band_power(f, p, lo, hi)) for lo, c, hi in fractional_octave_bands(12, 1000, 80_000)}


@analysis("spectral_shape", "random")
def spectral_shape(x, ctx):
    """Spectral centroid, spread, flatness, 95 % roll-off and entropy, 100 Hz to 80 kHz."""
    f, p = welch_psd(x, ctx)
    m = (f >= 100) & (f <= 80_000)
    f, p = f[m], p[m] + EPS
    w = p / p.sum()
    centroid = float((f * w).sum())
    return {"centroid_hz": centroid, "spread_hz": float(np.sqrt(((f - centroid) ** 2 * w).sum())),
            "flatness": float(np.exp(np.mean(np.log(p))) / np.mean(p)),
            "rolloff95_hz": float(f[np.searchsorted(np.cumsum(w), 0.95)]),
            "entropy": float(-(w * np.log2(w)).sum() / math.log2(len(w)))}


@analysis("peaks", "periodic")
def peaks(x, ctx):
    """Spectral peaks above the peak threshold (share of the tallest), with level and decay time; five slots."""
    f, p = welch_psd(x, ctx)
    m = (f >= 100) & (f <= 80_000)
    f, p = f[m], p[m]
    amp = np.sqrt(p)
    idx, _ = signal.find_peaks(amp, height=ctx.peak_threshold * amp.max(), distance=4)
    idx = idx[np.argsort(amp[idx])[::-1]][:5]
    out = {"count": float(len(idx))}
    sf, st, z = ctx.get("stft", lambda: signal.stft(x, fs=ctx.fs, nperseg=1024, noverlap=512))
    mag = np.abs(z) + 1e-15
    for i in range(5):
        if i < len(idx):
            fi = f[idx[i]]
            row = 20 * np.log10(mag[int(np.argmin(np.abs(sf - fi)))])
            k = int(np.argmax(row))
            below = np.nonzero(row[k:] < row[k] - 20)[0]
            decay = (st[k + below[0]] - st[k]) * 1000 if len(below) else (st[-1] - st[k]) * 1000
            out.update({f"p{i + 1}_hz": float(fi), f"p{i + 1}_db": db(p[idx[i]]), f"p{i + 1}_decay_ms": float(decay)})
        else:
            out.update({f"p{i + 1}_hz": math.nan, f"p{i + 1}_db": math.nan, f"p{i + 1}_decay_ms": math.nan})
    return out


@analysis("envelope", "periodic")
def envelope_spectrum(x, ctx):
    """Hilbert envelope of 2 to 40 kHz: strongest modulation 5 Hz to 1 kHz (bearings, rattle, chatter)."""
    env, fs_e = envelope(x, ctx)
    e = env - env.mean()
    f, p = signal.welch(e, fs=fs_e, nperseg=min(len(e), 8192))
    m = (f >= 5) & (f <= 1000)
    # The repetition rate, not the loudest harmonic: a train of impacts has harmonics of equal
    # strength, so the spectrum alone cannot tell 15 Hz from 45 Hz. The first strong peak of the
    # envelope's autocorrelation (between 1 ms and 200 ms) gives the period.
    r = _autocorrelation(e)
    lo, hi = int(0.001 * fs_e), min(int(0.2 * fs_e), len(r) - 1)
    pk, _ = signal.find_peaks(r[lo:hi])
    if len(pk):
        strong = pk[r[lo:hi][pk] >= 0.5 * r[lo:hi][pk].max()]
        mod = fs_e / (lo + strong[0])
    else:
        mod = math.nan
    j = int(np.argmin(np.abs(f - mod))) if math.isfinite(mod) else int(np.argmax(np.where(m, p, 0)))
    return {"mod_hz": float(mod), "mod_prominence_db": db(p[j]) - db(float(np.median(p[m]))),
            "env_kurtosis": float(np.mean(e ** 4) / (np.mean(e ** 2) ** 2 + EPS) - 3.0),
            "env_cv": float(env.std() / (env.mean() + 1e-15))}


@analysis("cepstrum", "periodic")
def cepstrum(x, ctx):
    """Real cepstrum: strongest quefrency 0.2 to 20 ms (families of harmonics or sidebands, gears)."""
    n = 1 << 15
    k = max(1, min(16, len(x) // n))
    acc = np.zeros(n)
    w = np.hanning(n)
    for i in range(k):
        seg = x[i * n:(i + 1) * n]
        if len(seg) < n:
            break
        acc += np.fft.irfft(np.log(np.abs(np.fft.rfft(seg * w)) + 1e-12), n)
    acc /= k
    q = np.arange(n) / ctx.fs
    m = (q >= 0.0002) & (q <= 0.02)
    j = int(np.argmax(acc[m]))
    return {"quefrency_ms": float(q[m][j] * 1000), "magnitude": float(acc[m][j]),
            "magnitude_over_median": float(acc[m][j] / (np.median(np.abs(acc[m])) + 1e-12))}


@analysis("spectral_kurtosis", "random")
def spectral_kurtosis(x, ctx):
    """Spectral kurtosis at two window lengths (a two-level kurtogram): where impulsive energy sits."""
    out = {}
    for nper in (128, 1024):
        f, t, z = signal.stft(x, fs=ctx.fs, nperseg=nper, noverlap=nper // 2)
        p = np.abs(z) ** 2
        sk = np.mean(p ** 2, axis=1) / (np.mean(p, axis=1) ** 2 + EPS) - 2.0
        m = (f >= 1000) & (f <= 80_000)
        j = int(np.argmax(sk[m]))
        out.update({f"max_{nper}": float(sk[m][j]), f"max_hz_{nper}": float(f[m][j]), f"mean_{nper}": float(np.mean(sk[m]))})
    return out


@analysis("tkeo", "random")
def tkeo(x, ctx):
    """Teager-Kaiser energy operator: impulsiveness of the energy."""
    psi = x[1:-1] ** 2 - x[:-2] * x[2:]
    mean = float(np.mean(np.abs(psi))) + EPS
    return {"mean_db": db(mean), "p999_over_mean": float(np.percentile(np.abs(psi), 99.9)) / mean}


@analysis("autocorr", "periodic")
def autocorr(x, ctx):
    """Autocorrelation of the envelope: strongest repetition 1 to 100 ms."""
    env, fs_e = envelope(x, ctx)
    r = _autocorrelation(env - env.mean())
    lo, hi = int(0.001 * fs_e), min(int(0.1 * fs_e), len(r) - 1)
    j = lo + int(np.argmax(r[lo:hi]))
    return {"max_r": float(r[j]), "lag_ms": float(j / fs_e * 1000)}


@analysis("wavelet_packets", "random")
def wavelet_packets(x, ctx):
    """Haar wavelet packets, level 4: energy and burstiness of 16 sub-bands."""
    nodes = [x[: len(x) // 16 * 16].astype(np.float64)]
    for _ in range(4):
        nxt = []
        for a in nodes:
            a = a[: len(a) // 2 * 2]
            nxt += [(a[0::2] + a[1::2]) / math.sqrt(2), (a[0::2] - a[1::2]) / math.sqrt(2)]
        nodes = nxt
    out = {}
    total = sum(float(np.sum(c * c)) for c in nodes) + EPS
    for i, c in enumerate(nodes):
        e = c * c
        out[f"n{i:02d}_share_db"] = db(float(e.sum()) / total)
        out[f"n{i:02d}_kurtosis"] = float(np.mean(e ** 2) / (np.mean(e) ** 2 + EPS))
    return out


@analysis("transients", "random")
def transients(x, ctx):
    """Impacts in the envelope: how many per second stand 6x above the median, and how fast they die."""
    env, fs_e = envelope(x, ctx)
    med = float(np.median(env)) + 1e-15
    idx, _ = signal.find_peaks(env, height=6 * med, distance=int(0.005 * fs_e))
    decays = []
    for i in idx[:200]:
        below = np.nonzero(env[i:i + int(0.05 * fs_e)] < env[i] / 10)[0]
        if len(below):
            decays.append(below[0] / fs_e * 1000)
    return {"impacts_per_s": len(idx) / (len(env) / fs_e), "decay_ms": float(np.median(decays)) if decays else math.nan,
            "max_over_median": float(env.max() / med)}


@analysis("harmonics", "periodic")
def harmonics(x, ctx):
    """Strongest tone 50 Hz to 20 kHz: energy in its harmonics, and sidebands at the envelope modulation."""
    f, p = welch_psd(x, ctx)
    df = f[1] - f[0]
    m = (f >= 50) & (f <= 20_000)
    k0 = int(np.argmax(np.where(m, p, 0)))
    f0, p0 = f[k0], p[k0] + EPS

    def at(freq):
        if freq <= 0 or freq >= f[-1]:
            return 0.0
        j = int(round(freq / df))
        return float(p[max(j - 1, 0):j + 2].max())

    harm = sum(at(k * f0) for k in range(2, 11))
    mod = REGISTRY["envelope"].fn(x, ctx)["mod_hz"]
    side = at(f0 - mod) + at(f0 + mod) if math.isfinite(mod) else 0.0
    return {"f0_hz": float(f0), "harmonic_ratio_db": db(harm / p0), "sideband_ratio_db": db(side / p0)}


@analysis("orders", "periodic", needs_tach=True)
def orders(x, ctx):
    """Order tracking from the tach: order spectrum, the part's listed orders, and time-synchronous average."""
    t = ctx.tach_s
    if t is None or len(t) < 4 * ctx.ppr + 2:
        return {"rpm": math.nan, "top_order": math.nan, "top_order_db": math.nan, "tsa_residual_db": math.nan,
                **{f"{name}_db": math.nan for name in ctx.orders}}
    revs = np.arange(len(t)) / ctx.ppr  # revolutions at each pulse
    n_per_rev = 256
    ts = np.arange(len(x)) / ctx.fs
    angle = np.interp(ts, t, revs, left=np.nan, right=np.nan)
    ok = ~np.isnan(angle)
    a0, a1 = math.ceil(angle[ok][0]), math.floor(angle[ok][-1])
    grid = np.arange(a0, a1, 1 / n_per_rev)
    xa = np.interp(grid, angle[ok], x[ok])  # the signal sampled at equal angles
    n_rev = len(xa) // n_per_rev
    xa = xa[: n_rev * n_per_rev]
    spec = np.abs(np.fft.rfft(xa * np.hanning(len(xa)))) ** 2
    order_axis = np.fft.rfftfreq(len(xa), 1 / n_per_rev)
    m = (order_axis >= 0.5) & (order_axis <= 100)
    j = int(np.argmax(spec[m]))
    tsa = xa.reshape(n_rev, n_per_rev).mean(axis=0)
    residual = xa.reshape(n_rev, n_per_rev) - tsa
    out = {"rpm": float(60 * (revs[-1] - revs[0]) / (t[-1] - t[0])), "top_order": float(order_axis[m][j]),
           "top_order_db": db(float(spec[m][j]) / len(xa) ** 2),
           "tsa_residual_db": db(float(np.mean(residual ** 2)) / (float(np.mean(xa ** 2)) + EPS))}
    for name, order in ctx.orders.items():
        k = int(np.argmin(np.abs(order_axis - order)))
        out[f"{name}_db"] = db(float(spec[max(k - 1, 0):k + 2].max()) / len(xa) ** 2)
    return out


def compute(x: np.ndarray, ctx: Context, skip: set[str] = frozenset()) -> tuple[dict[str, float], dict[str, float]]:
    """Runs every analysis not in `skip`. Returns ({"analysis.feature": value}, {analysis: seconds})."""
    x = np.asarray(x, dtype=np.float64)
    values, timings = {}, {}
    for name, a in REGISTRY.items():
        if name in skip:
            continue
        t0 = time.perf_counter()
        for k, v in a.fn(x, ctx).items():
            values[f"{name}.{k}"] = float(v)
        timings[name] = time.perf_counter() - t0
    return values, timings
