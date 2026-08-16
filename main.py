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
import daemon_ctl
import db
import db_api
import runner
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
app.include_router(db_api.router)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Все 9 профилей z2r (тот же порядок, что в z0r::show_menu "111) Тестировать
# все профили" и в rank_strategies.sh case PROFILE) -- статус-таблица на
# /controls читает set_strategy_cli.sh get/max для ЛЮБОГО из них, это
# read-only, ошибиться нечем. YT_QUIC_UDP=5/GAMES_UDP=7 -- [dev]-заглушки
# в самом z2r (ещё не реализованы), get/max для них ожидаемо может вернуть
# "ошибка чтения" -- не баг панели. FB_TLS=8/FB_HTTP=9 ("Fallback_TLS"/
# "Fallback_HTTP" в rank_strategies.sh) хранятся в отдельном
# locked.manual.tsv (см. z2r_autobench_lib.sh::set_strategy/get_strategy),
# но тот же get/max CLI их тоже читает. Живой инцидент 2026-08-15: раньше
# тут были только 5 профилей, "№" в таблице выглядел как случайный обрубок
# 1,2,3,4,6.
PROFILE_NUMBERS = {
    "YT_TLS": 1, "GV_TLS": 2, "RKN_TLS": 3, "DS_TLS": 4,
    "YT_QUIC_UDP": 5, "VOICE_UDP": 6, "GAMES_UDP": 7,
    "FB_TLS": 8, "FB_HTTP": 9,
}
PROFILE_PROTO = {"VOICE_UDP": "udp", "YT_QUIC_UDP": "udp", "GAMES_UDP": "udp", "FB_HTTP": "http"}

# Подмножество PROFILE_NUMBERS, которое Zenith'овский orchestrator/main.py
# реально умеет запускать -- см. Zenith/orchestrator/genome.py::
# PROFILE_FILTER_TYPE/PROFILE_FILTERS, там определены ТОЛЬКО эти 4 (ни
# GV_TLS, ни fallback/dev-профили выше там не описаны -- main.py --profile
# GV_TLS не упадёт сразу, но результат ничем не подкреплён реальным боевым
# фильтром песочницы, см. genome.py докстринг про живой разрыв
# достоверности песочницы). Кнопка "запустить подбор" на /controls
# показывает только эти -- остальные профили в статус-таблице видны, но
# не запускаемы с панели.
RUNNABLE_PROFILES = ["YT_TLS", "RKN_TLS", "DS_TLS", "VOICE_UDP"]


def make_connect_string(**fields) -> str:
    """base64(JSON), одна непрозрачная строка вместо отдельных полей --
    как VLESS/Outline/WireGuard-ключи (по прямому запросу). z0r
    (z2r_autobench) декодирует её сам через decode_connect_string() --
    формат {"m": "panel", url, token, ...}. Один и тот же формат для
    ноды с локальным буфером (hub-and-spoke, sync_api.py push/pull) и
    для ноды без своей БД вообще (ZENITH_DB_MODE=api, db_api.py) -- обе
    ходят сюда по HTTP+токену, разница только в частоте вызова, не в
    транспорте. Сырой MySQL наружу больше не открывается ни для какой
    ноды (см. Zenith db/schema.sql комментарий про environments)."""
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


@app.post("/nodes/{environment_id}/delete")
def delete_node(request: Request, environment_id: int, user: str = Depends(auth.require_login)):
    """Только для нод БЕЗ истории (db.delete_environment сам это
    перепроверяет, шаблон только прячет кнопку -- не единственная защита).
    Типичный случай -- 'pending-<uuid8>', токен выписали, но нода так и
    не подключилась (или тестовый прогон, который решили не продолжать)."""
    conn = db.connect()
    try:
        if db.get_environment_genome_count(conn, environment_id) > 0:
            environments = db.list_environments(conn)
            return templates.TemplateResponse(
                request, "nodes.html",
                {
                    "user": user, "environments": environments, "new_token": None,
                    "new_token_uuid": None, "connect_string": None,
                    "error": "У ноды есть история (genome_scores) -- удалить нельзя, только деактивировать.",
                },
                status_code=409,
            )
        db.delete_environment(conn, environment_id)
    finally:
        conn.close()
    return RedirectResponse(url="/nodes", status_code=303)


@app.post("/nodes/{environment_id}/force-delete")
def force_delete_node(environment_id: int, user: str = Depends(auth.require_login)):
    """Удаление ЛЮБОЙ ноды по прямому запросу, вместе с историей --
    в отличие от delete_node выше, тут genome_count НЕ проверяется.
    Шаблон требует более серьёзное подтверждение (ввод имени ноды), чем
    у обычного удаления, но сама защита -- на уровне UI/confirm, не
    сервера (сервер тут доверяет человеческой сессии полностью, как и
    остальные /nodes-роуты)."""
    conn = db.connect()
    try:
        db.force_delete_environment(conn, environment_id)
    finally:
        conn.close()
    return RedirectResponse(url="/nodes", status_code=303)


@app.post("/nodes/{environment_id}/reissue-token")
def reissue_node_token(request: Request, environment_id: int, user: str = Depends(auth.require_login)):
    """Новый токен для уже существующей (обычно ещё не откликнувшейся)
    ноды -- показывается один раз, тем же блоком, что и при создании
    (см. create_node выше). Старый токен сразу перестаёт работать."""
    conn = db.connect()
    try:
        token = db.reissue_node_token(conn, environment_id)
        environments = db.list_environments(conn)
    finally:
        conn.close()
    node_uuid = next((e["node_uuid"] for e in environments if e["id"] == environment_id), None)
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


@app.post("/nodes/{environment_id}/toggle-active")
def toggle_node_active(request: Request, environment_id: int, user: str = Depends(auth.require_login)):
    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT active FROM environments WHERE id=%s", (environment_id,))
        row = cur.fetchone()
        if row:
            db.set_environment_active(conn, environment_id, not row[0])
    finally:
        conn.close()
    return RedirectResponse(url="/nodes", status_code=303)


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


@app.post("/genome/{genome_id}/promote-strategy")
def genome_promote_strategy(
    genome_id: str, user: str = Depends(auth.require_login),
    environment_id: int = Form(...), strategy: str = Form(""),
):
    """Ручная отметка -- панель ничего сама не продвигает (см. README
    "Границы ответственности панели"), это просто запись факта: человек
    уже сделал promote.py + вставил блок в /opt/zapret2/config + перезапустил
    zapret2 руками, и теперь говорит панели, каким номером strategy=N это
    легло, чтобы /genome/<id> и /overview показывали это, а не только
    голый rendered_args. Пустое поле -- снять отметку."""
    strategy = strategy.strip()
    try:
        strategy_n = int(strategy) if strategy else None
    except ValueError:
        strategy_n = None
    conn = db.connect()
    try:
        db.set_promoted_strategy(conn, genome_id, environment_id, strategy_n)
    finally:
        conn.close()
    return RedirectResponse(url=f"/genome/{genome_id}", status_code=303)


@app.get("/knowledge")
def knowledge_page(request: Request, user: str = Depends(auth.require_login)):
    conn = db.connect()
    try:
        rollup = db.knowledge_family_rollup(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "knowledge.html", {"user": user, "rollup": rollup})


def _controls_context(user: str, run_error: str | None = None, daemon_error: str | None = None) -> dict:
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

    return {
        "user": user, "profile_status": profile_status, "by_profile": by_profile,
        "local_env_name": config.LOCAL_ENVIRONMENT_NAME,
        "run_profiles": RUNNABLE_PROFILES,
        "run_error": run_error,
        "daemon_error": daemon_error,
    }


@app.get("/controls")
def controls_page(request: Request, user: str = Depends(auth.require_login)):
    return templates.TemplateResponse(request, "controls.html", _controls_context(user))


@app.post("/controls/run")
def controls_run(
    request: Request, user: str = Depends(auth.require_login),
    profile: str = Form(...), rounds: int = Form(20), domain: str = Form(""),
):
    if profile not in RUNNABLE_PROFILES:
        error = "Неизвестный профиль."
    else:
        error = runner.start(profile, max(1, min(rounds, 500)), domain.strip() or None)
    return templates.TemplateResponse(request, "controls.html", _controls_context(user, run_error=error))


@app.post("/controls/run/stop")
def controls_run_stop(request: Request, user: str = Depends(auth.require_login)):
    error = runner.stop()
    return templates.TemplateResponse(request, "controls.html", _controls_context(user, run_error=error))


@app.get("/controls/run/status")
def controls_run_status(user: str = Depends(auth.require_login)):
    return runner.status()


@app.post("/controls/daemon/start")
def controls_daemon_start(request: Request, user: str = Depends(auth.require_login)):
    error = daemon_ctl.start()
    return templates.TemplateResponse(request, "controls.html", _controls_context(user, daemon_error=error))


@app.post("/controls/daemon/stop")
def controls_daemon_stop(request: Request, user: str = Depends(auth.require_login)):
    error = daemon_ctl.stop()
    return templates.TemplateResponse(request, "controls.html", _controls_context(user, daemon_error=error))


@app.get("/controls/daemon/status")
def controls_daemon_status(user: str = Depends(auth.require_login)):
    return {"active": daemon_ctl.is_active(), "log": daemon_ctl.log_tail()}


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
