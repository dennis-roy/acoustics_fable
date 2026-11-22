"""Tests of the database and the Setup and Programming screens.

The Setup capture runs end to end: web screen -> acquisition service -> Python Pico simulator
-> recording on disk -> rows in the database."""

import json
import re
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

try:
    import flask  # noqa: F401
    import numpy  # noqa: F401
    import scipy  # noqa: F401

    HAVE_DEPS = True
except ImportError:
    HAVE_DEPS = False

if HAVE_DEPS:
    from eol_acquire.service import AcquisitionService
    from eol_acquire.store import RecordingStore
    from eol_app import db as dbm
    from eol_app.security import hash_password, password_problem, verify_password
    from eol_app.web import create_app
    from pico_sim import PicoSim

NEW_MASTER_PW = "Zynith2026x"


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@unittest.skipUnless(HAVE_DEPS, "flask, numpy or scipy is not installed")
class DatabaseTests(unittest.TestCase):
    def test_migrations_create_every_table_and_are_repeatable(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "eol.sqlite3"
            conn = dbm.open_database(path, hash_password)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            for t in ("users", "roles", "sessions", "audit_log", "settings", "shifts", "stations", "parts", "failure_modes",
                      "sensors", "recordings", "features", "samples", "reference_data", "models", "model_priority",
                      "test_results", "calibration_checks", "drift_events", "io_events", "edge_outbox", "context_events"):
                self.assertIn(t, tables)
            self.assertEqual(dbm.schema_version(conn), max(n for n, _ in dbm.migrations()))
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            conn.close()
            conn = dbm.open_database(path, hash_password)  # second start: nothing doubled
            self.addCleanup(conn.close)
            self.assertEqual(conn.execute("SELECT count(*) FROM users WHERE is_master = 1").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM settings").fetchone()[0], len(dbm.SETTINGS))

    def test_the_schema_refuses_bad_rows(self):
        with tempfile.TemporaryDirectory() as d:
            conn = dbm.open_database(Path(d) / "eol.sqlite3", hash_password)
            self.addCleanup(conn.close)
            now = dbm.utcnow()
            bad = [
                ("INSERT INTO parts (part_number, capture_s, created_utc) VALUES (?, 10, ?)", ("X" * 21, now)),
                ("INSERT INTO parts (part_number, tach_connected, created_utc) VALUES ('P1', 1, ?)", (now,)),
                ("INSERT INTO users (username, role, password_hash, created_utc) VALUES ('a', 'boss', 'x', ?)", (now,)),
                ("INSERT INTO test_results (at_utc, part_id, verdict) VALUES (?, 999, 'OK')", (now,)),
                ("INSERT INTO users (username, role, password_hash, is_master, created_utc) VALUES ('m2', 'programming', 'x', 1, ?)", (now,)),
            ]
            for sql, args in bad:
                with self.subTest(sql=sql[:50]):
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(sql, args)

    def test_passwords(self):
        h = hash_password("abc12345")
        self.assertTrue(verify_password("abc12345", h))
        self.assertFalse(verify_password("abc12346", h))
        self.assertFalse(verify_password("abc12345", "garbage"))
        self.assertNotIn("abc12345", h)
        self.assertIsNotNone(password_problem("short1"))
        self.assertIsNotNone(password_problem("onlyletters"))
        self.assertIsNotNone(password_problem("anna2026x", "anna"))
        self.assertIsNone(password_problem("Squeal2026"))


@unittest.skipUnless(HAVE_DEPS, "flask, numpy or scipy is not installed")
class WebTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        d = Path(self._dir.name)
        self.sim = PicoSim().start()
        self.addCleanup(self.sim.stop)
        self.service = AcquisitionService(RecordingStore(d / "data"), microphone_id="sim mic", data_port=self.sim.data_path)
        self.sock = Path(tempfile.mkdtemp(dir="/tmp")) / "acq.sock"
        threading.Thread(target=self.service.serve, args=(self.sock,), daemon=True).start()
        self.assertTrue(self.service.ready.wait(5))
        self.addCleanup(self.service.shutdown)
        self.db_path = d / "eol.sqlite3"
        self.app = create_app(self.db_path, self.sock)
        self.c = self.app.test_client()

    def db(self):
        conn = dbm.connect(self.db_path)
        self.addCleanup(conn.close)
        return conn

    def login(self, user="master", pw=NEW_MASTER_PW):
        return self.c.post("/login", data={"username": user, "password": pw})

    def master(self):
        """Logs in as master and does the forced password change."""
        r = self.login(pw=dbm.MASTER_DEFAULT_PASSWORD)
        self.assertEqual(r.status_code, 302)
        page = self.c.get("/", follow_redirects=True).get_data(as_text=True)
        self.assertIn("must be changed", page)
        r = self.c.post("/password", data={"csrf": csrf_of(page), "current": dbm.MASTER_DEFAULT_PASSWORD,
                                           "new": NEW_MASTER_PW, "repeat": NEW_MASTER_PW})
        self.assertEqual(r.status_code, 302)
        return csrf_of(self.c.get("/").get_data(as_text=True))

    def test_master_must_change_the_default_password_first(self):
        self.login(pw=dbm.MASTER_DEFAULT_PASSWORD)
        r = self.c.get("/setup")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/password", r.headers["Location"])
        self.master()
        self.assertEqual(self.c.get("/setup").status_code, 200)

    def test_wrong_passwords_lock_the_account(self):
        for _ in range(5):
            self.assertEqual(self.login(pw="wrong").status_code, 401)
        r = self.login(pw=dbm.MASTER_DEFAULT_PASSWORD)
        self.assertEqual(r.status_code, 403)
        self.assertIn("locked", r.get_data(as_text=True))
        actions = [r[0] for r in self.db().execute("SELECT action FROM audit_log ORDER BY id")]
        self.assertIn("account_locked", actions)
        self.assertEqual(actions.count("login_failed"), 5)

    def test_forms_without_the_session_token_are_refused(self):
        csrf = self.master()
        self.assertEqual(self.c.post("/setup/parts/new", data={"part_number": "X1"}).status_code, 403)
        self.assertEqual(self.c.post("/setup/parts/new", data={"csrf": "forged", "part_number": "X1"}).status_code, 403)
        self.assertEqual(self.c.post("/setup/parts/new", data={"csrf": csrf, "part_number": "X1", "capture_s": "5"}).status_code, 302)

    def test_roles_limit_what_a_user_can_open(self):
        csrf = self.master()
        page = self.c.post("/admin/users", data={"csrf": csrf, "username": "op1", "role": "testing"},
                           follow_redirects=True).get_data(as_text=True)
        temp = re.search(r"Temporary password: (\S+)", page).group(1)
        r = self.c.post("/logout", data={"csrf": csrf})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", self.c.get("/setup").headers["Location"], "logged out: back to the login page")
        self.assertIsNotNone(self.db().execute("SELECT ended_utc FROM sessions WHERE user_id = 1").fetchone()[0])
        self.login("op1", temp)
        page = self.c.get("/password").get_data(as_text=True)
        self.c.post("/password", data={"csrf": csrf_of(page), "current": temp, "new": "Operator9x", "repeat": "Operator9x"})
        self.assertEqual(self.c.get("/setup").status_code, 403)
        self.assertEqual(self.c.get("/admin/users").status_code, 403)
        self.assertEqual(self.c.get("/").status_code, 200)

    def test_programming_variables_are_checked_and_audited(self):
        csrf = self.master()
        self.c.post("/admin/settings", data={"csrf": csrf, "key": "samples_per_mode", "value": "30"})
        self.assertEqual(dbm.setting(self.db(), "samples_per_mode"), 10)
        self.c.post("/admin/settings", data={"csrf": csrf, "key": "samples_per_mode", "value": "12"})
        self.assertEqual(dbm.setting(self.db(), "samples_per_mode"), 12)
        row = self.db().execute("SELECT detail FROM audit_log WHERE action = 'setting_changed'").fetchone()
        self.assertEqual(json.loads(row[0]), {"from": "10", "to": "12"})

    def test_setup_end_to_end_capture_save_and_retest(self):
        csrf = self.master()
        r = self.c.post("/setup/parts/new", data={"csrf": csrf, "part_number": "BRK-1234", "description": "Front strut",
                                                   "capture_s": "5"})
        part_url = r.headers["Location"]
        part_id = int(part_url.rsplit("/", 1)[1])
        self.c.post(f"{part_url}/modes", data={"csrf": csrf, "name": "Squeal (front)"})
        mode_id = self.db().execute("SELECT id FROM failure_modes").fetchone()[0]

        # Capture a failure-mode sample and save it.
        page = self.c.post(f"/setup/parts/{part_id}/capture", data={"csrf": csrf, "target": f"mode:{mode_id}",
                                                                     "unit_serial": "U-17", "sample_set": "auto"})
        html = page.get_data(as_text=True)
        self.assertIn("VALID", html)
        capture_id = re.search(r'name="capture_id" value="([0-9a-f]+)"', html).group(1)
        self.c.post("/setup/capture/save", data={"csrf": csrf, "capture_id": capture_id})
        conn = self.db()
        s = conn.execute("SELECT * FROM samples").fetchone()
        rec = conn.execute("SELECT * FROM recordings").fetchone()
        self.assertEqual((s["kind"], s["failure_mode_id"], s["unit_serial"], s["sample_set"]), ("failure_mode", mode_id, "U-17", "training"))
        self.assertEqual(rec["purpose"], "failure_mode")
        self.assertEqual(rec["filter_name"], "eol-pdm3200k-d16-p40k-s120k-a100-r1")
        self.assertEqual(len(rec["sha256"]), 64)
        self.assertTrue(Path(rec["path"]).exists())
        self.assertEqual(conn.execute("SELECT identity FROM sensors").fetchone()[0], "sim mic")
        self.assertTrue(self.service.store.verify().ok)

        # Capture a Golden OK sample and throw it away: logged as aborted, nothing stored.
        html = self.c.post(f"/setup/parts/{part_id}/capture", data={"csrf": csrf, "target": "golden"}).get_data(as_text=True)
        capture_id = re.search(r'name="capture_id" value="([0-9a-f]+)"', html).group(1)
        self.c.post("/setup/capture/retest", data={"csrf": csrf, "capture_id": capture_id})
        self.assertEqual(conn.execute("SELECT count(*) FROM samples").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT target FROM aborted_captures").fetchone()[0], "golden")
        # A stale capture id cannot be saved afterwards.
        page = self.c.post("/setup/capture/save", data={"csrf": csrf, "capture_id": capture_id}, follow_redirects=True)
        self.assertIn("no longer waiting", page.get_data(as_text=True))

        # Deleting the failure mode deletes its samples (soft), keeps the recording, and is audited.
        self.c.post(f"/setup/modes/{mode_id}/delete", data={"csrf": csrf})
        self.assertIsNotNone(conn.execute("SELECT deleted_utc FROM samples").fetchone()[0])
        self.assertTrue(Path(rec["path"]).exists())
        actions = {r[0] for r in conn.execute("SELECT action FROM audit_log")}
        self.assertTrue({"part_created", "failure_mode_created", "sample_saved", "capture_aborted", "failure_mode_deleted"} <= actions)

    def test_names_and_limits_follow_the_spec(self):
        csrf = self.master()
        for data, word in (({"part_number": "X" * 21, "capture_s": "5"}, "Part number"),
                           ({"part_number": "bad;name", "capture_s": "5"}, "Part number"),
                           ({"part_number": "P1", "capture_s": "11"}, "5 to 10"),
                           ({"part_number": "P1", "capture_s": "5", "description": "d" * 61}, "60")):
            with self.subTest(data=data):
                r = self.c.post("/setup/parts/new", data={"csrf": csrf, **data})
                self.assertEqual(r.status_code, 400)
                self.assertIn(word, r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
