"""Tests of the analysis catalogue, the golden gain and the models, on synthetic parts."""

import math
import pickle
import time
import unittest

try:
    import numpy as np
    import scipy  # noqa: F401
    import sklearn  # noqa: F401

    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

if HAVE_DEPS:
    from eol_analysis import ml
    from eol_analysis.catalogue import CATALOGUE_VERSION, REGISTRY, Context, compute
    from eol_analysis.gain import band_level, calibration_gain, reference_level
    from synth import FS, part, tach

CLASSES = ("golden", "squeal", "rattle")


def features(kind, seed, **ctx):
    return compute(part(kind, seed), Context(FS, **ctx))[0]


@unittest.skipUnless(HAVE_DEPS, "numpy, scipy or scikit-learn is not installed")
class CatalogueTests(unittest.TestCase):
    def test_every_analysis_runs_and_names_are_unique_and_finite(self):
        values, timings = compute(part("golden", 1), Context(FS))
        self.assertEqual(set(timings), set(REGISTRY))
        self.assertGreater(len(values), 150)
        bad = [k for k, v in values.items() if not math.isfinite(v) and not k.startswith(("peaks.p", "orders.", "transients.decay"))]
        self.assertEqual(bad, [])
        self.assertTrue(CATALOGUE_VERSION)

    def test_a_squeal_shows_as_a_peak_and_in_its_bands(self):
        g, s = features("golden", 2), features("squeal", 2)
        self.assertAlmostEqual(s["peaks.p1_hz"], 25_000, delta=200)
        self.assertGreater(s["third_octave.25k_db"] - g["third_octave.25k_db"], 15)

    def test_a_rattle_shows_as_impacts_modulation_and_impulsiveness(self):
        g, r = features("golden", 3), features("rattle", 3)
        self.assertAlmostEqual(r["transients.impacts_per_s"], 15, delta=2)
        self.assertAlmostEqual(r["envelope.mod_hz"], 15, delta=2)
        self.assertGreater(r["time.kurtosis"], g["time.kurtosis"] + 3)
        self.assertGreater(r["spectral_kurtosis.max_128"], g["spectral_kurtosis.max_128"])
        self.assertGreater(r["tkeo.p999_over_mean"], g["tkeo.p999_over_mean"])

    def test_a_harmonic_family_shows_in_the_cepstrum(self):
        t = np.arange(int(FS)) / FS
        x = sum(0.01 * np.sin(2 * np.pi * 500 * k * t) for k in range(1, 30)) + np.random.default_rng(0).normal(0, 0.001, len(t))
        v = compute(x, Context(FS))[0]
        self.assertAlmostEqual(v["cepstrum.quefrency_ms"], 2.0, delta=0.05)

    def test_order_tracking_with_a_tach(self):
        rpm, seconds = 1800.0, 1.0
        t = np.arange(int(FS * seconds)) / FS
        x = 0.05 * np.sin(2 * np.pi * (rpm / 60) * 7 * t) + np.random.default_rng(1).normal(0, 0.002, len(t))  # 7th order
        v = compute(x, Context(FS, tach_s=tach(rpm, seconds, ppr=2), ppr=2, orders={"gear": 7.0, "none": 4.0}))[0]
        self.assertAlmostEqual(v["orders.rpm"], rpm, delta=5)
        self.assertAlmostEqual(v["orders.top_order"], 7.0, delta=0.1)
        self.assertGreater(v["orders.gear_db"], v["orders.none_db"] + 20, "the listed order that is present stands out")
        self.assertLess(v["orders.tsa_residual_db"], -20, "a signal locked to the shaft is almost all in the synchronous average")
        no_tach = compute(x, Context(FS))[0]
        self.assertTrue(math.isnan(no_tach["orders.rpm"]))

    def test_skipped_analyses_are_not_computed(self):
        values, timings = compute(part("golden", 1), Context(FS), skip={"wavelet_packets", "cepstrum"})
        self.assertNotIn("wavelet_packets", timings)
        self.assertFalse(any(k.startswith("cepstrum.") for k in values))

    def test_timing_is_reported_per_analysis(self):
        t0 = time.perf_counter()
        _, timings = compute(part("golden", 1), Context(FS))
        self.assertLessEqual(sum(timings.values()), time.perf_counter() - t0 + 1e-6)
        self.assertTrue(all(v >= 0 for v in timings.values()))


@unittest.skipUnless(HAVE_DEPS, "numpy, scipy or scikit-learn is not installed")
class GainTests(unittest.TestCase):
    def test_band_level_of_a_sine(self):
        t = np.arange(int(FS)) / FS
        self.assertAlmostEqual(band_level(0.5 * np.sin(2 * np.pi * 10_000 * t), FS), 0.5 / math.sqrt(2), delta=0.005)
        self.assertLess(band_level(0.5 * np.sin(2 * np.pi * 60_000 * t), FS), 0.01, "outside the 1 to 40 kHz band")

    def test_gain_corrects_a_change_of_sensitivity_and_refuses_a_large_one(self):
        ref = reference_level([0.10, 0.11, 0.09])
        g, why = calibration_gain(ref, 0.06)  # 4.4 dB less sensitive
        self.assertAlmostEqual(g, 0.10 / 0.06)
        self.assertIsNone(why)
        self.assertIn("outside", calibration_gain(ref, 0.05)[1], "+6.02 dB is just over the limit")
        g, why = calibration_gain(ref, 0.03)
        self.assertIn("outside", why)
        self.assertIsNotNone(calibration_gain(ref, 0.0)[1])
        with self.assertRaises(ValueError):
            reference_level([])


def training_data(classes=CLASSES, per_class=10, units=5, offset=0):
    values, labels, groups = [], [], []
    for c in classes:
        for i in range(per_class):
            values.append(features(c, 1000 * CLASSES.index(c) + i + offset if c in CLASSES else 9000 + i))
            labels.append(ml.OK if c == "golden" else c)
            groups.append(f"{c}-unit{i % units}")
    return ml.TrainingData(values, labels, groups)


@unittest.skipUnless(HAVE_DEPS, "numpy, scipy or scikit-learn is not installed")
class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = training_data()
        cls.model, cls.report = ml.train(cls.data)

    def test_new_parts_are_judged_right(self):
        for kind, verdict, mode in (("golden", "OK", None), ("squeal", "NOK", "squeal"), ("rattle", "NOK", "rattle")):
            for seed in (500, 501, 502):
                with self.subTest(kind=kind, seed=seed):
                    d = self.model.decide(features(kind, 50_000 + seed))
                    self.assertEqual((d.verdict, d.failure_mode), (verdict, mode), d.reasons)

    def test_a_fault_never_trained_on_never_passes(self):
        # Twenty different parts: an escape here is the worst error the stand can make.
        verdicts = [self.model.decide(features("novel", 60_000 + s)).verdict for s in range(20)]
        self.assertEqual(verdicts.count("OK"), 0, verdicts)
        self.assertGreaterEqual(verdicts.count("UNDEFINED"), 15, verdicts)

    def test_good_parts_pass(self):
        verdicts = [self.model.decide(features("golden", 61_000 + s)).verdict for s in range(20)]
        self.assertGreaterEqual(verdicts.count("OK"), 19, verdicts)

    def test_cross_validation_is_grouped_and_reports_rates(self):
        cv = ml.cross_validate(self.data)
        self.assertEqual(cv["folds"], 5)
        self.assertEqual(cv["tested"], 30)
        self.assertEqual(cv["escape_rate"], 0.0)
        self.assertLessEqual(cv["false_reject_rate"], 0.1)
        self.assertGreaterEqual(cv["right_mode_rate"], 0.8)
        # A fault is either named correctly or left UNDEFINED: never passed, never given another fault's name.
        for true in ("squeal", "rattle"):
            self.assertLessEqual(set(cv["confusion"][true]), {f"NOK:{true}", "UNDEFINED"}, cv["confusion"])

    def test_one_unit_per_class_cannot_be_cross_validated_by_unit(self):
        d = ml.TrainingData(self.data.values, self.data.labels, ["same"] * len(self.data.labels))
        self.assertEqual(ml.cross_validate(d)["folds"], 0)

    def test_any_enabled_method_can_veto_a_pass(self):
        class AlwaysSqueal:
            classes_ = np.array(["OK", "rattle", "squeal"])

            def predict_proba(self, z):
                return np.array([[0.05, 0.05, 0.9]])

        golden = features("golden", 60_000)
        self.assertEqual(self.model.decide(golden).verdict, "OK")
        saved = self.model.classifiers["gboost"]
        try:
            self.model.classifiers["gboost"] = AlwaysSqueal()
            off = [(m, e and m != "gboost") for m, e in ml.DEFAULT_PRIORITY]
            on = [(m, e or m == "gboost") for m, e in ml.DEFAULT_PRIORITY]
            self.assertEqual(self.model.decide(golden, off).verdict, "OK", "a disabled method has no say")
            d = self.model.decide(golden, on)
            self.assertNotEqual(d.verdict, "OK", "an enabled method saying NOK blocks the pass")
            self.assertTrue(any("gboost" in r for r in d.reasons))
        finally:
            self.model.classifiers["gboost"] = saved

    def test_the_first_method_in_the_order_gives_the_reported_mode(self):
        rattle = features("rattle", 70_000)
        d = self.model.decide(rattle)
        self.assertEqual(d.failure_mode, "rattle")
        self.assertEqual([v["method"] for v in d.votes], [m for m, _ in ml.DEFAULT_PRIORITY])

    def test_pruning_report_never_prunes_integrity(self):
        self.assertIn("best_d_by_analysis", self.report)
        self.assertNotIn("level", self.report["pruned"])
        for name in self.model.feature_names:
            self.assertNotIn(name.split(".")[0], self.model.pruned)

    def test_too_few_samples_are_refused(self):
        with self.assertRaisesRegex(ValueError, "Golden OK"):
            ml.train(ml.TrainingData(self.data.values[:3], self.data.labels[:3], self.data.groups[:3]))
        idx = list(range(10)) + [10, 11]  # golden plus two squeal
        sub = ml.TrainingData([self.data.values[i] for i in idx], [self.data.labels[i] for i in idx], [self.data.groups[i] for i in idx])
        with self.assertRaisesRegex(ValueError, "squeal"):
            ml.train(sub)

    def test_a_saved_model_decides_the_same(self):
        copy = pickle.loads(pickle.dumps(self.model))
        v = features("squeal", 80_000)
        self.assertEqual(copy.decide(v).to_dict(), self.model.decide(v).to_dict())

    def test_golden_only_trains_an_envelope_without_pruning(self):
        d = ml.TrainingData(self.data.values[:10], self.data.labels[:10], self.data.groups[:10])
        model, report = ml.train(d)
        self.assertEqual(model.pruned, [])
        self.assertIn("nothing pruned", report["note"])
        self.assertEqual(model.decide(features("golden", 90_000)).verdict, "OK")
        self.assertNotEqual(model.decide(features("squeal", 90_000)).verdict, "OK")


if __name__ == "__main__":
    unittest.main()
