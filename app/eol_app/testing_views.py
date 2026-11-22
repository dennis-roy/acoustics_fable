"""Testing: calibration at the start of each part, then testing of production parts.

Calibration (spec, Testing): the operator tests one physical Golden OK part and one known
Reject. The golden must pass and also sets the gain (eol_analysis/gain.py); the reject
must fail. Until both are done, testing of that part is blocked.

A test: capture, INVALID checks, analysis of the capture scaled by the gain, the part's
approved model decides, the result goes to the screen and to the PLC through the Pico's
outputs. The raw data of a production part is only ever in memory: it is fetched from
the acquisition service, analysed and dropped. What is stored is the result, the model's
features, the decision with its distances, probabilities and reasons, and the context.

Outputs (decision D7): OK, NOK and UNDEFINED each close their own line; INVALID closes
none, and the screen shows why. READY is closed while waiting for the next part.
"""

from __future__ import annotations

import json
import math
import time

from flask import Flask, flash, g, redirect, render_template, request, url_for

from eol_acquire.client import AcquireClient, AcquireError
from eol_analysis.catalogue import CATALOGUE_VERSION, compute
from eol_analysis.gain import band_level, calibration_gain
from eol_dsp import decimate_bits, get_filter

from . import db as dbm
from .training import analysis_context, load_model
from .web import login_required, row_or_404

OUT_OK, OUT_NOK, OUT_UNDEFINED, OUT_READY, OUT_BUSY = 1, 2, 4, 8, 16
LINE = {"OK": OUT_OK, "NOK": OUT_NOK, "UNDEFINED": OUT_UNDEFINED, "INVALID": 0}


def run_test(conn, client: AcquireClient, part, purpose: str, user_id: int, *, gain: float | None,
             unit: str | None = None, retest_of: int | None = None) -> dict:
    """One test. Returns the stored result as a dict (with 'gain' and, for a golden, 'gain_problem')."""
    bundle, version = load_model(conn, part["id"])
    if bundle is None:
        raise ValueError("this part has no approved model")
    model = bundle["model"]
    priority = [(r["method"], bool(r["enabled"])) for r in
                conn.execute("SELECT method, enabled FROM model_priority WHERE part_id = ? ORDER BY position", (part["id"],))]
    _outputs(conn, client, OUT_BUSY)
    cap = client.capture(part["capture_s"])
    out = {"verdict": "INVALID", "failure_mode": None, "drift": False, "reasons": cap["reasons"], "gain": gain,
           "gain_problem": None, "decision": None, "timings": {}}
    values = {}
    if cap["valid"]:
        raw, clock = client.fetch(cap["capture_id"])
        client.discard(cap["capture_id"])
        spec = get_filter(clock)
        x = decimate_bits(raw, spec).inner().astype("float64")
        del raw
        if purpose == "calibration_golden":
            out["gain"], out["gain_problem"] = calibration_gain(bundle["reference_level"], band_level(x, spec.rate_hz),
                                                                dbm.setting(conn, "gain_limit_db"))
        if out["gain_problem"]:
            out["reasons"] = [out["gain_problem"]]
        else:
            t0 = time.perf_counter()
            values, timings = compute(x * out["gain"], analysis_context(conn, part["id"], spec.rate_hz), skip=set(model.pruned))
            decision = model.decide(values, priority or None)
            out.update(verdict=decision.verdict, failure_mode=decision.failure_mode, drift=decision.drift,
                       reasons=decision.reasons, decision=decision.to_dict(),
                       timings={k: round(v, 4) for k, v in timings.items()}, analysis_s=round(time.perf_counter() - t0, 3))
            out["filter"] = (spec.name, spec.sha256)
    else:
        try:
            client.discard(cap["capture_id"])
        except AcquireError:
            pass
    mode_id = None
    if out["failure_mode"]:
        row = conn.execute("SELECT id FROM failure_modes WHERE part_id = ? AND name = ? AND deleted_utc IS NULL",
                           (part["id"], out["failure_mode"])).fetchone()
        mode_id = row["id"] if row else None
    now = dbm.utcnow()
    decision_json = {"model_version": version, "catalogue": CATALOGUE_VERSION, "decision": out["decision"],
                     "timings_s": out["timings"], "analysis_s": out.get("analysis_s"), "capture": {
                         k: cap.get(k) for k in ("probe", "frames", "missing", "peak_dbfs", "rms_dbfs", "ones_fraction", "longest_run_bits")}}
    with dbm.transaction(conn):
        rid = conn.execute(
            "INSERT INTO test_results (at_utc, station_id, part_id, unit_serial, purpose, verdict, failure_mode_id, drift, invalid_reasons,"
            " decision_json, gain, retest_of, user_id) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, part["id"], unit, purpose, out["verdict"], mode_id if out["verdict"] == "NOK" else None, int(out["drift"]),
             json.dumps(out["reasons"]) if out["verdict"] == "INVALID" else None, json.dumps(decision_json),
             out["gain"] if out["gain"] is not None and math.isfinite(out["gain"]) else None, retest_of, user_id)).lastrowid
        if values:
            kept = {k: v for k, v in values.items() if k in set(model.feature_names)}
            conn.execute("INSERT INTO features (owner_type, owner_id, catalogue_version, filter_name, filter_sha256, gain, values_json,"
                         " created_utc) VALUES ('test_result', ?, ?, ?, ?, ?, ?, ?)",
                         (rid, CATALOGUE_VERSION, out["filter"][0], out["filter"][1], out["gain"], json.dumps(kept), now))
        if out["drift"]:
            d = out["decision"]
            conn.execute("INSERT INTO drift_events (at_utc, part_id, test_result_id, feature, rule, value, limit_value) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (now, part["id"], rid, "golden_distance", "above drift fraction of the golden threshold",
                          d["golden_distance"], d["golden_threshold"]))
        if purpose == "production":
            conn.execute("INSERT INTO edge_outbox (created_utc, topic, payload_json) VALUES (?, ?, ?)",
                         (now, f"eol/station1/{part['part_number']}/result",
                          json.dumps({"result_id": rid, "at": now, "unit": unit, "verdict": out["verdict"], "mode": out["failure_mode"],
                                      "drift": out["drift"]})))
    out["outputs"] = _outputs(conn, client, LINE[out["verdict"]] | OUT_READY)
    out["id"] = rid
    return out


def _outputs(conn, client: AcquireClient, mask: int) -> str | None:
    """Sets the Pico's outputs. Returns None, or why they could not be set (logged as an I/O event)."""
    try:
        client.set_outputs(mask)
        problem = None
    except AcquireError as exc:
        problem = str(exc)
    with dbm.transaction(conn):
        conn.execute("INSERT INTO io_events (at_utc, station_id, kind, detail_json) VALUES (?, 1, 'outputs', ?)",
                     (dbm.utcnow(), json.dumps({"mask": mask, "problem": problem})))
    return problem


def register_testing(app: Flask) -> None:
    state = app.config.setdefault("TEST_STATE", {})

    def client() -> AcquireClient:
        sock = app.config["ACQUIRE_SOCKET"]
        if not sock:
            from eol_acquire.paths import acquire_socket
            sock = acquire_socket()
        return AcquireClient(sock)

    def my_state() -> dict:
        return state.setdefault(g.session["token_sha256"], {})

    @app.get("/testing")
    @login_required("testing")
    def testing_home():
        parts = g.db.execute(
            "SELECT p.*, (SELECT max(version) FROM models m WHERE m.part_id = p.id AND m.state = 'approved') AS model"
            " FROM parts p WHERE p.archived_utc IS NULL ORDER BY p.part_number").fetchall()
        return render_template("testing_parts.html", parts=parts)

    @app.post("/testing/select/<int:part_id>")
    @login_required("testing")
    def testing_select(part_id):
        part = row_or_404(g.db, "SELECT * FROM parts WHERE id = ?", part_id)
        st = my_state()
        st.clear()
        st.update(part_id=part_id, step="golden")
        with dbm.transaction(g.db):
            g.db.execute("INSERT INTO context_events (at_utc, station_id, kind, value, user_id) VALUES (?, 1, 'part', ?, ?)",
                         (dbm.utcnow(), part["part_number"], g.user["user_id"]))
        return redirect(url_for("testing_calibrate", part_id=part_id))

    @app.route("/testing/<int:part_id>/calibrate", methods=["GET", "POST"])
    @login_required("testing")
    def testing_calibrate(part_id):
        part = row_or_404(g.db, "SELECT * FROM parts WHERE id = ?", part_id)
        st = my_state()
        if st.get("part_id") != part_id:
            return redirect(url_for("testing_home"))
        result = None
        if request.method == "POST":
            step = st.get("step", "golden")
            purpose = "calibration_golden" if step == "golden" else "calibration_reject"
            try:
                result = run_test(g.db, client(), part, purpose, g.user["user_id"], gain=st.get("gain") if step == "reject" else None)
            except (AcquireError, ValueError) as exc:
                flash(f"Test failed to run: {exc}")
                return redirect(url_for("testing_calibrate", part_id=part_id))
            if step == "golden":
                if result["verdict"] == "OK":
                    st.update(step="reject", gain=result["gain"], golden_result=result["id"])
                else:
                    _calibration(g.db, part_id, g.user["user_id"], result["id"], None, False, result["gain"],
                                 f"the Golden OK part did not pass: {result['verdict']}")
                    flash("Calibration failed: the Golden OK part must pass. Testing stays blocked. Check the part, the fixture and the microphone, then start again.")
            else:
                passed = result["verdict"] in ("NOK", "UNDEFINED")
                cid = _calibration(g.db, part_id, g.user["user_id"], st.get("golden_result"), result["id"], passed, st.get("gain"),
                                   None if passed else f"the Reject part was not rejected: {result['verdict']}")
                if passed:
                    st.update(step="done", calibration_id=cid)
                    flash("Calibration passed. Testing is open for this part.")
                    return redirect(url_for("testing_run", part_id=part_id))
                st.update(step="golden")
                flash("Calibration failed: the Reject part must fail. Testing stays blocked. Start the calibration again.")
        return render_template("calibrate.html", part=part, step=st.get("step", "golden"), result=result, gain=st.get("gain"))

    @app.route("/testing/<int:part_id>/run", methods=["GET", "POST"])
    @login_required("testing")
    def testing_run(part_id):
        part = row_or_404(g.db, "SELECT * FROM parts WHERE id = ?", part_id)
        st = my_state()
        if st.get("part_id") != part_id or not st.get("calibration_id"):
            flash("Calibrate this part first.")
            return redirect(url_for("testing_home"))
        result = None
        if request.method == "POST":
            retest_of = st.get("last_result") if request.form.get("retest") == "1" else None
            try:
                result = run_test(g.db, client(), part, "production", g.user["user_id"], gain=st["gain"],
                                  unit=request.form.get("unit_serial", "").strip()[:40] or None, retest_of=retest_of)
            except (AcquireError, ValueError) as exc:
                flash(f"Test failed to run: {exc}")
                return redirect(url_for("testing_run", part_id=part_id))
            with dbm.transaction(g.db):
                g.db.execute("UPDATE test_results SET calibration_check_id = ? WHERE id = ?", (st["calibration_id"], result["id"]))
            st["last_result"] = result["id"]
        return render_template("test_run.html", part=part, result=result, gain=st["gain"], last=st.get("last_result"))


def _calibration(conn, part_id, user_id, golden_id, reject_id, passed, gain, why) -> int:
    with dbm.transaction(conn):
        cid = conn.execute(
            "INSERT INTO calibration_checks (at_utc, station_id, part_id, user_id, golden_result_id, reject_result_id, passed, gain, detail_json)"
            " VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)", (dbm.utcnow(), part_id, user_id, golden_id, reject_id, int(passed),
                                                    gain if gain is not None and math.isfinite(gain) else None,
                                                    json.dumps({"why": why}) if why else None)).lastrowid
        dbm.audit(conn, user_id, "calibration_passed" if passed else "calibration_failed", "part", part_id, {"gain": gain, "why": why})
    return cid
