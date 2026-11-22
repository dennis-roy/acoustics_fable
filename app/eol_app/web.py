"""The web application: login, Setup, Programming. Served on the CM5 and shown on its touch screen.

Roles (spec, Access): programming (master and engineers) can do everything; setup does
parts, failure modes and calibrated samples; testing does testing. Every change is in
the audit log. Sessions are a random token in an HttpOnly cookie; only its SHA-256 is
stored. Every form carries a per-session token (CSRF) that the server checks.
"""

from __future__ import annotations

import functools
import json
import os
import secrets
import sqlite3
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for

from . import db as dbm
from .security import hash_password, new_token, password_problem, token_hash, utc_after, verify_password

COOKIE = "eol_session"
AREAS = {"setup": {"programming", "setup"}, "testing": {"programming", "setup", "testing"},
         "programming": {"programming"}}


def create_app(db_path: str | Path, acquire_socket: str | Path | None = None, secure_cookie: bool = False) -> Flask:
    app = Flask(__name__)
    app.secret_key = _secret_key(Path(db_path))
    app.config.update(DB_PATH=str(db_path), ACQUIRE_SOCKET=str(acquire_socket) if acquire_socket else None,
                      SECURE_COOKIE=secure_cookie, PENDING={})
    dbm.open_database(db_path, hash_password).close()

    @app.before_request
    def load_session():
        g.db = dbm.connect(app.config["DB_PATH"])
        g.user = g.session = None
        token = request.cookies.get(COOKIE)
        if token:
            row = g.db.execute(
                "SELECT s.*, u.username, u.role, u.must_change_password, u.is_master, u.active FROM sessions s"
                " JOIN users u ON u.id = s.user_id WHERE s.token_sha256 = ? AND s.ended_utc IS NULL",
                (token_hash(token),)).fetchone()
            idle = dbm.setting(g.db, "session_idle_minutes")
            if row and row["active"] and row["last_seen_utc"] >= utc_after(-idle):
                g.session, g.user = row, row
                g.db.execute("UPDATE sessions SET last_seen_utc = ? WHERE token_sha256 = ?",
                             (dbm.utcnow(), row["token_sha256"]))
            elif row:
                g.db.execute("UPDATE sessions SET ended_utc = ? WHERE token_sha256 = ?", (dbm.utcnow(), row["token_sha256"]))
        if request.method == "POST" and request.endpoint not in ("login", "static"):
            if not g.session or request.form.get("csrf") != g.session["csrf_token"]:
                abort(403)
        if g.user and g.user["must_change_password"] and request.endpoint not in ("password", "logout", "static"):
            return redirect(url_for("password"))

    @app.teardown_request
    def close_db(exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.context_processor
    def common():
        station = g.db.execute("SELECT name FROM stations ORDER BY id LIMIT 1").fetchone() if "db" in g else None
        return {"user": g.get("user"), "csrf": g.session["csrf_token"] if g.get("session") else "",
                "station": station["name"] if station else "", "has": lambda endpoint: endpoint in app.view_functions,
                "can": lambda area: bool(g.get("user")) and g.user["role"] in AREAS[area]}

    register_auth(app)
    from .setup_views import register_setup
    from .admin_views import register_admin
    register_setup(app)
    register_admin(app)
    return app


def _secret_key(db_path: Path) -> bytes:
    """Signs the short messages shown after an action. Made once, kept beside the database, owner-readable only."""
    path = db_path.with_name(db_path.name + ".secret")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(secrets.token_bytes(32))
    return path.read_bytes()


def login_required(area: str | None = None):
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*a, **kw):
            if not g.user:
                return redirect(url_for("login", next=request.path))
            if area and g.user["role"] not in AREAS[area]:
                abort(403)
            return fn(*a, **kw)
        return inner
    return wrap


def register_auth(app: Flask) -> None:
    @app.get("/")
    @login_required()
    def home():
        return render_template("home.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            return render_template("login.html")
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = g.db
        u = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        now = dbm.utcnow()
        if u and u["locked_until_utc"] and u["locked_until_utc"] > now:
            flash(f"This account is locked until {u['locked_until_utc']} (UTC) after too many wrong passwords.")
            return render_template("login.html"), 403
        if not u or not u["active"] or not verify_password(password, u["password_hash"]):
            with dbm.transaction(conn):
                if u:
                    fails = u["failed_logins"] + 1
                    locked = None
                    if fails >= dbm.setting(conn, "lockout_attempts"):
                        locked = utc_after(dbm.setting(conn, "lockout_minutes"))
                        dbm.audit(conn, u["id"], "account_locked", "user", u["username"], {"until": locked})
                    conn.execute("UPDATE users SET failed_logins = ?, locked_until_utc = ? WHERE id = ?",
                                 (0 if locked else fails, locked, u["id"]))
                dbm.audit(conn, u["id"] if u else None, "login_failed", "user", username)
            flash("Wrong user name or password.")
            return render_template("login.html"), 401
        token, digest = new_token()
        with dbm.transaction(conn):
            conn.execute("UPDATE users SET failed_logins = 0, locked_until_utc = NULL WHERE id = ?", (u["id"],))
            conn.execute("INSERT INTO sessions (token_sha256, user_id, csrf_token, created_utc, last_seen_utc) VALUES (?, ?, ?, ?, ?)",
                         (digest, u["id"], new_token()[0], now, now))
            conn.execute("INSERT INTO context_events (at_utc, station_id, kind, value, user_id) VALUES (?, 1, 'operator', ?, ?)",
                         (now, u["username"], u["id"]))
            dbm.audit(conn, u["id"], "login", "user", u["username"])
        nxt = request.args.get("next", "")
        resp = redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("home"))
        resp.set_cookie(COOKIE, token, httponly=True, samesite="Strict", secure=app.config["SECURE_COOKIE"])
        return resp

    @app.post("/logout")
    def logout():
        if g.session:
            with dbm.transaction(g.db):
                g.db.execute("UPDATE sessions SET ended_utc = ? WHERE token_sha256 = ?", (dbm.utcnow(), g.session["token_sha256"]))
                dbm.audit(g.db, g.user["user_id"], "logout", "user", g.user["username"])
        resp = redirect(url_for("login"))
        resp.delete_cookie(COOKIE)
        return resp

    @app.route("/password", methods=["GET", "POST"])
    @login_required()
    def password():
        if request.method == "GET":
            return render_template("password.html")
        u = g.db.execute("SELECT * FROM users WHERE id = ?", (g.user["user_id"],)).fetchone()
        new = request.form.get("new", "")
        if not verify_password(request.form.get("current", ""), u["password_hash"]):
            flash("The current password is wrong.")
        elif new != request.form.get("repeat", ""):
            flash("The two new passwords differ.")
        elif verify_password(new, u["password_hash"]):
            flash("The new password must differ from the old one.")
        elif problem := password_problem(new, u["username"]):
            flash(f"New password: {problem}.")
        else:
            with dbm.transaction(g.db):
                g.db.execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?", (hash_password(new), u["id"]))
                dbm.audit(g.db, u["id"], "password_changed", "user", u["username"])
            flash("Password changed.")
            return redirect(url_for("home"))
        return render_template("password.html"), 400


def json_or_none(value) -> str | None:
    return json.dumps(value) if value is not None else None


def row_or_404(conn: sqlite3.Connection, sql: str, *args):
    row = conn.execute(sql, args).fetchone()
    if row is None:
        abort(404)
    return row
