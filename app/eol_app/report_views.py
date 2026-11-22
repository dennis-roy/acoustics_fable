"""Reports (spec, Industry 4.0): yield, retests, failure Pareto, UNDEFINED log, controlled drift,
by operator and shift, calibrations, model history. On screen, as CSV, and printable to PDF from
the browser (the page has a print layout)."""

from __future__ import annotations

import csv
import io
import json
import time
from collections import Counter, defaultdict

from flask import Flask, Response, g, render_template, request

from . import db as dbm
from .web import login_required


def _range():
    today = time.strftime("%Y-%m-%d", time.gmtime())
    week = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 6 * 86400))
    start = request.args.get("from") or week
    end = request.args.get("to") or today
    return start, end, f"{start}T00:00:00Z", f"{end}T23:59:59Z"


def _local_hhmm(at_utc: str) -> str:
    t = time.mktime(time.strptime(at_utc, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    return time.strftime("%H:%M", time.localtime(t))


def shift_of(at_utc: str, shifts) -> str:
    hhmm = _local_hhmm(at_utc)
    for s in shifts:
        a, b = s["start_local"], s["end_local"]
        if (a <= hhmm < b) if a < b else (hhmm >= a or hhmm < b):
            return s["name"]
    return "no shift"


def build(conn, start_utc: str, end_utc: str, part_id: int | None) -> dict:
    where, args = "r.at_utc BETWEEN ? AND ? AND r.purpose = 'production'", [start_utc, end_utc]
    if part_id:
        where += " AND r.part_id = ?"
        args.append(part_id)
    rows = conn.execute(
        f"SELECT r.*, p.part_number, f.name AS mode, u.username FROM test_results r JOIN parts p ON p.id = r.part_id"
        f" LEFT JOIN failure_modes f ON f.id = r.failure_mode_id LEFT JOIN users u ON u.id = r.user_id WHERE {where} ORDER BY r.at_utc",
        args).fetchall()
    shifts = conn.execute("SELECT * FROM shifts WHERE active = 1 ORDER BY start_local").fetchall()
    verdicts = Counter(r["verdict"] for r in rows)
    first = {}
    for r in rows:  # the first result of each unit (a result without a serial is its own unit)
        key = (r["part_id"], r["unit_serial"] or f"result-{r['id']}")
        first.setdefault(key, r)
    fpy = sum(1 for r in first.values() if r["verdict"] == "OK") / len(first) if first else None
    by = lambda keyf: sorted(Counter(keyf(r) for r in rows).items(), key=lambda kv: -kv[1])  # noqa: E731
    per = defaultdict(Counter)
    for r in rows:
        per[r["username"] or "?"][r["verdict"]] += 1
        per["shift: " + shift_of(r["at_utc"], shifts)][r["verdict"]] += 1
    return {
        "rows": rows, "total": len(rows), "verdicts": verdicts, "units": len(first), "fpy": fpy,
        "retests": sum(1 for r in rows if r["retest_of"]),
        "pareto": by(lambda r: r["mode"]) if rows else [],
        "pareto_nok": sorted(Counter(r["mode"] for r in rows if r["verdict"] == "NOK").items(), key=lambda kv: -kv[1]),
        "undefined": [r for r in rows if r["verdict"] == "UNDEFINED"],
        "drift": [r for r in rows if r["drift"]],
        "per": {k: dict(v) for k, v in sorted(per.items())},
        "calibrations": conn.execute("SELECT c.*, p.part_number, u.username FROM calibration_checks c JOIN parts p ON p.id = c.part_id"
                                     " LEFT JOIN users u ON u.id = c.user_id WHERE c.at_utc BETWEEN ? AND ? ORDER BY c.at_utc",
                                     (start_utc, end_utc)).fetchall(),
        "models": conn.execute("SELECT p.part_number, m.version, m.state, m.trained_utc, m.approved_utc, m.metrics_json FROM models m"
                               " JOIN parts p ON p.id = m.part_id WHERE m.method = 'golden_envelope' ORDER BY p.part_number, m.version DESC").fetchall(),
    }


def register_reports(app: Flask) -> None:
    @app.get("/reports")
    @login_required("testing")
    def reports():
        start, end, s_utc, e_utc = _range()
        part_id = request.args.get("part", type=int)
        data = build(g.db, s_utc, e_utc, part_id)
        models = [dict(m, cv=json.loads(m["metrics_json"] or "{}").get("cross_validation", {})) for m in data["models"]]
        parts = g.db.execute("SELECT id, part_number FROM parts ORDER BY part_number").fetchall()
        return render_template("reports.html", d=data, start=start, end=end, part_id=part_id, parts=parts, models=models)

    @app.get("/reports/results.csv")
    @login_required("testing")
    def reports_csv():
        start, end, s_utc, e_utc = _range()
        data = build(g.db, s_utc, e_utc, request.args.get("part", type=int))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["result_id", "time_utc", "part", "unit_serial", "verdict", "failure_mode", "controlled_drift", "retest_of", "operator", "gain"])
        for r in data["rows"]:
            w.writerow([r["id"], r["at_utc"], r["part_number"], r["unit_serial"] or "", r["verdict"], r["mode"] or "", r["drift"],
                        r["retest_of"] or "", r["username"] or "", r["gain"] if r["gain"] is not None else ""])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=eol-results-{start}-to-{end}.csv"})

    @app.route("/admin/shifts", methods=["GET", "POST"])
    @login_required("programming")
    def admin_shifts():
        from flask import flash, redirect, url_for
        if request.method == "POST":
            name, a, b = (request.form.get(k, "").strip() for k in ("name", "start", "end"))
            try:
                with dbm.transaction(g.db):
                    if request.form.get("delete"):
                        g.db.execute("UPDATE shifts SET active = 0 WHERE id = ?", (request.form["delete"],))
                    else:
                        g.db.execute("INSERT INTO shifts (name, start_local, end_local) VALUES (?, ?, ?)", (name, a, b))
                    dbm.audit(g.db, g.user["user_id"], "shift_changed", "shift", name or request.form.get("delete"), {"start": a, "end": b})
            except Exception as exc:  # constraint errors: bad time format or duplicate name
                flash(f"Not saved: {exc}")
            return redirect(url_for("admin_shifts"))
        return render_template("admin_shifts.html", shifts=g.db.execute("SELECT * FROM shifts WHERE active = 1 ORDER BY start_local").fetchall())
