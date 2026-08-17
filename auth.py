"""Человеческий логин -- один админ-аккаунт из .env (см. config.py,
gen_password_hash.py), сессия через подписанную cookie (Starlette
SessionMiddleware, требует PANEL_SESSION_SECRET). Токены нод -- отдельный
механизм (Bearer, см. sync_api.py), не смешивается с человеческой сессией."""
import hashlib
import hmac
import os
import sys

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
    """Bearer-токен ноды -- отдельно от человеческой сессии, для sync API.

    Заодно self-report: нода на КАЖДЫЙ authenticated-запрос (push/pull/
    bootstrap) шлёт X-Node-Name/X-Node-Provider (см.
    orchestrator/panel_client.py) -- свои собственные ZENITH_ENVIRONMENT_
    NAME/PROVIDER. Панель обновляет ими имя/провайдера этой записи, а не
    просит вводить их в веб-форме при создании токена (см. db.create_node
    -- токен создаётся пустым). Идемпотентно на каждый запрос, не только
    при первом контакте -- если оператор поменяет .env на ноде, панель
    сама подхватит на следующей синхронизации. Специально до bootstrap()
    (не только push()) -- иначе "какой у меня провайдер" не был бы
    известен ДО первой генерации, а bootstrap как раз нужен ПЕРЕД ней."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = auth_header[len("Bearer "):].strip()
    conn = db.connect()
    try:
        env = db.get_environment_by_token(conn, token)
        if not env:
            raise HTTPException(status_code=401, detail="Unknown or inactive node token")
        node_name = request.headers.get("x-node-name", "").strip()
        node_provider = request.headers.get("x-node-provider", "").strip()
        if node_name and node_provider:
            ok, provider_mismatch = db.self_report_node(conn, env["id"], node_name, node_provider)
            if not ok:
                raise HTTPException(
                    status_code=409,
                    detail=f"Имя '{node_name}' уже занято другой нодой -- смени ZENITH_ENVIRONMENT_NAME в .env",
                )
            env["name"] = node_name
            if provider_mismatch:
                # provider зафиксирован при первом self-report и больше не
                # переписывается автоматически (см. db.self_report_node)
                # -- нода прислала ДРУГОЙ provider, чем уже сохранён.
                # Не блокируем запрос (name/last_sync_at всё равно
                # обновляются), но громко логируем -- либо оператор
                # реально сменил ZENITH_ENVIRONMENT_PROVIDER (тогда смена
                # provider делается на /nodes руками), либо это стоит
                # заметить (найдено при аудите перед деплоем на МТС
                # 2026-08-17).
                print(
                    f"self_report_node: нода '{node_name}' (id={env['id']}) прислала provider='{node_provider}', "
                    f"но у неё уже зафиксирован другой -- provider НЕ изменён, поменяй на /nodes руками, если это осознанно.",
                    file=sys.stderr,
                )
            else:
                env["provider"] = node_provider
        # last_sync_at раньше обновлялся только внутри sync_push -- ноды в
        # ZENITH_DB_MODE=api никогда не зовут push (у них нет локального
        # снапшота, который пушить), поэтому у них колонка молча оставалась
        # пустой навсегда, хотя нода реально активна на каждый раунд.
        # require_node -- общая точка для ВСЕХ authenticated-запросов
        # (push/pull/bootstrap И db_api.py), обновляем тут одним местом.
        db.touch_node_sync(conn, env["id"])
    finally:
        conn.close()
    return env
