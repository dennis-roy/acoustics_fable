"""Training, approval, method order, and the Testing flow (calibration gate, results, outputs).

Training uses synthetic parts (analysis/tests/synth.py). The Testing flow runs end to end
through the acquisition service and the Python Pico simulator, with a stand-in model whose
decisions are scripted, so every path of the flow can be driven."""

import json
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

try:
    import flask  # noqa: F401
    import sklearn  # noqa: F401

    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

if HAVE_DEPS:

    from eol_acquire.service import AcquisitionService
    from eol_acquire.store import RecordingStore
    from eol_analysis.catalogue import CATALOGUE_VERSION, Context, compute
    from eol_analysis.gain import band_level
    from eol_analysis.ml import Decision
    from eol_app import db as dbm
    from eol_app import testing_views, training
    from eol_app.security import hash_password
    from eol_app.web import create_app
    from eol_dsp import decimate_bits, get_filter
    from pico_sim import PicoSim, micsig

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "analysis" / "tests"))
    from synth import FS, part as synth_part

PW = "Zynith2026x"


def csrf_of(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@unittest.skipUnless(HAVE_DEPS, "flask or scikit-learn is not installed")
class TrainingTests(unittest.TestCase):
    """Samples with synthetic features in a real database, then train, approve, load."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.db_path = str(Path(d.name) / "eol.sqlite3")
        self.conn = dbm.open_database(self.db_path, hash_password)
        self.addCleanup(self.conn.close)
        c, now = self.conn, dbm.utcnow()
        spec = get_filter()
        c.execute("INSERT INTO parts (id, part_number, capture_s, created_utc) VALUES (1, 'P1', 5, ?)", (now,))
        for mid, name in ((1, "squeal"), (2, "rattle")):
            c.execute("INSERT INTO failure_modes (id, part_id, name, created_utc) VALUES (?, 1, ?, ?)", (mid, name, now))
        sid = 0
        for kind, mode in (("golden", None), ("squeal", 1), ("rattle", 2)):
            for i in range(12):
                sid += 1
                x = synth_part(kind, 100 * sid)
                values, _ = compute(x, Context(FS))
                values["gain.band_level"] = band_level(x, FS)
                c.execute("INSERT INTO recordings (id, path, sha256, bytes, seconds, clock_hz, purpose, captured_utc) VALUES (?, '/none', ?, 1, 1, 3200000, ?, ?)",
                          (f"rec-{sid}", "0" * 64, "golden" if mode is None else "failure_mode", now))
                c.execute("INSERT INTO samples (id, part_id, kind, failure_mode_id, sample_set, unit_serial, recording_id, created_utc)"
                          " VALUES (?, 1, ?, ?, ?, ?, ?, ?)", (sid, "golden" if mode is None else "failure_mode", mode,
                                                                "validation" if i % 6 == 5 else "training", f"{kind}-u{i % 5}", f"rec-{sid}", now))
                c.execute("INSERT INTO features (owner_type, owner_id, catalogue_version, filter_name, filter_sha256, gain, values_json, created_utc)"
                          " VALUES ('sample', ?, ?, ?, ?, 1.0, ?, ?)", (sid, CATALOGUE_VERSION, spec.name, spec.sha256, json.dumps(values), now))

    def test_train_approve_load_and_refuse_a_changed_model_file(self):
        progress = {}
        version = training.train_part(self.db_path, 1, None, progress)
        self.assertEqual(version, 1)
        rows = self.conn.execute("SELECT method, state, metrics_json, artifact_path FROM models").fetchall()
        self.assertEqual({r["method"] for r in rows}, {"golden_envelope", "isolation_forest", "knn", "svm", "gboost"})
        metrics = json.loads(rows[0]["metrics_json"])
        self.assertEqual(metrics["cross_validation"]["escape_rate"], 0.0)
        self.assertEqual(metrics["validation"]["escapes"], 0)
        self.assertEqual(metrics["validation"]["samples"], 6)
        self.assertIsNone(training.load_model(self.conn, 1)[0], "nothing approved yet")
        training.approve(self.conn, 1, 1, None)
        bundle, v = training.load_model(self.conn, 1)
        self.assertEqual(v, 1)
        self.assertGreater(bundle["reference_level"], 0)
        golden = compute(synth_part("golden", 99_999), Context(FS))[0]
        self.assertEqual(bundle["model"].decide(golden).verdict, "OK")
        path = Path(rows[0]["artifact_path"])
        path.write_bytes(path.read_bytes() + b"x")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            training.load_model(self.conn, 1)
        actions = [r[0] for r in self.conn.execute("SELECT action FROM audit_log")]
        self.assertIn("model_trained", actions)
        self.assertIn("model_approved", actions)

    def test_a_second_approval_retires_the_first(self):
        training.train_part(self.db_path, 1, None, {})
        training.approve(self.conn, 1, 1, None)
        training.train_part(self.db_path, 1, None, {})
        training.approve(self.conn, 1, 2, None)
        states = dict(self.conn.execute("SELECT version, state FROM models WHERE method = 'knn'").fetchall())
        self.assertEqual(states, {1: "retired", 2: "approved"})
        with self.assertRaises(ValueError):
            training.approve(self.conn, 1, 1, None)


class StubModel:
    """Decides what the test tells it to."""
    pruned, feature_names = [], ["level.rms_dbfs"]

    def __init__(self):
        self.next = []

    def decide(self, values, priority=None):
        verdict, mode, drift = self.next.pop(0)
        return Decision(verdict, mode, drift, 0.5, 1.0, {}, [], [f"scripted {verdict}"])


@unittest.skipUnless(HAVE_DEPS, "flask or scikit-learn is not installed")
class TestingFlowTests(unittest.TestCase):
    def start(self, mic="gnd"):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        sim = PicoSim(mic=mic).start()
        self.addCleanup(sim.stop)
        self.service = AcquisitionService(RecordingStore(self.dir / "data"), microphone_id="sim mic", data_port=sim.data_path)
        sock = Path(tempfile.mkdtemp(dir="/tmp")) / "acq.sock"
        threading.Thread(target=self.service.serve, args=(sock,), daemon=True).start()
        self.assertTrue(self.service.ready.wait(5))
        self.addCleanup(self.service.shutdown)
        self.db_path = self.dir / "eol.sqlite3"
        app = create_app(self.db_path, sock)
        self.c = app.test_client()
        conn = dbm.connect(self.db_path)
        self.addCleanup(conn.close)
        self.conn = conn
        conn.execute("INSERT INTO parts (id, part_number, capture_s, created_utc) VALUES (1, 'P1', 5, ?)", (dbm.utcnow(),))
        conn.execute("INSERT INTO failure_modes (id, part_id, name, created_utc) VALUES (1, 1, 'squeal', ?)", (dbm.utcnow(),))
        # The simulated signal's level, so the golden calibration gives a gain of 1.
        x = decimate_bits(micsig.stream(0, 2_000_000), get_filter()).inner()
        self.stub = StubModel()
        bundle = {"model": self.stub, "reference_level": band_level(x, 200_000.0)}
        patcher = mock.patch.object(testing_views, "load_model", return_value=(bundle, 7))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.c.post("/login", data={"username": "master", "password": dbm.MASTER_DEFAULT_PASSWORD})
        page = self.c.get("/password").get_data(as_text=True)
        self.c.post("/password", data={"csrf": csrf_of(page), "current": dbm.MASTER_DEFAULT_PASSWORD, "new": PW, "repeat": PW})
        self.csrf = csrf_of(self.c.get("/").get_data(as_text=True))

    def post(self, url, **data):
        return self.c.post(url, data={"csrf": self.csrf, **data}, follow_redirects=True).get_data(as_text=True)

    def calibrate(self, golden=("OK", None, False), reject=("NOK", "squeal", False)):
        self.post("/testing/select/1")
        self.stub.next = [golden, reject]
        self.post("/testing/1/calibrate")
        return self.post("/testing/1/calibrate")

    def test_calibration_then_testing_stores_results_but_never_raw_data(self):
        self.start()
        self.assertIn("Calibrate this part first", self.post("/testing/1/run", unit_serial="U1"))
        page = self.calibrate()
        self.assertIn("Calibration passed", page)
        cal = self.conn.execute("SELECT * FROM calibration_checks").fetchone()
        self.assertEqual(cal["passed"], 1)
        self.assertAlmostEqual(cal["gain"], 1.0, delta=0.05)

        self.stub.next = [("OK", None, True)]
        page = self.post("/testing/1/run", unit_serial="U-100")
        self.assertIn("CONTROLLED DRIFT", page)
        r = self.conn.execute("SELECT * FROM test_results WHERE purpose = 'production'").fetchone()
        self.assertEqual((r["verdict"], r["unit_serial"], r["drift"], r["calibration_check_id"]), ("OK", "U-100", 1, cal["id"]))
        decision = json.loads(r["decision_json"])
        self.assertEqual(decision["model_version"], 7)
        self.assertIn("envelope", decision["timings_s"])
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM features WHERE owner_type = 'test_result' AND owner_id = ?", (r["id"],)).fetchone())
        self.assertEqual(self.conn.execute("SELECT count(*) FROM drift_events").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM edge_outbox").fetchone()[0], 1)

        # Retest of the same unit is linked to the first result.
        self.stub.next = [("NOK", "squeal", False)]
        page = self.post("/testing/1/run", unit_serial="U-100", retest="1")
        self.assertIn("NOK: squeal", page)
        r2 = self.conn.execute("SELECT * FROM test_results ORDER BY id DESC").fetchone()
        self.assertEqual((r2["retest_of"], r2["failure_mode_id"]), (r["id"], 1))

        # Without a heartbeat the Pico refuses outputs: the screen says so and it is logged.
        self.assertIn("PLC outputs were not set", page)
        events = [json.loads(e[0]) for e in self.conn.execute("SELECT detail_json FROM io_events")]
        self.assertTrue(all("SAFE_STATE" in (e["problem"] or "") for e in events))
        self.assertEqual(list(self.dir.rglob("*.pdm")), [], "no raw data of any test was written")

    def test_a_golden_that_does_not_pass_blocks_testing(self):
        self.start()
        self.post("/testing/select/1")
        self.stub.next = [("UNDEFINED", None, False)]
        page = self.post("/testing/1/calibrate")
        self.assertIn("Calibration failed", page)
        self.assertEqual(self.conn.execute("SELECT passed FROM calibration_checks").fetchone()[0], 0)
        self.assertIn("Calibrate this part first", self.post("/testing/1/run"))

    def test_a_reject_that_passes_blocks_testing(self):
        self.start()
        page = self.calibrate(reject=("OK", None, False))
        self.assertIn("the Reject part must fail", page)
        self.assertIn("Calibrate this part first", self.post("/testing/1/run"))

    def test_an_invalid_capture_is_invalid_with_its_reason_and_lights_no_result_line(self):
        self.start(mic="stuck")
        self.post("/testing/select/1")
        self.stub.next = []
        page = self.post("/testing/1/calibrate")
        self.assertIn("INVALID", page)
        self.assertIn("stuck", page)
        r = self.conn.execute("SELECT * FROM test_results").fetchone()
        self.assertEqual(r["verdict"], "INVALID")
        self.assertIn("stuck", r["invalid_reasons"])
        masks = [json.loads(e[0])["mask"] for e in self.conn.execute("SELECT detail_json FROM io_events")]
        self.assertEqual(masks[-1] & 0b111, 0, "INVALID closes no result line")

    def test_changing_part_needs_a_new_calibration(self):
        self.start()
        self.calibrate()
        self.post("/testing/select/1")
        self.assertIn("Calibrate this part first", self.post("/testing/1/run"))



@unittest.skipUnless(HAVE_DEPS, "flask or scikit-learn is not installed")
class ReportTests(unittest.TestCase):
    def test_yield_retests_pareto_shifts_and_csv(self):
        from eol_app.report_views import build, shift_of
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        db_path = Path(d.name) / "eol.sqlite3"
        app = create_app(db_path)
        conn = dbm.connect(db_path)
        self.addCleanup(conn.close)
        now = dbm.utcnow()
        conn.execute("INSERT INTO parts (id, part_number, capture_s, created_utc) VALUES (1, 'P1', 5, ?)", (now,))
        conn.execute("INSERT INTO failure_modes (id, part_id, name, created_utc) VALUES (1, 1, 'squeal', ?), (2, 1, 'rattle', ?)", (now, now))
        rows = [("U1", "OK", None, None, 0), ("U2", "NOK", 1, None, 0), ("U2", "OK", None, 2, 0), ("U3", "NOK", 1, None, 0),
                ("U4", "NOK", 2, None, 0), ("U5", "UNDEFINED", None, None, 0), ("U6", "OK", None, None, 1), (None, "INVALID", None, None, 0)]
        for i, (unit, verdict, mode, retest, drift) in enumerate(rows, start=1):
            conn.execute("INSERT INTO test_results (id, at_utc, part_id, unit_serial, verdict, failure_mode_id, retest_of, drift, user_id)"
                         " VALUES (?, ?, 1, ?, ?, ?, ?, ?, 1)", (i, now, unit, verdict, mode, retest, drift))
        r = build(conn, now[:10] + "T00:00:00Z", now[:10] + "T23:59:59Z", None)
        self.assertEqual(r["total"], 8)
        self.assertEqual(r["units"], 7)  # U1..U6, and the INVALID one without a serial
        self.assertAlmostEqual(r["fpy"], 2 / 7)  # first results OK: U1 and U6
        self.assertEqual(r["retests"], 1)
        self.assertEqual(r["pareto_nok"], [("squeal", 2), ("rattle", 1)])
        self.assertEqual(len(r["undefined"]), 1)
        self.assertEqual(len(r["drift"]), 1)
        shifts = [{"name": "A", "start_local": "06:00", "end_local": "14:00"}, {"name": "N", "start_local": "22:00", "end_local": "06:00"}]
        with mock.patch("eol_app.report_views._local_hhmm", return_value="23:30"):
            self.assertEqual(shift_of(now, shifts), "N")
        with mock.patch("eol_app.report_views._local_hhmm", return_value="07:00"):
            self.assertEqual(shift_of(now, shifts), "A")
        c = app.test_client()
        c.post("/login", data={"username": "master", "password": dbm.MASTER_DEFAULT_PASSWORD})
        conn.execute("UPDATE users SET must_change_password = 0")
        page = c.get("/reports").get_data(as_text=True)
        self.assertIn("28.6 %", page)
        csv_text = c.get("/reports/results.csv").get_data(as_text=True)
        self.assertEqual(len(csv_text.strip().splitlines()), 9)
        self.assertIn("squeal", csv_text)

if __name__ == "__main__":
    unittest.main()
