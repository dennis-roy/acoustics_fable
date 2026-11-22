"""System tests of the link, run against two stand-ins for the Pico:

  - the Python simulator (simulator/pico_sim), and
  - the firmware core itself, compiled for a PC (firmware/host/core_host.c).

Both get exactly the same checks. The C core needs the path of its binary in the
environment variable EOL_CORE_HOST; scripts/selftest.sh builds it and sets that.

No Pico is involved. See scripts/selftest.sh --hardware for the tests with a real one.
"""

import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest

from eol_link import protocol as p
from eol_link.client import CommandFailed, PicoLink, VersionMismatch
from eol_link.heartbeat import APP_DEADLINE_S, AppLiveness, HeartbeatDaemon
from eol_link.linktest import LinkTest, run_linktest
from eol_link.mictest import run_mictest
from eol_link.pdmspec import have_numpy
from eol_link.transport import Port
from pico_sim import Faults, PicoSim, micsig

HB_TIMEOUT_S = 0.5
CORE_HOST = os.environ.get("EOL_CORE_HOST")


class PySimDevice:
    def __init__(self, **kwargs):
        self._sim = PicoSim(**kwargs).start()
        self.data_path, self.hb_path = self._sim.data_path, self._sim.hb_path

    def outputs(self) -> int:
        return self._sim.outputs

    def rebooted(self) -> bool:
        return self._sim.rebooted

    def mic_clock_log(self) -> list[int]:
        return list(self._sim.mic_clock_log)

    def stall(self, seconds: float):
        self._sim.stall(seconds)

    def close(self):
        self._sim.stop()


class CCoreDevice:
    def __init__(self, mic="gnd", mic_release="low"):
        env = dict(os.environ, EOL_SIM_MIC=mic or "absent", EOL_SIM_RELEASE=mic_release)
        self._proc = subprocess.Popen([CORE_HOST], stdout=subprocess.PIPE, text=True, env=env)
        tag, self.data_path, self.hb_path = self._proc.stdout.readline().split()
        assert tag == "PORTS"
        self._outputs = 0
        self._rebooted = False
        self._mic_clock_log = []
        threading.Thread(target=self._follow, daemon=True).start()

    def _follow(self):
        for line in self._proc.stdout:
            words = line.split()
            if words[0] == "OUT":
                self._outputs = int(words[2])
            elif words[0] == "MIC":
                self._mic_clock_log.append(int(words[1]))
            elif words[0] == "REBOOT":
                self._rebooted = True

    def outputs(self) -> int:
        return self._outputs

    def rebooted(self) -> bool:
        return self._rebooted

    def mic_clock_log(self) -> list[int]:
        return list(self._mic_clock_log)

    def stall(self, seconds: float):
        self._proc.send_signal(signal.SIGSTOP)
        time.sleep(seconds)
        self._proc.send_signal(signal.SIGCONT)

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


def mic_capture(link: PicoLink, ms: int, **start) -> tuple[p.MicStatus | None, list[p.Frame], int | None]:
    """One microphone stream of `ms` milliseconds: (probe result, PDM frames, frames the device says it made)."""
    link.command(p.T_STREAM_START, p.pack_stream_start(p.SRC_MIC, p.MODE_REAL, 0, ms, **start))
    status, frames = None, []
    for f in link.frames(timeout=2.0):
        if f.type == p.T_MIC_STATUS:
            status = p.unpack_mic_status(f.payload)
        elif f.type == p.T_PDM:
            frames.append(f)
        elif f.type == p.T_EVENT and p.unpack_event(f.payload)[0] == p.EV_STREAM_STOPPED:
            return status, frames, p.unpack_event(f.payload)[1]
    return status, frames, None


def assert_is_the_simulated_signal(test: unittest.TestCase, frames: list[p.Frame]) -> None:
    """Every bit of the frames is the documented simulated microphone signal, in order.

    The signal repeats, and a device may start anywhere in it, so the first frame says where."""
    got = b"".join(f.payload for f in frames)
    twice = micsig.block() * 2
    start = twice.find(got[:64])
    test.assertGreaterEqual(start, 0, "the first frame is not a piece of the simulated signal")
    # Stretches of the signal look alike, so the first place that fits may be the wrong one.
    while 0 <= start < micsig.BLOCK_BYTES and micsig.stream(start, len(got)) != got:
        start = twice.find(got[:64], start + 1)
    test.assertTrue(0 <= start < micsig.BLOCK_BYTES, "the frames are not the simulated signal, bit for bit")


class LinkScenarios:
    """Checks that apply to anything that claims to be a Pico front end."""

    def make_device(self, **mic):
        raise NotImplementedError

    def setUp(self):
        self.dev = self.make_device()
        self.addCleanup(self.dev.close)
        self.port = Port(self.dev.data_path)
        self.addCleanup(self.port.close)
        self.link = PicoLink(self.port)

    def another_device(self, **mic) -> PicoLink:
        """A device with another simulated microphone, handshake done."""
        dev = self.make_device(**mic)
        self.addCleanup(dev.close)
        port = Port(dev.data_path)
        self.addCleanup(port.close)
        link = PicoLink(port)
        link.hello()
        return link

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
        self.assertTrue(info.features & p.FEATURE_MIC)

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
        self.assert_refused(p.ST_BAD_ARG, p.T_STREAM_START, p.pack_stream_start(7, p.MODE_REAL, 1, 100))
        self.assert_refused(p.ST_BAD_ARG, p.T_STREAM_START, p.pack_stream_start(p.SRC_SYNTH, 2, 1, 100))
        self.link.command(p.T_STREAM_START, p.pack_stream_start(p.SRC_SYNTH, p.MODE_REAL, 1, 0))
        self.assert_refused(p.ST_BUSY, p.T_STREAM_START, p.pack_stream_start(p.SRC_SYNTH, p.MODE_REAL, 1, 0))
        self.link.command(p.T_STREAM_STOP)

    # ---- microphone ----

    def test_microphone_stream_is_found_and_arrives_bit_for_bit(self):
        self.link.hello()
        status, frames, generated = mic_capture(self.link, 300)
        self.assertEqual((status.verdict, status.use, status.forced), (p.MIC_SELECT_GND, p.MIC_USE_L, False))
        self.assertEqual((status.clock_hz, status.periods >= p.MIC_PROBE_PERIODS), (3_200_000, True))
        self.assertEqual([f.seq for f in frames], list(range(30)))
        self.assertEqual(generated, 30)
        self.assertFalse(any(f.flags for f in frames), "microphone frames carry no SYNTH or GAP flag")
        self.assertEqual({b.t_us - a.t_us for a, b in zip(frames, frames[1:])}, {10_000})
        assert_is_the_simulated_signal(self, frames)
        # The clock: wake-up rate first, never straight to the ultrasonic rate, and off afterwards.
        wait_for(lambda: self.dev.mic_clock_log()[-1:] == [0], 0.5)
        self.assertEqual(self.dev.mic_clock_log(), [2_000_000, 3_200_000, 0])

    def test_the_probe_finds_select_either_way_whatever_the_released_line_does(self):
        for mic, verdict, use in (("gnd", p.MIC_SELECT_GND, p.MIC_USE_L), ("vdd", p.MIC_SELECT_VDD, p.MIC_USE_H)):
            for release in ("low", "hold", "high"):
                with self.subTest(select=mic, released_line=release):
                    link = self.another_device(mic=mic, mic_release=release)
                    status, frames, generated = mic_capture(link, 100)
                    self.assertEqual((status.verdict, status.use), (verdict, use))
                    self.assertEqual((len(frames), generated), (10, 10))
                    assert_is_the_simulated_signal(self, frames)

    def test_a_dead_microphone_line_is_reported_and_nothing_is_streamed(self):
        link = self.another_device(mic="stuck")
        status, frames, generated = mic_capture(link, 300)
        self.assertEqual((status.verdict, status.use), (p.MIC_STUCK, p.MIC_USE_NONE))
        self.assertEqual((frames, generated), ([], 0))
        result = run_mictest(link, 1.0)
        self.assertFalse(result.passed)
        self.assertIn("stuck", " ".join(result.failures))
        # The device is ready for the next command: nothing was left running.
        self.assertFalse(link.get_stats().state & p.STATE_STREAMING)

    def test_reading_the_wrong_half_on_request_shows_a_dead_line(self):
        self.link.hello()
        status, frames, _ = mic_capture(self.link, 100, select=p.MIC_USE_H)
        self.assertEqual((status.verdict, status.use, status.forced), (p.MIC_SELECT_GND, p.MIC_USE_H, True))
        self.assertEqual(len(frames), 10)
        self.assertEqual(b"".join(f.payload for f in frames), bytes(10 * p.PDM_PAYLOAD))

    def test_the_4_mhz_clock_gives_8_ms_frames(self):
        self.link.hello()
        status, frames, generated = mic_capture(self.link, 200, clock=1)
        self.assertEqual((status.clock_hz, status.clock_code), (4_000_000, 1))
        self.assertEqual((len(frames), generated), (25, 25))
        self.assertEqual({b.t_us - a.t_us for a, b in zip(frames, frames[1:])}, {8_000})

    def test_mictest_passes_on_a_healthy_microphone(self):
        self.link.hello()
        result = run_mictest(self.link, 1.5)
        self.assertEqual(result.failures, [])
        self.assertEqual((result.frames, result.pico_generated, result.pico_dropped), (150, 150, 0))
        self.assertAlmostEqual(result.pdm.ones_fraction, 0.5, delta=0.01)
        if not have_numpy():
            self.skipTest("numpy is not installed: the band levels were not checked")
        audible, ultrasonic, _ = result.bands
        # The simulated signal: square waves at 1 kHz (a quarter of full scale) and 32 kHz (a sixteenth).
        self.assertAlmostEqual(audible.dbfs, -9.0, delta=1.0)
        self.assertAlmostEqual(audible.peak_hz, 1000, delta=60)
        self.assertAlmostEqual(ultrasonic.dbfs, -21.1, delta=1.0)
        self.assertAlmostEqual(ultrasonic.peak_hz, 32_000, delta=60)

    def test_capture_that_falls_behind_is_counted_and_reported(self):
        self.link.hello()
        threading.Timer(0.6, self.dev.stall, [0.15]).start()  # freeze the device mid-stream
        result = run_mictest(self.link, 1.5)
        self.assertTrue(result.ended)
        self.assertGreater(result.overruns, 0)
        self.assertGreater(result.seq_gaps, 0)
        self.assertGreater(result.gap_flags, 0)
        self.assertEqual(result.pico_dropped, result.seq_gaps)
        self.assertEqual(result.frames + result.seq_gaps, result.pico_generated)
        self.assertEqual(result.pico_generated, 150)
        self.assertFalse(result.passed)

    def test_microphone_arguments_are_checked(self):
        self.link.hello()
        start = lambda **kw: p.pack_stream_start(p.SRC_MIC, kw.pop("mode", p.MODE_REAL), 0, 0, **kw)  # noqa: E731
        self.assert_refused(p.ST_BAD_ARG, p.T_STREAM_START, start(mode=p.MODE_MAX))
        self.assert_refused(p.ST_BAD_ARG, p.T_STREAM_START, start(clock=2))
        self.assert_refused(p.ST_BAD_ARG, p.T_STREAM_START, start(select=3))
        self.link.command(p.T_STREAM_START, start())
        self.assert_refused(p.ST_BUSY, p.T_STREAM_START, start())
        self.assert_refused(p.ST_BUSY, p.T_STREAM_START, p.pack_stream_start(p.SRC_SYNTH, p.MODE_REAL, 1, 0))
        self.link.command(p.T_STREAM_STOP)  # still in the wake-up phase: no frames were made
        stopped = [p.unpack_event(f.payload) for f in self.link.frames(0.3) if f.type == p.T_EVENT]
        self.assertIn((p.EV_STREAM_STOPPED, 0), stopped)
        self.assertEqual(self.dev.mic_clock_log(), [2_000_000, 0], "a stopped stream leaves the clock off")

    def test_firmware_without_microphone_capture_says_so(self):
        link = self.another_device(mic=None)
        self.assertFalse(link.info.features & p.FEATURE_MIC)
        with self.assertRaises(CommandFailed) as caught:
            link.command(p.T_STREAM_START, p.pack_stream_start(p.SRC_MIC, p.MODE_REAL, 0, 100))
        self.assertEqual(caught.exception.status, p.ST_NOT_IMPLEMENTED)
        self.assertFalse(run_mictest(link, 1.0).passed)

    # ---- restart into the bootloader ----

    def test_restart_to_bootloader_needs_the_handshake_and_the_magic_word(self):
        self.assert_refused(p.ST_BAD_VERSION, p.T_REBOOT, p.pack_reboot())
        self.link.hello()
        self.assert_refused(p.ST_BAD_ARG, p.T_REBOOT, b"\0\0\0\0")
        self.assert_refused(p.ST_BAD_ARG, p.T_REBOOT)
        self.assertFalse(self.dev.rebooted())
        self.link.reboot_to_bootloader()
        wait_for(self.dev.rebooted, 1.0)

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
    def make_device(self, **mic):
        return PySimDevice(**mic)


@unittest.skipUnless(CORE_HOST, "EOL_CORE_HOST not set: the C firmware core was not built")
class FirmwareCoreTests(LinkScenarios, unittest.TestCase):
    def make_device(self, **mic):
        return CCoreDevice(**mic)


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
