"""Features of calibrated samples, training, cross-validation and approval of a part's models.

Training runs in a background thread (one at a time) because computing the features of
100 samples takes a few minutes on the CM5. The trained model is pickled to
<data dir>/models/ and its SHA-256 stored; it is only ever loaded if the file still
matches that SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import threading
import time
from pathlib import Path

import numpy as np

from eol_analysis import ml
from eol_analysis.catalogue import CATALOGUE_VERSION, REGISTRY, Context, compute
from eol_analysis.gain import band_level, reference_level
from eol_dsp import decimate_bits, get_filter

from . import db as dbm

_lock = threading.Lock()


def capture_samples(path: str | Path, clock_hz: int):
    """200 kHz samples of a recording, edges removed, and the filter used."""
    spec = get_filter(clock_hz)
    s = decimate_bits(Path(path).read_bytes(), spec)
    return s.inner().astype(np.float64), spec


def analysis_context(conn, part_id: int, fs: float, tach_s=None) -> Context:
    part = conn.execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()
    orders = {r["name"]: r["order_value"] for r in conn.execute("SELECT name, order_value FROM part_orders WHERE part_id = ?", (part_id,))}
    return Context(fs, peak_threshold=dbm.setting(conn, "peak_threshold"), tach_s=tach_s,
                   ppr=part["tach_ppr"] or 1, orders=orders)


def sample_features(conn, sample) -> dict[str, float]:
    """Features of one calibrated sample, computed once per catalogue version and cached."""
    rec = conn.execute("SELECT * FROM recordings WHERE id = ?", (sample["recording_id"],)).fetchone()
    row = conn.execute("SELECT * FROM features WHERE owner_type = 'sample' AND owner_id = ?", (sample["id"],)).fetchone()
    spec = get_filter(rec["clock_hz"])
    if row and row["catalogue_version"] == CATALOGUE_VERSION and row["filter_sha256"] == spec.sha256:
        cached = json.loads(row["values_json"])
        if {k.split(".", 1)[0] for k in cached} >= set(REGISTRY):  # complete: nothing pruned away
            return cached
    x, spec = capture_samples(rec["path"], rec["clock_hz"])
    values, _ = compute(x, analysis_context(conn, sample["part_id"], spec.rate_hz))
    values["gain.band_level"] = band_level(x, spec.rate_hz)  # for the golden reference; never a model feature
    with dbm.transaction(conn):
        conn.execute("DELETE FROM features WHERE owner_type = 'sample' AND owner_id = ?", (sample["id"],))
        conn.execute("INSERT INTO features (owner_type, owner_id, catalogue_version, filter_name, filter_sha256, gain, values_json,"
                     " created_utc) VALUES ('sample', ?, ?, ?, ?, 1.0, ?, ?)",
                     (sample["id"], CATALOGUE_VERSION, spec.name, spec.sha256, json.dumps(values), dbm.utcnow()))
    return values


def model_dir(db_path: str) -> Path:
    return Path(db_path).parent / "models"


def train_part(db_path: str, part_id: int, user_id: int, progress: dict) -> int:
    """Trains every method for a part. Returns the new model version."""
    conn = dbm.connect(db_path)
    try:
        samples = conn.execute(
            "SELECT s.*, f.name AS mode FROM samples s LEFT JOIN failure_modes f ON f.id = s.failure_mode_id"
            " WHERE s.part_id = ? AND s.deleted_utc IS NULL ORDER BY s.id", (part_id,)).fetchall()
        values = []
        for i, s in enumerate(samples):
            progress["message"] = f"features of sample {i + 1} of {len(samples)}"
            values.append(sample_features(conn, s))
        label = [ml.OK if s["kind"] == "golden" else s["mode"] for s in samples]
        group = [s["unit_serial"] or f"sample-{s['id']}" for s in samples]
        model_values = [{k: v for k, v in vals.items() if not k.startswith("gain.")} for vals in values]
        tr = [i for i, s in enumerate(samples) if s["sample_set"] == "training"]
        va = [i for i, s in enumerate(samples) if s["sample_set"] == "validation"]
        data = ml.TrainingData([model_values[i] for i in tr], [label[i] for i in tr], [group[i] for i in tr])
        priority = [(r["method"], bool(r["enabled"])) for r in
                    conn.execute("SELECT method, enabled FROM model_priority WHERE part_id = ? ORDER BY position", (part_id,))] or None
        progress["message"] = "training"
        model, prune_report = ml.train(data)
        progress["message"] = "cross-validating"
        cv = ml.cross_validate(data, priority=priority)
        val_rows = [(label[i], model.decide(model_values[i], priority)) for i in va]
        validation = {
            "samples": len(val_rows),
            "escapes": sum(1 for t, d in val_rows if t != ml.OK and d.verdict == "OK"),
            "false_rejects": sum(1 for t, d in val_rows if t == ml.OK and d.verdict != "OK"),
            "right_mode": sum(1 for t, d in val_rows if t != ml.OK and d.failure_mode == t),
            "nok_samples": sum(1 for t, _ in val_rows if t != ml.OK),
            "ok_samples": sum(1 for t, _ in val_rows if t == ml.OK),
        }
        golden_levels = [values[i]["gain.band_level"] for i in tr if label[i] == ml.OK]
        reference = reference_level(golden_levels)
        version = (conn.execute("SELECT max(version) FROM models WHERE part_id = ?", (part_id,)).fetchone()[0] or 0) + 1
        mdir = model_dir(db_path)
        mdir.mkdir(parents=True, exist_ok=True)
        path = mdir / f"part-{part_id}-v{version}.pkl"
        blob = pickle.dumps({"model": model, "reference_level": reference, "catalogue": CATALOGUE_VERSION})
        path.write_bytes(blob)
        digest = hashlib.sha256(blob).hexdigest()
        metrics = {"cross_validation": cv, "validation": validation,
                   "training_samples": len(tr), "classes": sorted(set(data.labels))}
        now = dbm.utcnow()
        with dbm.transaction(conn):
            if not conn.execute("SELECT 1 FROM model_priority WHERE part_id = ?", (part_id,)).fetchone():
                for pos, (method, enabled) in enumerate(ml.DEFAULT_PRIORITY):
                    conn.execute("INSERT INTO model_priority (part_id, position, method, enabled) VALUES (?, ?, ?, ?)",
                                 (part_id, pos, method, int(enabled)))
            for method in ml.METHODS:
                conn.execute(
                    "INSERT INTO models (part_id, method, version, state, feature_names_json, pruned_json, params_json, metrics_json,"
                    " artifact_path, artifact_sha256, trained_utc, trained_by) VALUES (?, ?, ?, 'trained', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (part_id, method, version, json.dumps(model.feature_names), json.dumps(prune_report),
                     json.dumps(model.settings.__dict__), json.dumps(metrics), str(path), digest, now, user_id))
            conn.execute("INSERT INTO reference_data (part_id, kind, value_json, created_utc, created_by) VALUES (?, 'golden_reference', ?, ?, ?)",
                         (part_id, json.dumps({"level": reference, "band_hz": [1000, 40000], "model_version": version,
                                               "samples": [samples[i]["id"] for i in tr if label[i] == ml.OK]}), now, user_id))
            dbm.audit(conn, user_id, "model_trained", "part", part_id, {"version": version, "escape_rate": cv.get("escape_rate"),
                                                                         "false_reject_rate": cv.get("false_reject_rate")})
        return version
    finally:
        conn.close()


def start_training(app, part_id: int, user_id: int) -> bool:
    """Starts training in the background. False if a training is already running."""
    if not _lock.acquire(blocking=False):
        return False
    jobs = app.config.setdefault("JOBS", {})
    progress = {"state": "running", "message": "starting", "started": time.time()}
    jobs[part_id] = progress

    def run():
        try:
            progress["version"] = train_part(app.config["DB_PATH"], part_id, user_id, progress)
            progress["state"], progress["message"] = "done", f"version {progress['version']} trained"
        except Exception as exc:  # shown on the screen; the details are in the log
            app.logger.exception("training part %s failed", part_id)
            progress["state"], progress["message"] = "failed", str(exc)
        finally:
            _lock.release()

    threading.Thread(target=run, name=f"train-part-{part_id}", daemon=True).start()
    return True


def load_model(conn, part_id: int):
    """(model bundle, version) of the part's approved model, or (None, None). Refuses a changed file."""
    row = conn.execute("SELECT * FROM models WHERE part_id = ? AND state = 'approved' ORDER BY version DESC LIMIT 1",
                       (part_id,)).fetchone()
    if row is None:
        return None, None
    blob = Path(row["artifact_path"]).read_bytes()
    if hashlib.sha256(blob).hexdigest() != row["artifact_sha256"]:
        raise ValueError(f"model file {row['artifact_path']} does not match its SHA-256: it has changed since training")
    return pickle.loads(blob), row["version"]


def approve(conn, part_id: int, version: int, user_id: int) -> list[str]:
    """Approves a version, retires the one before, and deletes stored values of pruned analyses. Returns them."""
    rows = conn.execute("SELECT * FROM models WHERE part_id = ? AND version = ? AND state = 'trained'", (part_id, version)).fetchall()
    if not rows:
        raise ValueError(f"version {version} is not waiting for approval")
    pruned = json.loads(rows[0]["pruned_json"] or "{}").get("pruned", [])
    now = dbm.utcnow()
    with dbm.transaction(conn):
        conn.execute("UPDATE models SET state = 'retired' WHERE part_id = ? AND state = 'approved'", (part_id,))
        conn.execute("UPDATE models SET state = 'approved', approved_utc = ?, approved_by = ? WHERE part_id = ? AND version = ?",
                     (now, user_id, part_id, version))
        if pruned:
            for f in conn.execute("SELECT f.id, f.values_json FROM features f JOIN samples s ON s.id = f.owner_id"
                                  " WHERE f.owner_type = 'sample' AND s.part_id = ?", (part_id,)).fetchall():
                vals = {k: v for k, v in json.loads(f["values_json"]).items() if k.split(".", 1)[0] not in pruned}
                conn.execute("UPDATE features SET values_json = ? WHERE id = ?", (json.dumps(vals), f["id"]))
        dbm.audit(conn, user_id, "model_approved", "part", part_id, {"version": version, "pruned_analyses": pruned})
    return pruned
