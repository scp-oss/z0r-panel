#!/usr/bin/env python3
"""z0r-panel: веб-панель для Zenith (scp-oss/Zenith) -- обзор геномов по
нодам/провайдерам, история мутаций конкретных стратегий, read-only
статус боевых профилей. Плюс sync API для удалённых нод (см.
sync_api.py). Отдельный репозиторий от Zenith (был вложенной
Zenith/panel/), но работает НА той же машине, что локальный
orchestrator, и делит с ним ту же MySQL (см. config.py) -- отдельной БД
для панели нет.

Границы ответственности панели (сознательно, по прямому запросу --
"только через существующие скрипты"): панель НИКОГДА не пишет в боевой
/opt/zapret2/config и не запускает set_strategy_cli.sh set откуда-либо в
UI. Она только читает (get/max) и отображает то, что Zenith'овские
orchestrator/main.py и promote.py уже посчитали -- финальное применение
остаётся ручным шагом человека, как и раньше.

Запуск: см. run.sh / README.md (systemd-юнит zenith-panel.service).
"""
import base64
import json
import subprocess
import sys

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import auth
import config
import db
import sync_api

if not config.PANEL_SESSION_SECRET:
    print("PANEL_SESSION_SECRET не задан в .env -- сессии не переживут рестарт панели. "
          "Сгенерируй: python3 -c 'import secrets; print(secrets.token_hex(32))'", file=sys.stderr)

app = FastAPI(title="Zenith panel")
app.add_middleware(
    SessionMiddleware,
    secret_key=config.PANEL_SESSION_SECRET or "dev-insecure-change-me",
    https_only=config.PANEL_COOKIE_HTTPS_ONLY,
)
app.add_exception_handler(auth.NotAuthenticated, auth.not_authenticated_handler)
app.include_router(sync_api.router)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Совпадает с orchestrator/promote.py::PROFILE_NUMBERS -- дублируется
# сознательно (см. z2r_test-voice-bot/bot.py::VOICE_FILTER_LINES для того
# же паттерна): панель и orchestrator -- разные процессы на одном хосте,
# общего python-модуля между ними нет, а тянуть sys.path в соседний
# каталог ради одного словаря лишняя связанность.
PROFILE_NUMBERS = {"YT_TLS": 1, "GV_TLS": 2, "RKN_TLS": 3, "DS_TLS": 4, "VOICE_UDP": 6}
PROFILE_PROTO = {"VOICE_UDP": "udp"}


def make_connect_string(**fields) -> str:
    """base64(JSON), одна непрозрачная строка вместо отдельных полей --
    как VLESS/Outline/WireGuard-ключи (по прямому запросу). z0r
    (z2r_autobench) декодирует её сам через decode_connect_string() --
    формат {"m": "panel"|"db", ...} общий с
    Zenith/db/create_remote_db_user.sh (режим "db"), см. его исходник."""
    raw = json.dumps(fields, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _run_cli(*args) -> str | None:
    try:
        out = subprocess.run(
            ["sudo", "-n", "bash", config.SET_STRATEGY_CLI, *args],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except Exception:
        return None


@app.get("/login")
def login_form(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": None})


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    if username == config.PANEL_ADMIN_USER and auth.verify_password(password, config.PANEL_ADMIN_PASSWORD_HASH):
        request.session["user"] = username
        return RedirectResponse(url=next or "/", status_code=302)
    return templates.TemplateResponse(
        request, "login.html", {"next": next, "error": "Неверный логин или пароль"}, status_code=401,
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


@app.get("/")
def overview(request: Request, user: str = Depends(auth.require_login)):
    conn = db.connect()
    try:
        rows = db.overview_rows(conn)
    finally:
        conn.close()
    by_profile = {}
    for row in rows:
        by_profile.setdefault(row["profile"], []).append(row)
    return templates.TemplateResponse(request, "overview.html", {"user": user, "by_profile": by_profile})


@app.get("/nodes")
def nodes_page(request: Request, user: str = Depends(auth.require_login)):
    conn = db.connect()
    try:
        environments = db.list_environments(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        request, "nodes.html",
        {"user": user, "environments": environments, "new_token": None, "new_token_uuid": None, "connect_string": None},
    )


@app.post("/nodes")
def create_node(request: Request, user: str = Depends(auth.require_login)):
    """Токен создаётся ПУСТЫМ -- ни имя, ни провайдер тут не спрашиваются
    (см. db.create_node): нода сообщает их сама при первом обращении к
    панели, из своих ZENITH_ENVIRONMENT_NAME/PROVIDER -- то же значение
    оператор и так вводит на самой ноде при установке, вводить второй раз
    в этой форме незачем."""
    conn = db.connect()
    try:
        token, node_uuid = db.create_node(conn)
        environments = db.list_environments(conn)
    finally:
        conn.close()
    connect_string = (
        make_connect_string(m="panel", url=config.PANEL_PUBLIC_URL, token=token)
        if config.PANEL_PUBLIC_URL else None
    )
    return templates.TemplateResponse(
        request, "nodes.html",
        {
            "user": user, "environments": environments, "new_token": token, "new_token_uuid": node_uuid,
            "panel_public_url": config.PANEL_PUBLIC_URL, "connect_string": connect_string,
        },
    )


@app.get("/genome/{genome_id}")
def genome_detail(request: Request, genome_id: str, user: str = Depends(auth.require_login)):
    conn = db.connect()
    try:
        genome = db.get_genome(conn, genome_id)
        scores = db.genome_scores_by_env(conn, genome_id) if genome else []
        ancestors = db.genome_ancestors(conn, genome_id) if genome else []
        children = db.genome_children(conn, genome_id) if genome else []
    finally:
        conn.close()
    if not genome:
        return templates.TemplateResponse(request, "login.html", {"next": "/", "error": "Геном не найден"}, status_code=404)
    return templates.TemplateResponse(
        request, "genome.html",
        {"user": user, "genome": genome, "scores": scores, "ancestors": ancestors, "children": children},
    )


@app.get("/knowledge")
def knowledge_page(request: Request, user: str = Depends(auth.require_login)):
    conn = db.connect()
    try:
        rollup = db.knowledge_family_rollup(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "knowledge.html", {"user": user, "rollup": rollup})


@app.get("/controls")
def controls_page(request: Request, user: str = Depends(auth.require_login)):
    conn = db.connect()
    try:
        local_env_id = db.get_or_create_local_environment(conn)
        by_profile = {
            profile: db.controls_for_profile(conn, profile, local_env_id)
            for profile in PROFILE_NUMBERS
        }
    finally:
        conn.close()

    profile_status = []
    for profile, num in PROFILE_NUMBERS.items():
        proto = PROFILE_PROTO.get(profile, "tls")
        locked = _run_cli("get", str(num), proto)
        max_strat = _run_cli("max", str(num))
        profile_status.append({
            "profile": profile, "num": num, "proto": proto,
            "locked": locked, "max": max_strat,
        })

    return templates.TemplateResponse(
        request, "controls.html",
        {
            "user": user, "profile_status": profile_status, "by_profile": by_profile,
            "local_env_name": config.LOCAL_ENVIRONMENT_NAME,
        },
    )


if __name__ == "__main__":
    import uvicorn

    if config.PANEL_TLS_CERT and config.PANEL_TLS_KEY:
        # Публичный TLS-порт -- слушаем на всех интерфейсах вне
        # зависимости от PANEL_HOST (та настройка -- только для
        # HTTP-fallback-режима ниже).
        uvicorn.run(
            app, host="0.0.0.0", port=config.PANEL_PORT,
            ssl_certfile=config.PANEL_TLS_CERT, ssl_keyfile=config.PANEL_TLS_KEY,
        )
    else:
        uvicorn.run(app, host=config.PANEL_HOST, port=config.PANEL_PORT)
