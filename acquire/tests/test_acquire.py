"""Tests of the acquisition service and the recording store, against the Python Pico simulator.

Need numpy and scipy (the INVALID checks use the decimation filter)."""

import json
import math
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    import numpy as np
    import scipy  # noqa: F401

    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

from eol_link import protocol as p
from eol_link.capture import MicCapture
from eol_link.recording import verify_recording
from pico_sim import PicoSim, micsig

if HAVE_DEPS:
    from eol_acquire import store as store_mod
    from eol_acquire.client import AcquireClient, AcquireError
    from eol_acquire.evaluate import Limits, evaluate
    from eol_acquire.service import AcquisitionService
    from eol_acquire.store import RecordingStore, StoreError

CLOCK = 3_200_000
INFO = p.Info(1, 1, "0.3.0", 1, 500, 128_000_000, 128_000, "db6511d14d774692", 4000, 250, 0xB, "test")


def make_capture(raw: bytes, verdict: int = p.MIC_SELECT_GND, **kw) -> "MicCapture":
    """A capture as capture_mic would return it, without a Pico."""
    status = p.MicStatus(verdict, p.MIC_USE_L, False, 0, CLOCK, 100_000, 50_000, 0, 0, 0, 25_000, 25_000)
    n = len(raw) // p.PDM_PAYLOAD
    cap = MicCapture(len(raw) * 8 / CLOCK, 0.5, CLOCK, "2026-11-22T10:00:00Z", INFO, raw=raw, status=status,
                     first_t_us=1_000_000, frames=n + 50, preroll_frames=50, pico_generated=n + 50,
                     pico_dropped=0, ended=True)
    for k, v in kw.items():
        setattr(cap, k, v)
    return cap


def one_bit(x):
    v1 = v2 = 0.0
    bits = np.empty(len(x), dtype=np.uint8)
    for n, xn in enumerate(x.tolist()):
        y = 1.0 if v2 >= 0 else -1.0
        bits[n] = y > 0
        v1 += xn - y
        v2 += v1 - y
    return np.packbits(bits).tobytes()


@unittest.skipUnless(HAVE_DEPS, "numpy or scipy is not installed")
class EvaluateTests(unittest.TestCase):
    def test_a_healthy_stream_is_valid_and_its_levels_are_reported(self):
        ev = evaluate(make_capture(micsig.stream(0, 400_000)))
        self.assertEqual(ev.reasons, [])
        self.assertTrue(ev.valid)
        self.assertAlmostEqual(ev.ones_fraction, 0.5, delta=0.01)
        self.assertLess(ev.peak_dbfs, -3)
        self.assertEqual(ev.filter_name, "eol-pdm3200k-d16-p40k-s120k-a100-r1")
        self.assertEqual(len(ev.bands), 3)

    def test_each_check_names_its_reason(self):
        cases = {
            "stuck": make_capture(bytes(400_000)),
            "run of": make_capture(micsig.stream(0, 200_000) + bytes(8) + micsig.stream(0, 200_000)),
            "frames missing": make_capture(micsig.stream(0, 400_000), seq_gaps=3),
            "probe": make_capture(micsig.stream(0, 400_000), verdict=p.MIC_STUCK),
            "CRC errors": make_capture(micsig.stream(0, 400_000), crc_errors=1),
            "never finished": make_capture(micsig.stream(0, 400_000), ended=False),
        }
        for word, cap in cases.items():
            with self.subTest(word):
                ev = evaluate(cap)
                self.assertFalse(ev.valid)
                self.assertIn(word, " ".join(ev.reasons))

    def test_a_peak_above_the_limit_is_invalid(self):
        n = np.arange(160_000)
        loud = one_bit(0.8 * np.sin(2 * np.pi * 2_000 * n / CLOCK))  # peak -1.9 dBFS
        ev = evaluate(make_capture(loud))
        self.assertAlmostEqual(ev.peak_dbfs, 20 * math.log10(0.8), delta=0.3)
        self.assertIn("peak", " ".join(ev.reasons))
        quieter = one_bit(0.5 * np.sin(2 * np.pi * 2_000 * n / CLOCK))  # peak -6 dBFS
        self.assertTrue(evaluate(make_capture(quieter)).valid)
        # The limit is a setting, and the evaluation records the one it used.
        ev = evaluate(make_capture(loud), Limits(peak_max_dbfs=0.0))
        self.assertNotIn("peak", " ".join(ev.reasons))
        self.assertEqual(ev.to_dict()["limits"]["peak_max_dbfs"], 0.0)

    def test_without_data_nothing_is_valid(self):
        self.assertFalse(evaluate(make_capture(b"")).valid)


@unittest.skipUnless(HAVE_DEPS, "numpy or scipy is not installed")
class StoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = RecordingStore(self._dir.name)
        self.cap = make_capture(micsig.stream(0, 400_000))
        self.ev = evaluate(self.cap)

    def save(self, cap=None, ev=None, **kw):
        kw.setdefault("purpose", "golden")
        kw.setdefault("microphone_id", "KAS-700-0164 bench 1")
        return self.store.save(cap or self.cap, ev or self.ev, **kw)

    def test_a_saved_recording_is_complete_read_only_and_verifiable(self):
        rec = self.save(part="BRK-1234", note="first golden")
        pdm = Path(rec["path"])
        desc = pdm.with_suffix(".json")
        self.assertEqual(pdm.read_bytes(), self.cap.raw)
        meta = json.loads(desc.read_text())
        self.assertEqual(meta["sha256"], rec["sha256"])
        for key in ("microphone_id", "pico", "filter", "software", "select_probe", "evaluation", "captured_utc"):
            self.assertIn(key, meta)
        self.assertEqual(meta["pico"]["firmware"], "0.3.0")
        self.assertEqual(meta["filter"]["name"], "eol-pdm3200k-d16-p40k-s120k-a100-r1")
        for f in (pdm, desc):
            self.assertEqual(stat.S_IMODE(f.stat().st_mode) & 0o222, 0, f"{f.name} must be read-only")
        self.assertIsNone(verify_recording(desc))
        report = self.store.verify()
        self.assertEqual((report.intact, report.damaged, report.unfinished), ([rec["id"]], [], []))
        self.assertEqual([r["id"] for r in self.store.list()], [rec["id"]])
        self.assertEqual(list(pdm.parent.parent.parent.parts[-1:]), ["recordings"])

    def test_test_and_production_parts_can_never_be_stored(self):
        for purpose in ("test", "production", "", "Golden"):
            with self.subTest(purpose=purpose):
                with self.assertRaisesRegex(StoreError, "never stored"):
                    self.save(purpose=purpose)
        self.assertEqual(self.store.list(), [])

    def test_an_invalid_capture_is_refused_as_a_calibrated_sample_but_kept_for_the_bench(self):
        cap = make_capture(bytes(400_000))
        ev = evaluate(cap)
        for purpose, extra in (("golden", {}), ("failure_mode", {"failure_mode": "squeal"})):
            with self.subTest(purpose):
                with self.assertRaisesRegex(StoreError, "INVALID"):
                    self.save(cap, ev, purpose=purpose, **extra)
        rec = self.save(cap, ev, purpose="bench", note="stuck line, for the record")
        self.assertFalse(json.loads(Path(rec["path"]).with_suffix(".json").read_text())["evaluation"]["valid"])

    def test_names_follow_the_setup_rules(self):
        self.save(purpose="failure_mode", failure_mode="Squeal (front) 2/3", part="BRK-1234_A.1")
        for kw, word in (({"purpose": "failure_mode"}, "required"),
                         ({"purpose": "failure_mode", "failure_mode": "x" * 31}, "up to 30"),
                         ({"purpose": "failure_mode", "failure_mode": "bad;name"}, "letters"),
                         ({"part": "P" * 21}, "up to 20"),
                         ({"failure_mode": "squeal"}, "only goes with"),
                         ({"microphone_id": ""}, "microphone identity"),
                         ({"note": "n" * 201}, "up to 200")):
            with self.subTest(kw=kw):
                with self.assertRaisesRegex(StoreError, word):
                    self.save(**kw)

    def test_a_full_disk_is_refused_before_anything_is_written(self):
        with mock.patch.object(store_mod.shutil, "disk_usage", return_value=mock.Mock(free=100 * 2**20)):
            with self.assertRaisesRegex(StoreError, "disk space"):
                self.save()
        self.assertEqual(self.store.verify().intact, [])

    def test_verify_finds_changed_missing_and_unfinished_recordings(self):
        changed, missing, fine = self.save(), self.save(), self.save()
        path = Path(changed["path"])
        os.chmod(path, 0o644)
        data = bytearray(path.read_bytes())
        data[1000] ^= 0x01
        path.write_bytes(bytes(data))
        Path(missing["path"]).unlink()
        # An interrupted save leaves data without a description.
        orphan = Path(fine["path"]).with_name("rec-20261122T100000Z-dead00.pdm")
        orphan.write_bytes(b"\x55" * 100)
        report = self.store.verify()
        self.assertEqual(report.intact, [fine["id"]])
        reasons = dict(report.damaged)
        self.assertIn("SHA-256", reasons[changed["id"]])
        self.assertIn("missing", reasons[missing["id"]])
        self.assertEqual(report.unfinished, [orphan.stem])
        self.assertFalse(report.ok)

    def test_a_save_interrupted_after_the_data_leaves_no_recording_that_looks_complete(self):
        import eol_link.recording as rec_mod

        real = rec_mod._write_atomic
        calls = []

        def crash_on_description(path, data):
            calls.append(path.suffix)
            if path.suffix == ".json":
                raise OSError("power cut")
            real(path, data)

        with mock.patch.object(rec_mod, "_write_atomic", crash_on_description):
            with self.assertRaises(OSError):
                self.save()
        self.assertEqual(calls, [".pdm", ".json"], "the data is written first, the description last")
        report = self.store.verify()
        self.assertEqual((report.intact, report.damaged), ([], []))
        self.assertEqual(len(report.unfinished), 1)
        self.assertEqual(self.store.list(), [])


@unittest.skipUnless(HAVE_DEPS, "numpy or scipy is not installed")
class ServiceTests(unittest.TestCase):
    """The service against the Python simulator: the "simulated capture" of build-plan step 4."""

    def start(self, **sim_kwargs):
        sim = PicoSim(**sim_kwargs).start()
        self.addCleanup(sim.stop)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.store = RecordingStore(d.name)
        self.service = AcquisitionService(self.store, microphone_id="sim mic", data_port=sim.data_path)
        return self.service

    def ok(self, req):
        reply = self.service.handle(req)
        self.assertTrue(reply["ok"], reply.get("error"))
        return reply

    def refused(self, req, word):
        reply = self.service.handle(req)
        self.assertFalse(reply["ok"])
        self.assertIn(word, reply["error"])

    def test_capture_save_and_verify(self):
        self.start()
        cap = self.ok({"cmd": "capture", "seconds": 1})["capture"]
        self.assertTrue(cap["valid"], cap["reasons"])
        self.assertAlmostEqual(cap["seconds"], 1.0, places=6)
        self.assertEqual(cap["frames"], 150)  # 0.5 s pre-roll plus 1 s, 10 ms each
        self.assertEqual(cap["probe"], p.MIC_VERDICT_NAMES[p.MIC_SELECT_GND])
        rec = self.ok({"cmd": "save", "capture_id": cap["capture_id"], "purpose": "golden", "part": "SIM-1"})["recording"]
        self.assertEqual(rec["bytes"], 400_000)
        # The recording is the simulated signal after the pre-roll, bit for bit.
        self.assertEqual(Path(rec["path"]).read_bytes(), micsig.stream(50 * p.PDM_PAYLOAD, 400_000))
        self.assertTrue(self.store.verify().ok)
        self.assertEqual(self.ok({"cmd": "status"})["recordings"], 1)

    def test_only_the_waiting_capture_can_be_saved_once(self):
        self.start()
        first = self.ok({"cmd": "capture", "seconds": 0.5})["capture"]["capture_id"]
        second = self.ok({"cmd": "capture", "seconds": 0.5})["capture"]["capture_id"]
        self.refused({"cmd": "save", "capture_id": first, "purpose": "bench"}, "only the latest")
        self.ok({"cmd": "discard", "capture_id": second})
        self.refused({"cmd": "save", "capture_id": second, "purpose": "bench"}, "no capture waiting")
        self.assertEqual(self.store.list(), [])

    def test_a_dead_microphone_line_is_invalid_and_cannot_become_a_golden_sample(self):
        self.start(mic="stuck")
        cap = self.ok({"cmd": "capture", "seconds": 0.5})["capture"]
        self.assertFalse(cap["valid"])
        self.assertIn("stuck", " ".join(cap["reasons"]))
        self.refused({"cmd": "save", "capture_id": cap["capture_id"], "purpose": "golden"}, "INVALID")
        # The Pico streams nothing from a dead line, so there is nothing to keep even for the bench.
        self.refused({"cmd": "save", "capture_id": cap["capture_id"], "purpose": "bench"}, "no data")

    def test_bad_requests_are_refused_with_a_reason(self):
        self.start()
        self.refused({"cmd": "capture", "seconds": 0}, "seconds")
        self.refused({"cmd": "capture", "seconds": 61}, "seconds")
        self.refused({"cmd": "capture", "seconds": "5"}, "seconds")
        self.refused({"cmd": "capture", "seconds": 1, "clock_mhz": 2.0}, "clock_mhz")
        self.refused({"cmd": "explode"}, "unknown command")
        self.refused({"cmd": "discard", "capture_id": "x"}, "no capture waiting")

    def test_firmware_without_capture_and_a_missing_pico_are_reported(self):
        self.start(mic=None)
        self.refused({"cmd": "capture", "seconds": 1}, "no microphone capture")
        self.service.data_port = "/dev/does-not-exist"
        self.refused({"cmd": "capture", "seconds": 1}, "Pico link")

    def test_over_the_socket(self):
        service = self.start()
        sock = Path(tempfile.mkdtemp(dir="/tmp")) / "acquire.sock"
        self.addCleanup(lambda: sock.parent.rmdir() if sock.parent.exists() and not any(sock.parent.iterdir()) else None)
        threading.Thread(target=service.serve, args=(sock,), daemon=True).start()
        deadline = time.monotonic() + 5
        while not sock.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.addCleanup(service.shutdown)
        client = AcquireClient(sock)
        self.assertEqual(client.status()["microphone_id"], "sim mic")
        cap = client.capture(0.5)
        rec = client.save(cap["capture_id"], "failure_mode", failure_mode="rattle")
        self.assertTrue(rec["id"].startswith("rec-"))
        with self.assertRaisesRegex(AcquireError, "no capture waiting"):
            client.discard(cap["capture_id"])
        with self.assertRaisesRegex(AcquireError, "not running"):
            AcquireClient(sock.with_name("nothing.sock")).status()


if __name__ == "__main__":
    unittest.main()
