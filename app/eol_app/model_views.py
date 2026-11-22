"""Models of a part: train, read the cross-validated rates, approve, and order the methods."""

from __future__ import annotations

import json

from flask import Flask, flash, g, redirect, render_template, request, url_for

from eol_analysis import ml

from . import db as dbm
from .training import approve, start_training
from .web import login_required, row_or_404

LOCKED = {"golden_envelope"}  # always first-class: cannot be disabled


def register_models(app: Flask) -> None:
    @app.get("/setup/parts/<int:part_id>/models")
    @login_required("setup")
    def models_page(part_id):
        part = row_or_404(g.db, "SELECT * FROM parts WHERE id = ?", part_id)
        versions = g.db.execute(
            "SELECT version, state, trained_utc, approved_utc, metrics_json, pruned_json, min(id) AS id FROM models"
            " WHERE part_id = ? GROUP BY version ORDER BY version DESC", (part_id,)).fetchall()
        priority = g.db.execute("SELECT * FROM model_priority WHERE part_id = ? ORDER BY position", (part_id,)).fetchall()
        if not priority:
            priority = [{"method": m, "enabled": int(e), "position": i} for i, (m, e) in enumerate(ml.DEFAULT_PRIORITY)]
        job = app.config.get("JOBS", {}).get(part_id)
        parsed = [dict(v, metrics=json.loads(v["metrics_json"] or "{}"), prune=json.loads(v["pruned_json"] or "{}")) for v in versions]
        return render_template("models.html", part=part, versions=parsed, priority=priority, job=job, locked=LOCKED)

    @app.post("/setup/parts/<int:part_id>/train")
    @login_required("setup")
    def models_train(part_id):
        row_or_404(g.db, "SELECT id FROM parts WHERE id = ?", part_id)
        if start_training(app, part_id, g.user["user_id"]):
            flash("Training started. This page shows its progress.")
        else:
            flash("A training is already running. Wait for it to finish.")
        return redirect(url_for("models_page", part_id=part_id))

    @app.post("/setup/parts/<int:part_id>/approve/<int:version>")
    @login_required("programming")
    def models_approve(part_id, version):
        try:
            pruned = approve(g.db, part_id, version, g.user["user_id"])
        except ValueError as exc:
            flash(str(exc))
        else:
            flash(f"Version {version} approved." + (f" Pruned analyses, no longer computed for this part: {', '.join(pruned)}." if pruned else ""))
        return redirect(url_for("models_page", part_id=part_id))

    @app.post("/setup/parts/<int:part_id>/priority")
    @login_required("programming")
    def models_priority(part_id):
        row_or_404(g.db, "SELECT id FROM parts WHERE id = ?", part_id)
        order = [m for m in request.form.get("order", "").split(",") if m in ml.METHODS]
        if sorted(order) != sorted(ml.METHODS):
            flash("The order must list every method once.")
            return redirect(url_for("models_page", part_id=part_id))
        enabled = set(request.form.getlist("enabled")) | LOCKED
        with dbm.transaction(g.db):
            g.db.execute("DELETE FROM model_priority WHERE part_id = ?", (part_id,))
            for pos, m in enumerate(order):
                g.db.execute("INSERT INTO model_priority (part_id, position, method, enabled) VALUES (?, ?, ?, ?)",
                             (part_id, pos, m, int(m in enabled)))
            dbm.audit(g.db, g.user["user_id"], "model_priority_changed", "part", part_id,
                      {"order": order, "enabled": sorted(enabled & set(order))})
        flash("Method order saved. It applies from the next test.")
        return redirect(url_for("models_page", part_id=part_id))
