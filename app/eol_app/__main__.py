"""Command line: python3 -m eol_app {serve,migrate,reset-master,info}"""

from __future__ import annotations

import argparse
import sys

from eol_acquire.paths import acquire_socket, data_dir

from . import db as dbm
from .security import hash_password


def main() -> int:
    ap = argparse.ArgumentParser(prog="eol_app", description="EOL test stand application")
    ap.add_argument("--db", default=None, help=f"database file (default {data_dir() / 'eol.sqlite3'})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="run the web application")
    sv.add_argument("--host", default="127.0.0.1", help="127.0.0.1 = this machine only (default); 0.0.0.0 = the network too")
    sv.add_argument("--port", type=int, default=8080)
    sv.add_argument("--acquire-socket", default=None, help=f"acquisition service socket (default {acquire_socket()})")
    sub.add_parser("migrate", help="create or update the database, then stop")
    sub.add_parser("reset-master", help="give the master login a new temporary password (recovery; needs a shell on the CM5)")
    sub.add_parser("info", help="database file, schema version, counts")
    args = ap.parse_args()
    db_path = args.db or str(data_dir() / "eol.sqlite3")

    if args.cmd == "serve":
        from .web import create_app
        app = create_app(db_path, args.acquire_socket)
        try:
            from waitress import serve
        except ImportError:
            print("waitress is not installed; using Flask's own server")
            app.run(host=args.host, port=args.port, threaded=True)
        else:
            print(f"EOL application on http://{args.host}:{args.port}/  (database {db_path})")
            serve(app, host=args.host, port=args.port, threads=8)
        return 0

    conn = dbm.open_database(db_path, hash_password)
    if args.cmd == "migrate":
        print(f"{db_path}: schema version {dbm.schema_version(conn)}")
    elif args.cmd == "info":
        print(f"database        {db_path}")
        print(f"schema version  {dbm.schema_version(conn)}")
        for table in ("users", "parts", "failure_modes", "samples", "recordings", "test_results", "audit_log"):
            print(f"{table:15} {conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]}")
    elif args.cmd == "reset-master":
        from .admin_views import temporary_password
        pw = temporary_password()
        with dbm.transaction(conn):
            conn.execute("UPDATE users SET password_hash = ?, must_change_password = 1, failed_logins = 0, locked_until_utc = NULL,"
                         " active = 1 WHERE is_master = 1", (hash_password(pw),))
            dbm.audit(conn, None, "master_reset", "user", dbm.MASTER_USERNAME, {"by": "command line on the CM5"})
        print(f"master's temporary password: {pw}  (it must be changed at the next login)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
