"""Setup: parts, failure modes, and the calibrated samples that train the models.

The capture flow (spec, Setup): choose what to record (a failure mode, or Golden OK),
capture, look at the result, then Save (kept permanently as a calibrated sample) or
Retest (thrown away and logged as aborted). Deleting a sample or a failure mode later is
audited and invalidates the part's models until they are retrained and approved.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for

from eol_acquire.client import AcquireClient, AcquireError

from . import db as dbm
from .web import login_required, row_or_404

NAME = re.compile(r"[A-Za-z0-9 \-_./()]+")


def _name_problem(value: str, what: str, limit: int) -> str | None:
    if not value or len(value) > limit or not NAME.fullmatch(value):
        return f"{what}: 1 to {limit} characters, letters, digits, space and - _ . / ( )"
    return None


def part_summary(conn, part_id: int) -> dict:
    modes = conn.execute(
        "SELECT f.id, f.name, (SELECT count(*) FROM samples s WHERE s.failure_mode_id = f.id AND s.deleted_utc IS NULL) AS n"
        " FROM failure_modes f WHERE f.part_id = ? AND f.deleted_utc IS NULL ORDER BY f.name", (part_id,)).fetchall()
    golden = conn.execute("SELECT count(*) FROM samples WHERE part_id = ? AND kind = 'golden' AND deleted_utc IS NULL",
                          (part_id,)).fetchone()[0]
    return {"modes": modes, "golden": golden, "per_mode": dbm.setting(conn, "samples_per_mode"),
            "golden_target": dbm.setting(conn, "golden_samples")}


def invalidate_models(conn, part_id: int, user_id: int, why: str) -> None:
    n = conn.execute("UPDATE models SET state = 'invalidated' WHERE part_id = ? AND state IN ('trained', 'approved')",
                     (part_id,)).rowcount
    if n:
        dbm.audit(conn, user_id, "models_invalidated", "part", part_id, {"models": n, "why": why})


def register_setup(app: Flask) -> None:
    def client() -> AcquireClient:
        sock = app.config["ACQUIRE_SOCKET"]
        if not sock:
            from eol_acquire.paths import acquire_socket
            sock = acquire_socket()
        return AcquireClient(sock)

    @app.get("/setup")
    @login_required("setup")
    def setup_parts():
        parts = g.db.execute(
            "SELECT p.*, (SELECT count(*) FROM failure_modes f WHERE f.part_id = p.id AND f.deleted_utc IS NULL) AS modes,"
            " (SELECT count(*) FROM samples s WHERE s.part_id = p.id AND s.deleted_utc IS NULL) AS samples"
            " FROM parts p WHERE p.archived_utc IS NULL ORDER BY p.part_number").fetchall()
        return render_template("setup_parts.html", parts=parts)

    @app.route("/setup/parts/new", methods=["GET", "POST"])
    @login_required("setup")
    def part_new():
        limit = dbm.setting(g.db, "capture_limit_s")
        if request.method == "GET":
            return render_template("part_form.html", part=None, limit=limit)
        f = request.form
        number, desc = f.get("part_number", "").strip(), f.get("description", "").strip()
        problem = _name_problem(number, "Part number", 20) or (desc and len(desc) > 60 and "Description: up to 60 characters")
        values, problem2 = _part_values(f, limit)
        problem = problem or problem2
        if not problem and g.db.execute("SELECT 1 FROM parts WHERE part_number = ?", (number,)).fetchone():
            problem = f"Part number {number} already exists."
        if problem:
            flash(problem)
            return render_template("part_form.html", part=None, limit=limit, form=f), 400
        with dbm.transaction(g.db):
            cur = g.db.execute(
                "INSERT INTO parts (part_number, description, tach_connected, tach_ppr, capture_s, created_utc, created_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)", (number, desc, *values, dbm.utcnow(), g.user["user_id"]))
            dbm.audit(g.db, g.user["user_id"], "part_created", "part", cur.lastrowid, {"part_number": number, "description": desc})
        return redirect(url_for("part_detail", part_id=cur.lastrowid))

    def _part_values(f, limit):
        tach = f.get("tach_connected") == "1"
        try:
            capture_s = int(f.get("capture_s", "10"))
            ppr = int(f.get("tach_ppr") or 1) if tach else None
        except ValueError:
            return None, "Capture duration and pulses per revolution must be whole numbers."
        if not 5 <= capture_s <= limit:
            return None, f"Capture duration: 5 to {limit} s."
        if tach and not 1 <= ppr <= 1000:
            return None, "Pulses per revolution: 1 to 1000."
        return (1 if tach else 0, ppr, capture_s), None

    @app.route("/setup/parts/<int:part_id>", methods=["GET", "POST"])
    @login_required("setup")
    def part_detail(part_id):
        part = row_or_404(g.db, "SELECT * FROM parts WHERE id = ? AND archived_utc IS NULL", part_id)
        limit = dbm.setting(g.db, "capture_limit_s")
        if request.method == "POST":
            desc = request.form.get("description", "").strip()
            values, problem = _part_values(request.form, limit)
            if len(desc) > 60:
                problem = "Description: up to 60 characters"
            if problem:
                flash(problem)
            else:
                with dbm.transaction(g.db):
                    g.db.execute("UPDATE parts SET description = ?, tach_connected = ?, tach_ppr = ?, capture_s = ? WHERE id = ?",
                                 (desc, *values, part_id))
                    dbm.audit(g.db, g.user["user_id"], "part_changed", "part", part_id,
                              {"description": desc, "tach_connected": values[0], "tach_ppr": values[1], "capture_s": values[2]})
                flash("Saved.")
            return redirect(url_for("part_detail", part_id=part_id))
        samples = g.db.execute(
            "SELECT s.*, f.name AS mode, r.seconds, r.sha256 FROM samples s LEFT JOIN failure_modes f ON f.id = s.failure_mode_id"
            " JOIN recordings r ON r.id = s.recording_id WHERE s.part_id = ? AND s.deleted_utc IS NULL ORDER BY s.id DESC",
            (part_id,)).fetchall()
        return render_template("part_detail.html", part=part, limit=limit, samples=samples, **part_summary(g.db, part_id))

    @app.post("/setup/parts/<int:part_id>/modes")
    @login_required("setup")
    def mode_add(part_id):
        row_or_404(g.db, "SELECT id FROM parts WHERE id = ?", part_id)
        name = request.form.get("name", "").strip()
        problem = _name_problem(name, "Failure mode", 30)
        if not problem and g.db.execute("SELECT 1 FROM failure_modes WHERE part_id = ? AND name = ? AND deleted_utc IS NULL",
                                        (part_id, name)).fetchone():
            problem = f"Failure mode {name} already exists for this part."
        if problem:
            flash(problem)
        else:
            with dbm.transaction(g.db):
                cur = g.db.execute("INSERT INTO failure_modes (part_id, name, created_utc, created_by) VALUES (?, ?, ?, ?)",
                                   (part_id, name, dbm.utcnow(), g.user["user_id"]))
                dbm.audit(g.db, g.user["user_id"], "failure_mode_created", "failure_mode", cur.lastrowid, {"part_id": part_id, "name": name})
                invalidate_models(g.db, part_id, g.user["user_id"], f"failure mode {name} added")
        return redirect(url_for("part_detail", part_id=part_id))

    @app.post("/setup/modes/<int:mode_id>/delete")
    @login_required("setup")
    def mode_delete(mode_id):
        mode = row_or_404(g.db, "SELECT * FROM failure_modes WHERE id = ? AND deleted_utc IS NULL", mode_id)
        now = dbm.utcnow()
        with dbm.transaction(g.db):
            g.db.execute("UPDATE failure_modes SET deleted_utc = ?, deleted_by = ? WHERE id = ?", (now, g.user["user_id"], mode_id))
            n = g.db.execute("UPDATE samples SET deleted_utc = ?, deleted_by = ? WHERE failure_mode_id = ? AND deleted_utc IS NULL",
                             (now, g.user["user_id"], mode_id)).rowcount
            dbm.audit(g.db, g.user["user_id"], "failure_mode_deleted", "failure_mode", mode_id, {"name": mode["name"], "samples": n})
            invalidate_models(g.db, mode["part_id"], g.user["user_id"], f"failure mode {mode['name']} deleted")
        flash(f"Failure mode {mode['name']} deleted, with its {n} sample(s). The recordings themselves are kept.")
        return redirect(url_for("part_detail", part_id=mode["part_id"]))

    @app.post("/setup/samples/<int:sample_id>/delete")
    @login_required("setup")
    def sample_delete(sample_id):
        s = row_or_404(g.db, "SELECT * FROM samples WHERE id = ? AND deleted_utc IS NULL", sample_id)
        with dbm.transaction(g.db):
            g.db.execute("UPDATE samples SET deleted_utc = ?, deleted_by = ? WHERE id = ?", (dbm.utcnow(), g.user["user_id"], sample_id))
            dbm.audit(g.db, g.user["user_id"], "sample_deleted", "sample", sample_id, {"recording": s["recording_id"]})
            invalidate_models(g.db, s["part_id"], g.user["user_id"], f"sample {sample_id} deleted")
        flash("Sample deleted. Its recording is kept.")
        return redirect(url_for("part_detail", part_id=s["part_id"]))

    # ---- capture: choose, capture, then Save or Retest ----

    def _target(part_id: int, target: str):
        """('golden', None) or ('failure_mode', mode row)."""
        if target == "golden":
            return "golden", None
        if target.startswith("mode:") and target[5:].isdigit():
            mode = g.db.execute("SELECT * FROM failure_modes WHERE id = ? AND part_id = ? AND deleted_utc IS NULL",
                                (int(target[5:]), part_id)).fetchone()
            if mode:
                return "failure_mode", mode
        abort(400)

    def _count(part_id, kind, mode):
        if kind == "golden":
            return g.db.execute("SELECT count(*) FROM samples WHERE part_id = ? AND kind = 'golden' AND deleted_utc IS NULL",
                                (part_id,)).fetchone()[0]
        return g.db.execute("SELECT count(*) FROM samples WHERE failure_mode_id = ? AND deleted_utc IS NULL", (mode["id"],)).fetchone()[0]

    @app.get("/setup/parts/<int:part_id>/capture")
    @login_required("setup")
    def capture_page(part_id):
        part = row_or_404(g.db, "SELECT * FROM parts WHERE id = ?", part_id)
        kind, mode = _target(part_id, request.args.get("target", ""))
        target_n = dbm.setting(g.db, "golden_samples" if kind == "golden" else "samples_per_mode")
        return render_template("capture.html", part=part, kind=kind, mode=mode, target=request.args["target"],
                               count=_count(part_id, kind, mode), target_n=target_n,
                               every=dbm.setting(g.db, "validation_every"))

    @app.post("/setup/parts/<int:part_id>/capture")
    @login_required("setup")
    def capture_run(part_id):
        part = row_or_404(g.db, "SELECT * FROM parts WHERE id = ?", part_id)
        target = request.form.get("target", "")
        kind, mode = _target(part_id, target)
        count = _count(part_id, kind, mode)
        target_n = dbm.setting(g.db, "golden_samples" if kind == "golden" else "samples_per_mode")
        if count >= target_n:
            flash(f"This target already has {count} of {target_n} samples. Delete one, or raise the target in Programming.")
            return redirect(url_for("part_detail", part_id=part_id))
        unit = request.form.get("unit_serial", "").strip()[:40] or None
        chosen = request.form.get("sample_set", "auto")
        every = dbm.setting(g.db, "validation_every")
        sample_set = chosen if chosen in ("training", "validation") else ("validation" if (count + 1) % every == 0 else "training")
        try:
            cap = client().capture(part["capture_s"])
        except AcquireError as exc:
            flash(f"Capture failed: {exc}")
            return redirect(url_for("capture_page", part_id=part_id, target=target))
        app.config["PENDING"][g.session["token_sha256"]] = {
            "capture_id": cap["capture_id"], "part_id": part_id, "target": target, "unit_serial": unit,
            "sample_set": sample_set, "valid": cap["valid"], "reasons": cap["reasons"]}
        return render_template("capture_result.html", part=part, kind=kind, mode=mode, cap=cap, target=target,
                               sample_set=sample_set, unit=unit, count=count, target_n=target_n)

    def _pending():
        p = app.config["PENDING"].pop(g.session["token_sha256"], None)
        if not p or p["capture_id"] != request.form.get("capture_id"):
            flash("That capture is no longer waiting. Capture again.")
            return None
        return p

    @app.post("/setup/capture/save")
    @login_required("setup")
    def capture_save():
        p = _pending()
        if p is None:
            return redirect(url_for("setup_parts"))
        part = row_or_404(g.db, "SELECT * FROM parts WHERE id = ?", p["part_id"])
        kind, mode = _target(p["part_id"], p["target"])
        try:
            rec = client().save(p["capture_id"], kind, part=part["part_number"],
                                failure_mode=mode["name"] if mode else None, note=f"unit {p['unit_serial'] or '-'}")
        except AcquireError as exc:
            flash(f"Not saved: {exc}")
            return redirect(url_for("capture_page", part_id=p["part_id"], target=p["target"]))
        meta = json.loads(Path(rec["path"]).with_suffix(".json").read_text())
        now = dbm.utcnow()
        with dbm.transaction(g.db):
            sensor = g.db.execute("SELECT id FROM sensors WHERE identity = ?", (meta["microphone_id"],)).fetchone()
            sensor_id = sensor["id"] if sensor else g.db.execute(
                "INSERT INTO sensors (kind, identity, installed_utc) VALUES ('microphone', ?, ?)", (meta["microphone_id"], now)).lastrowid
            g.db.execute(
                "INSERT INTO recordings (id, path, sha256, bytes, seconds, clock_hz, sensor_id, firmware, filter_name, filter_sha256,"
                " purpose, captured_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rec["id"], rec["path"], rec["sha256"], rec["bytes"], rec["seconds"], meta["clock_hz"], sensor_id,
                 f"{meta['pico']['firmware']} ({meta['pico']['build']})", meta["filter"]["name"], meta["filter"]["sha256"],
                 kind, meta["captured_utc"]))
            sid = g.db.execute(
                "INSERT INTO samples (part_id, kind, failure_mode_id, sample_set, unit_serial, recording_id, created_utc, created_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (p["part_id"], kind, mode["id"] if mode else None, p["sample_set"],
                                                     p["unit_serial"], rec["id"], now, g.user["user_id"])).lastrowid
            dbm.audit(g.db, g.user["user_id"], "sample_saved", "sample", sid,
                      {"recording": rec["id"], "sha256": rec["sha256"], "kind": kind, "set": p["sample_set"]})
            invalidate_models(g.db, p["part_id"], g.user["user_id"], f"sample {sid} added")
        flash(f"Saved as sample {sid} ({p['sample_set']}).")
        return redirect(url_for("capture_page", part_id=p["part_id"], target=p["target"]))

    @app.post("/setup/capture/retest")
    @login_required("setup")
    def capture_retest():
        p = _pending()
        if p is None:
            return redirect(url_for("setup_parts"))
        try:
            client().discard(p["capture_id"])
        except AcquireError:
            pass  # already gone: a newer capture replaced it, which is just as discarded
        with dbm.transaction(g.db):
            g.db.execute("INSERT INTO aborted_captures (at_utc, part_id, target, valid, reasons, user_id) VALUES (?, ?, ?, ?, ?, ?)",
                         (dbm.utcnow(), p["part_id"], p["target"], int(p["valid"]), json.dumps(p["reasons"]), g.user["user_id"]))
            dbm.audit(g.db, g.user["user_id"], "capture_aborted", "part", p["part_id"], {"target": p["target"], "valid": p["valid"]})
        flash("Discarded. Capture again when ready.")
        return redirect(url_for("capture_page", part_id=p["part_id"], target=p["target"]))
