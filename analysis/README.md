# analysis: what is measured, and how OK / NOK / UNDEFINED is decided

Python package `eol_analysis`. Needs numpy, scipy and scikit-learn (`../requirements.txt`).

**State: build-plan step 6. Tested on synthetic parts on a laptop and on the CM5; the catalogue has run on real bench recordings. No model has been trained on real parts yet: that needs Golden OK and failure-mode parts.**

## The chain

```
 raw 1-bit stream ─► decimation filter (../dsp) ─► 200 kHz samples ─► × golden gain ─► catalogue: ~190 features
                                                                                          │
                                                       part's approved model ◄────────────┘
                                                                │
                                         OK / NOK:<mode> / UNDEFINED PRODUCT (+ CONTROLLED DRIFT)
```

INVALID is decided before all of this, by the acquisition service (`../acquire`).

## Golden-relative gain (`gain.py`)

The spec leaves the method to me: "NDT-style relative scaling … so real loudness differences stay visible and the gain is recorded".

- **Reference:** the RMS level, 1 to 40 kHz, of the Golden OK training samples (median), stored with each trained model.
- **At calibration:** the golden part is measured again; `gain = reference / golden level now`.
- **Every capture until the next calibration** is multiplied by that gain before analysis.

So a slow change of the microphone chain (ageing, dust, a slightly moved microphone) is cancelled. Real differences between parts stay, because every part of the session gets the same gain. The gain is stored with the calibration and with every result. If it is outside ±6 dB (programming variable `gain_limit_db`), the calibration fails: a change that large should be looked at, not scaled away.

## The catalogue (`catalogue.py`)

Each analysis is a plug-in registered with `@analysis(name, path)`; a customer configuration can add one without changing the rest. `CATALOGUE_VERSION` changes whenever results change, so features from different catalogue versions are never mixed.

| Analysis | Path | What it gives |
|---|---|---|
| `level` | integrity (never pruned) | overall RMS and peak, dBFS |
| `time` | random | crest factor, kurtosis, skewness, spread of the 10 ms level |
| `third_octave` | both | 1/3-octave band levels, 100 Hz to 80 kHz, named by nominal centre |
| `twelfth_octave` | periodic | 1/12-octave levels, 1 to 80 kHz (tones, whine, squeal) |
| `spectral_shape` | random | centroid, spread, flatness, 95 % roll-off, entropy |
| `peaks` | periodic | spectral peaks above `peak_threshold` (0.7) of the tallest: frequency, level, decay time (5 slots) |
| `envelope` | periodic | Hilbert envelope of 2 to 40 kHz: repetition rate (5 Hz to 1 kHz), prominence, kurtosis |
| `cepstrum` | periodic | strongest quefrency 0.2 to 20 ms (harmonic and sideband families, gears) |
| `spectral_kurtosis` | random | spectral kurtosis at two window lengths (a two-level kurtogram) |
| `tkeo` | random | Teager-Kaiser energy: level and impulsiveness |
| `autocorr` | periodic | strongest repetition of the envelope, 1 to 100 ms |
| `wavelet_packets` | random | Haar wavelet packets, level 4: energy share and burstiness of 16 bands |
| `transients` | random | impacts per second in the envelope and their decay |
| `harmonics` | periodic | strongest tone 50 Hz to 20 kHz: harmonic ratio, sideband ratio |
| `orders` | periodic, needs the tach | order spectrum, the part's listed orders, time-synchronous average (TSA) residual |

The spec's "periodic and random paths scored separately, then fused" is done in the models: every method sees features of both paths, and the safety-first rule fuses the methods. Separate scores per path for the screen are a later addition. **[NOT VERIFIED]** that a per-path split helps on real parts.

Without a tach (the bench has none yet), `orders` gives NaN and the models ignore it. It is tested on a synthetic tach only.

**Speed on the CM5** (Rev 1.0, Python 3.13, numpy 2.2.4, scipy 1.15.3), one core, 10 s capture: decimation filter 0.74 s + catalogue 1.15 to 1.20 s, about 1.9 s in total. Each result stores how long every analysis took (`decision_json.timings_s`): that is the compute-budget monitor the spec asks for. CM4: **[NOT VERIFIED]**.

## The models (`ml.py`)

Methods, each enabled and ordered per part (Setup → part → Models):

| Method | What | Default |
|---|---|---|
| `golden_envelope` | distance to the Golden OK samples: Mahalanobis (shrunk covariance) inside the PCA subspace of normal variation **and** the residual off it. Both must be small | always on |
| `isolation_forest` | a second one-class check on Golden OK, leave-one-out threshold | **off**: with about 10 golden samples it rejected good parts in the tests. Enable from about 30 |
| `knn` | k nearest neighbours after PCA, over Golden OK and every failure mode | on |
| `svm` | RBF support vector machine with calibrated probabilities, same classes | on |
| `gboost` | gradient-boosted trees, same classes | off: needs more samples |

Every failure mode also gets its own envelope, built the same way as the golden one.

**Why the residual matters (found in testing):** without it, a synthetic fault that moved one feature by 47 standard deviations, in a direction good parts never vary in, passed as OK. PCA keeps only the directions of normal variation, so a change at right angles to them is invisible to the Mahalanobis distance. The residual is exactly that change.

### The decision, safety first

1. **OK** only if the part is inside the golden envelope **and every enabled method agrees**. Any enabled method can veto a pass.
2. Otherwise **NOK:\<mode\>** if a method, taken in the part's method order, names a failure mode with probability ≥ 0.6 **and** the part lies inside that mode's envelope. The first method in the order that does so gives the reported mode. A part outside the golden envelope and inside exactly one mode's envelope is also NOK for that mode.
3. Otherwise **UNDEFINED PRODUCT**: outside all envelopes, or the methods disagree.

So the order changes which failure mode is reported when methods disagree. It never turns a NOK or UNDEFINED into OK.

### Controlled drift (yellow): options

| Option | Rule | Built |
|---|---|---|
| A. Close to the edge | an OK part whose golden distance is above 80 % of the threshold | **yes** (`drift_fraction`), stored in `drift_events` |
| B. Trend | the running mean of the golden distance over the last N OK parts rises by more than k standard deviations of the calibration samples | no |
| C. Feature watch | single features (for example the 20 to 31.5 kHz bands) move towards their golden limits over a shift | no |

A is simple and explainable on the screen; B and C catch slow change earlier but need settings that only real production data can tune. The user may ignore drift: it never changes OK into anything else.

### Training, cross-validation, pruning

- Training uses the **training set**; the **validation set** is judged afterwards by the trained model and reported separately.
- **Cross-validation is grouped by physical unit** (unit serial), so one part's samples never sit on both sides of a split. If a class has samples from only one unit, grouped cross-validation is impossible and the page says so: record samples from several units.
- Reported: **escape rate** (NOK passed as OK), **false-reject rate** (OK not passed), share with the right failure mode, and the confusion of verdicts.
- **Pruning**: an analysis is pruned if none of its features separates Golden OK from any failure mode by at least 0.5 standard deviations (Cohen's d). Pruning is redone inside every fold, so the reported rates are not flattered by it. Integrity analyses are never pruned. On approval, pruned analyses stop being computed for that part and their stored values are deleted; the selection report stays with the model.
- Minimums: 5 Golden OK and 3 per failure mode in the training set.

## Tests

```
PYTHONPATH=analysis:analysis/tests python3 -m unittest discover -s analysis/tests
```

Expected: 20 tests, `OK`. Synthetic parts (`tests/synth.py`): golden; squeal (a 25 kHz tone); rattle (impacts at 15 per second); and a novel fault (an 11 kHz buzz), never trained on. Checked:

- the right features respond to each fault;
- the cepstrum finds a 2 ms harmonic family;
- order tracking finds the 7th order and 1800 rpm;
- 20 new golden parts give at least 19 OK;
- new squeal and rattle parts are named correctly;
- **20 novel parts give zero OK**;
- cross-validation gives zero escapes, and a fault is never given another fault's name;
- any enabled method vetoes a pass, and a disabled method has no say;
- the first method in the order names the mode;
- too few samples are refused;
- a saved model decides the same;
- the gain limit.

I broke the code six ways on purpose (residual ignored, majority vote instead of any-veto, disabled methods voting, a mode named without its envelope, in-sample instead of leave-one-out limits, gain limit ignored); all six were caught.

## Not verified

- On real parts. All model figures come from synthetic parts. The real escape and false-reject rates come from Setup samples of real Golden OK and failure-mode parts.
- The defaults (margin 1.25, p_min 0.6, drift 80 %, prune d 0.5) are reasoned, not tuned on production data.
- Order tracking on a real tach.
