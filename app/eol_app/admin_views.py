"""Programming: users, programming variables, audit log. Programming role only."""

from __future__ import annotations

import re
import secrets

from flask import Flask, flash, g, redirect, render_template, request, url_for

from . import db as dbm
from .security import hash_password
from .web import login_required, row_or_404

USERNAME = re.compile(r"[A-Za-z0-9._-]{1,32}")


def temporary_password() -> str:
    return "t" + secrets.token_hex(4) + "9"  # letters and digits; must be changed at first login


def register_admin(app: Flask) -> None:
    @app.get("/admin/users")
    @login_required("programming")
    def admin_users():
        users = g.db.execute("SELECT * FROM users ORDER BY is_master DESC, role, username").fetchall()
        return render_template("admin_users.html", users=users)

    @app.post("/admin/users")
    @login_required("programming")
    def admin_user_create():
        name, role = request.form.get("username", "").strip(), request.form.get("role", "")
        if not USERNAME.fullmatch(name) or role not in ("programming", "setup", "testing"):
            flash("User name: 1 to 32 letters, digits, . _ -; role: programming, setup or testing.")
        elif g.db.execute("SELECT 1 FROM users WHERE username = ?", (name,)).fetchone():
            flash(f"User {name} already exists.")
        else:
            pw = temporary_password()
            with dbm.transaction(g.db):
                cur = g.db.execute("INSERT INTO users (username, role, password_hash, must_change_password, created_utc, created_by)"
                                   " VALUES (?, ?, ?, 1, ?, ?)", (name, role, hash_password(pw), dbm.utcnow(), g.user["user_id"]))
                dbm.audit(g.db, g.user["user_id"], "user_created", "user", cur.lastrowid, {"username": name, "role": role})
            flash(f"User {name} created. Temporary password: {pw} (it must be changed at first login).")
        return redirect(url_for("admin_users"))

    @app.post("/admin/users/<int:user_id>/<action>")
    @login_required("programming")
    def admin_user_action(user_id, action):
        u = row_or_404(g.db, "SELECT * FROM users WHERE id = ?", user_id)
        if u["is_master"] and action in ("deactivate", "role") and not g.user["is_master"]:
            flash("Only the master can change the master login.")
            return redirect(url_for("admin_users"))
        with dbm.transaction(g.db):
            if action == "reset":
                pw = temporary_password()
                g.db.execute("UPDATE users SET password_hash = ?, must_change_password = 1, failed_logins = 0, locked_until_utc = NULL"
                             " WHERE id = ?", (hash_password(pw), user_id))
                g.db.execute("UPDATE sessions SET ended_utc = ? WHERE user_id = ? AND ended_utc IS NULL", (dbm.utcnow(), user_id))
                flash(f"New temporary password for {u['username']}: {pw}")
            elif action == "unlock":
                g.db.execute("UPDATE users SET failed_logins = 0, locked_until_utc = NULL WHERE id = ?", (user_id,))
                flash(f"{u['username']} unlocked.")
            elif action in ("deactivate", "activate"):
                if u["is_master"]:
                    flash("The master login cannot be deactivated.")
                    return redirect(url_for("admin_users"))
                g.db.execute("UPDATE users SET active = ? WHERE id = ?", (1 if action == "activate" else 0, user_id))
                if action == "deactivate":
                    g.db.execute("UPDATE sessions SET ended_utc = ? WHERE user_id = ? AND ended_utc IS NULL", (dbm.utcnow(), user_id))
                flash(f"{u['username']} {action}d.")
            elif action == "role":
                role = request.form.get("role", "")
                if u["is_master"] or role not in ("programming", "setup", "testing"):
                    flash("That role change is not allowed.")
                    return redirect(url_for("admin_users"))
                g.db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
                flash(f"{u['username']} is now {role}.")
            else:
                return redirect(url_for("admin_users"))
            dbm.audit(g.db, g.user["user_id"], f"user_{action}", "user", user_id, {"username": u["username"]})
        return redirect(url_for("admin_users"))

    @app.route("/admin/settings", methods=["GET", "POST"])
    @login_required("programming")
    def admin_settings():
        if request.method == "POST":
            key = request.form.get("key", "")
            s = row_or_404(g.db, "SELECT * FROM settings WHERE key = ?", key)
            raw = request.form.get("value", "").strip()
            try:
                value = int(raw) if s["kind"] == "int" else float(raw) if s["kind"] == "float" else raw
            except ValueError:
                flash(f"{key}: not a number.")
                return redirect(url_for("admin_settings"))
            if s["kind"] in ("int", "float") and not (s["min_value"] <= value <= s["max_value"]):
                flash(f"{key}: {s['min_value']:g} to {s['max_value']:g}.")
            else:
                with dbm.transaction(g.db):
                    g.db.execute("UPDATE settings SET value = ?, updated_utc = ?, updated_by = ? WHERE key = ?",
                                 (str(value), dbm.utcnow(), g.user["user_id"], key))
                    dbm.audit(g.db, g.user["user_id"], "setting_changed", "setting", key, {"from": s["value"], "to": str(value)})
                flash(f"{key} = {value}")
            return redirect(url_for("admin_settings"))
        return render_template("admin_settings.html", settings=g.db.execute("SELECT * FROM settings ORDER BY key").fetchall())

    @app.get("/admin/audit")
    @login_required("programming")
    def admin_audit():
        rows = g.db.execute("SELECT a.*, u.username FROM audit_log a LEFT JOIN users u ON u.id = a.user_id"
                            " ORDER BY a.id DESC LIMIT 300").fetchall()
        return render_template("admin_audit.html", rows=rows)
