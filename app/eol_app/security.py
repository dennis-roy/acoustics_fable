"""Passwords (scrypt, from Python's standard library), lockout and sessions."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time

N, R, P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return f"scrypt${N}${R}${P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


def password_problem(password: str, username: str = "") -> str | None:
    """None if the password is acceptable, else why not."""
    if len(password) < 8:
        return "at least 8 characters"
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "at least one letter and one digit"
    if username and username.lower() in password.lower():
        return "must not contain the user name"
    return None


def new_token() -> tuple[str, str]:
    """(token for the browser, its SHA-256 for the database). Only the hash is stored."""
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def utc_after(minutes: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + minutes * 60))
