"""Человеческий логин -- один админ-аккаунт из .env (см. config.py,
gen_password_hash.py), сессия через подписанную cookie (Starlette
SessionMiddleware, требует PANEL_SESSION_SECRET). Токены нод -- отдельный
механизм (Bearer, см. sync_api.py), не смешивается с человеческой сессией."""
import hashlib
import hmac
import os

from fastapi import HTTPException, Request
from starlette.responses import RedirectResponse

import config
import db

PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, salt: bytes = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored or "$" not in stored:
        return False
    salt_hex, _, digest_hex = stored.partition("$")
    salt = bytes.fromhex(salt_hex)
    expected = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(expected.hex(), digest_hex)


class NotAuthenticated(Exception):
    pass


def require_login(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise NotAuthenticated()
    return user


def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url=f"/login?next={request.url.path}", status_code=302)


def require_node(request: Request) -> dict:
    """Bearer-токен ноды -- отдельно от человеческой сессии, для sync API."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = auth[len("Bearer "):].strip()
    conn = db.connect()
    try:
        env = db.get_environment_by_token(conn, token)
    finally:
        conn.close()
    if not env:
        raise HTTPException(status_code=401, detail="Unknown or inactive node token")
    return env
