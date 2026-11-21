"""System tests of the link, run against two stand-ins for the Pico:

  - the Python simulator (simulator/pico_sim), and
  - the firmware core itself, compiled for a PC (firmware/host/core_host.c).

Both get exactly the same checks. The C core needs the path of its binary in the
environment variable EOL_CORE_HOST; scripts/selftest.sh builds it and sets that.

No Pico is involved. See scripts/selftest.sh --hardware for the tests with a real one.
"""

import os
import subprocess
import tempfile
import threading
import time
import unittest

from eol_link import protocol as p
from eol_link.client import CommandFailed, PicoLink, VersionMismatch
from eol_link.heartbeat import APP_DEADLINE_S, AppLiveness, HeartbeatDaemon
from eol_link.linktest import LinkTest, run_linktest
from eol_link.transport import Port
from pico_sim import Faults, PicoSim

HB_TIMEOUT_S = 0.5
CORE_HOST = os.environ.get("EOL_CORE_HOST")


class PySimDevice:
    def __init__(self, **kwargs):
        self._sim = PicoSim(**kwargs).start()
        self.data_path, self.hb_path = self._sim.data_path, self._sim.hb_path

    def outputs(self) -> int:
        return self._sim.outputs

    def close(self):
        self._sim.stop()


class CCoreDevice:
    def __init__(self):
        self._proc = subprocess.Popen([CORE_HOST], stdout=subprocess.PIPE, text=True)
        tag, self.data_path, self.hb_path = self._proc.stdout.readline().split()
        assert tag == "PORTS"
        self._outputs = 0
        threading.Thread(target=self._follow, daemon=True).start()

    def _follow(self):
        for line in self._proc.stdout:
            tag, _, mask = line.split()
            if tag == "OUT":
                self._outputs = int(mask)

    def outputs(self) -> int:
        return self._outputs

    def close(self):
        self._proc.kill()
        self._proc.wait()
        self._proc.stdout.close()


class Heartbeat:
    """Runs the real heartbeat process code in a thread, with an application check-in we can stop."""

    def __init__(self, hb_path: str):
        self._dir = tempfile.TemporaryDirectory()
        sock = os.path.join(self._dir.name, "hb.sock")
        self.daemon = HeartbeatDaemon(hb_path, sock)
        self._thread = threading.Thread(target=self.daemon.run, daemon=True)
        self._thread.start()
        while not os.path.exists(sock):
            time.sleep(0.01)
        self._app = AppLiveness(sock)
        self.app_alive = True
        self._closing = False
        self._app_thread = threading.Thread(target=self._check_in, daemon=True)
        self._app_thread.start()

    def _check_in(self):
        while not self._closing:
            if self.app_alive:
                self._app.beat()
            time.sleep(0.1)

    def stop_daemon(self):
        self.daemon.stop()
        self._thread.join()

    def close(self):
        self._closing = True
        self._app_thread.join()
        self.stop_daemon()
        self._app.close()
        self._dir.cleanup()


def wait_for(condition, timeout: float) -> float:
    """Waits until condition() is true. Returns how long it took; fails the test on timeout."""
    start = time.monotonic()
    while not condition():
        if time.monotonic() - start > timeout:
            raise AssertionError(f"condition not met within {timeout} s")
        time.sleep(0.005)
    return time.monotonic() - start


class LinkScenarios:
    """Checks that apply to anything that claims to be a Pico front end."""

    def make_device(self):
        raise NotImplementedError

    def setUp(self):
        self.dev = self.make_device()
        self.addCleanup(self.dev.close)
        self.port = Port(self.dev.data_path)
        self.addCleanup(self.port.close)
        self.link = PicoLink(self.port)

    def healthy_link(self) -> Heartbeat:
        hb = Heartbeat(self.dev.hb_path)
        self.addCleanup(hb.close)
        self.link.hello()
        wait_for(lambda: hb.daemon.pico_healthy, 2.0)
        return hb

    def assert_refused(self, status: int, ftype: int, payload: bytes = b""):
        with self.assertRaises(CommandFailed) as caught:
            self.link.command(ftype, payload)
        self.assertEqual(p.STATUS_NAMES[caught.exception.status], p.STATUS_NAMES[status])

    # ---- handshake ----

    def test_handshake_reports_the_agreed_format(self):
        info = self.link.hello()
        self.assertEqual((info.proto_major, info.proto_minor), (p.PROTO_MAJOR, p.PROTO_MINOR))
        self.assertEqual((info.pdm_payload, info.tach_payload), (4000, 250))
        self.assertEqual(info.hb_timeout_ms, 500)
        self.assertEqual(info.sys_clk_hz, 128_000_000)

    def test_nothing_runs_before_the_handshake(self):
        self.assert_refused(p.ST_BAD_VERSION, p.T_STREAM_START, p.pack_stream_start(p.SRC_SYNTH, p.MODE_REAL, 1, 100))
        self.assert_refused(p.ST_BAD_VERSION, p.T_SET_OUTPUTS, p.pack_set_outputs(p.OUT_READY))

    def test_a_host_with_another_major_version_is_refused_by_the_device(self):
        self.assert_refused(p.ST_BAD_VERSION, p.T_HELLO, p.pack_hello(major=p.PROTO_MAJOR + 1))
        self.assert_refused(p.ST_BAD_VERSION, p.T_STREAM_START, p.pack_stream_start(p.SRC_SYNTH, p.MODE_REAL, 1, 100))

    # ---- stream ----

    def test_real_rate_stream_arrives_complete_and_correct(self):
        self.link.hello()
        result = run_linktest(self.link, 2.0)
        self.assertEqual(result.failures, [])
        self.assertEqual(result.frames, {p.T_PDM: 200, p.T_TACH: 200})
        self.assertEqual(result.pico_generated, 200)
        self.assertEqual(result.pico_dropped, 0)

    def test_maximum_rate_stream_loses_nothing(self):
        self.link.hello()
        result = run_linktest(self.link, 1.0, p.MODE_MAX, min_max_rate=0)
        self.assertEqual(result.failures, [])
        self.assertEqual(result.frames[p.T_PDM], result.pico_generated)
        self.assertGreater(result.rate_bytes_per_s, p.REAL_RATE_BYTES_PER_S)

    def test_a_stalled_reader_causes_loss_that_is_counted_and_reported(self):
        self.link.hello()
        test = LinkTest(self.link)
        test.start(3.0)
        time.sleep(2.0)  # do not read: the device's 64 kB buffer overflows
        result = test.finish()
        self.assertTrue(result.ended)
        self.assertGreater(result.seq_gaps, 0)
        self.assertGreater(result.gap_flags, 0)
        self.assertGreater(result.overflow_events, 0)
        # The device's own count of dropped frames matches what we found missing.
        self.assertEqual(result.pico_dropped, result.seq_gaps)
        self.assertEqual(result.frames[p.T_PDM] + result.frames[p.T_TACH] + result.seq_gaps, 2 * result.pico_generated)
        self.assertEqual((result.content_errors, result.crc_errors), (0, 0))
        self.assertFalse(result.passed)

    def test_stream_arguments_are_checked(self):
        self.link.hello()
        self.assert_refused(p.ST_NOT_IMPLEMENTED, p.T_STREAM_START, p.pack_stream_start(p.SRC_MIC, p.MODE_REAL, 1, 100))
        self.assert_refused(p.ST_BAD_ARG, p.T_STREAM_START, p.pack_stream_start(7, p.MODE_REAL, 1, 100))
        self.assert_refused(p.ST_BAD_ARG, p.T_STREAM_START, p.pack_stream_start(p.SRC_SYNTH, 2, 1, 100))
        self.link.command(p.T_STREAM_START, p.pack_stream_start(p.SRC_SYNTH, p.MODE_REAL, 1, 0))
        self.assert_refused(p.ST_BUSY, p.T_STREAM_START, p.pack_stream_start(p.SRC_SYNTH, p.MODE_REAL, 1, 0))
        self.link.command(p.T_STREAM_STOP)

    # ---- outputs and watchdog ----

    def test_outputs_are_off_and_refused_without_a_heartbeat(self):
        self.link.hello()
        self.assertEqual(self.dev.outputs(), 0)
        self.assert_refused(p.ST_SAFE_STATE, p.T_SET_OUTPUTS, p.pack_set_outputs(p.OUT_OK))
        self.assertEqual(self.dev.outputs(), 0)

    def test_outputs_follow_commands_while_healthy_and_watchdog_toggles(self):
        self.healthy_link()
        self.link.set_outputs(p.OUT_OK | p.OUT_READY)
        wait_for(lambda: self.dev.outputs() & 0x1F == p.OUT_OK | p.OUT_READY, 0.5)
        seen = set()
        end = time.monotonic() + 1.3
        while time.monotonic() < end:
            seen.add(self.dev.outputs() & p.OUT_WATCHDOG)
            time.sleep(0.01)
        self.assertEqual(seen, {0, p.OUT_WATCHDOG})

    def test_contradictory_or_reserved_outputs_are_refused(self):
        self.healthy_link()
        self.assert_refused(p.ST_BAD_ARG, p.T_SET_OUTPUTS, p.pack_set_outputs(p.OUT_OK | p.OUT_NOK))
        self.assert_refused(p.ST_BAD_ARG, p.T_SET_OUTPUTS, p.pack_set_outputs(p.OUT_NOK | p.OUT_UNDEFINED))
        self.assert_refused(p.ST_BAD_ARG, p.T_SET_OUTPUTS, p.pack_set_outputs(p.OUT_WATCHDOG))
        self.assertEqual(self.dev.outputs() & 0x1F, 0)

    def test_all_outputs_drop_within_the_timeout_when_heartbeats_stop(self):
        hb = self.healthy_link()
        self.link.set_outputs(p.OUT_OK | p.OUT_READY)
        wait_for(lambda: self.dev.outputs() & p.OUT_OK, 0.5)
        hb.stop_daemon()
        took = wait_for(lambda: self.dev.outputs() == 0, HB_TIMEOUT_S + 0.3)
        self.assertLess(took, HB_TIMEOUT_S + 0.3)
        time.sleep(1.2)
        self.assertEqual(self.dev.outputs(), 0, "the watchdog line must be static after a trip")
        self.assert_refused(p.ST_SAFE_STATE, p.T_SET_OUTPUTS, p.pack_set_outputs(p.OUT_OK))

    def test_outputs_drop_when_the_application_stops_checking_in_and_do_not_come_back(self):
        hb = self.healthy_link()
        self.link.set_outputs(p.OUT_OK | p.OUT_READY)
        wait_for(lambda: self.dev.outputs() & p.OUT_OK, 0.5)

        hb.app_alive = False  # the heartbeat process keeps running; the application has "hung"
        took = wait_for(lambda: self.dev.outputs() == 0, APP_DEADLINE_S + 0.6)
        self.assertGreater(took, APP_DEADLINE_S - 0.3)

        hb.app_alive = True
        wait_for(lambda: hb.daemon.pico_healthy, 1.0)
        time.sleep(0.3)
        self.assertEqual(self.dev.outputs() & 0x1F, 0, "a result from before the trip must not reappear")
        self.link.set_outputs(p.OUT_READY)
        wait_for(lambda: self.dev.outputs() & 0x1F == p.OUT_READY, 0.5)


class PythonSimulatorTests(LinkScenarios, unittest.TestCase):
    def make_device(self):
        return PySimDevice()


@unittest.skipUnless(CORE_HOST, "EOL_CORE_HOST not set: the C firmware core was not built")
class FirmwareCoreTests(LinkScenarios, unittest.TestCase):
    def make_device(self):
        return CCoreDevice()


class ReceiverCatchesFaultsTests(unittest.TestCase):
    """The simulator misbehaves on purpose; the receiver must notice every time."""

    def run_with(self, **sim_kwargs):
        dev = PySimDevice(**sim_kwargs)
        self.addCleanup(dev.close)
        port = Port(dev.data_path)
        self.addCleanup(port.close)
        link = PicoLink(port)
        link.hello()
        return run_linktest(link, 0.5)

    def test_clean_run_passes(self):
        self.assertTrue(self.run_with().passed)

    def test_a_missing_frame_is_caught(self):
        result = self.run_with(faults=Faults(drop_pdm={20}))
        self.assertEqual((result.seq_gaps, result.gap_flags), (1, 1))
        self.assertFalse(result.passed)

    def test_a_missing_last_frame_is_caught(self):
        result = self.run_with(faults=Faults(drop_pdm={49}))
        self.assertEqual(result.seq_gaps, 1)
        self.assertFalse(result.passed)

    def test_a_bit_flipped_on_the_wire_is_caught(self):
        result = self.run_with(faults=Faults(corrupt_pdm={20}))
        self.assertEqual((result.crc_errors, result.seq_gaps), (1, 1))
        self.assertFalse(result.passed)

    def test_wrong_data_with_a_valid_crc_is_caught(self):
        result = self.run_with(faults=Faults(wrong_data_pdm={20}))
        self.assertEqual((result.content_errors, result.crc_errors, result.seq_gaps), (1, 0, 0))
        self.assertFalse(result.passed)

    def test_a_pico_with_another_major_version_is_refused(self):
        dev = PySimDevice(proto_major=p.PROTO_MAJOR + 1)
        self.addCleanup(dev.close)
        with Port(dev.data_path) as port:
            link = PicoLink(port)
            with self.assertRaises(VersionMismatch):
                link.hello()
            self.assertEqual(link.info.proto_major, p.PROTO_MAJOR + 1)


if __name__ == "__main__":
    unittest.main()
