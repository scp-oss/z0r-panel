#!/usr/bin/env python3
"""z0r-panel: веб-панель для Zenith (scp-oss/Zenith) -- обзор геномов по
нодам/провайдерам, история мутаций конкретных стратегий, read-only
статус боевых профилей. Плюс sync API для удалённых нод (см.
sync_api.py). Отдельный репозиторий от Zenith (был вложенной
Zenith/panel/), но работает НА той же машине, что локальный
orchestrator, и делит с ним ту же MySQL (см. config.py) -- отдельной БД
для панели нет.

Границы ответственности панели прошли два осознанных изменения от
исходного "НИКОГДА не пишет в боевой /opt/zapret2/config" (только
get/max):
  - 2026-08-15: /controls научилась set_strategy_cli.sh set (ручное
    переключение) и restart zapret2 -- то же самое действие, что
    человек делает через z0r пункт 21, просто кнопкой вместо SSH,
    исполняет только то, что человек явно указал в форме;
  - 2026-08-16 (по прямому запросу "цель: автономная работа без
    человеческого вмешательства"): добавлено автопродвижение. Логика
    (find candidate -> apply -> set -> restart -> verify -> rollback)
    живёт НЕ в панели, а в Zenith'овском orchestrator/auto_promoter.py
    как systemd-юнит zenith-promoter.service -- НЕЗАВИСИМО от того,
    установлена панель или нет (по прямому запросу "панель это модуль...
    функционал должен быть в CLI и без панели"). Панель на /controls --
    ТОЛЬКО пульт (start/stop/log) поверх этого же юнита, см. daemon_ctl.py,
    так же, как для autotune-daemon и zenith-autorun.

Запуск: см. run.sh / README.md (systemd-юнит zenith-panel.service).
"""
import base64
import json
import subprocess

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import auth
import autoupdate_ctl
import config
import daemon_ctl
import db
import db_api
import runner
import sync_api

app = FastAPI(title="Zenith panel")
app.add_middleware(
    SessionMiddleware,
    secret_key=config.PANEL_SESSION_SECRET,
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

# Подмножество PROFILE_NUMBERS, которое Zenith умеет запускать -- см.
# runner.RUNNABLE_PROFILES докстринг. Кнопка "запустить подбор" на
# /controls показывает только эти -- остальные профили в статус-таблице
# видны, но не запускаемы с панели.


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


def _probe_real(host: str, path: str, min_bytes: int, timeout: int = 8) -> tuple:
    """curl обычным процессом панели, БЕЗ sandbox-скоупа -- та же логика,
    что и Zenith'овский orchestrator/auto_promoter.py::_probe_real() (см.
    её докстринг: tester.py::probe() тестирует ТОЛЬКО zenith-sandbox юзера
    через отдельный iptables-скоуп, не годится для проверки того, что
    реально видят пользователи через боевой zapret2)."""
    url = f"https://{host}{path or '/'}"
    try:
        out = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{size_download}",
             "--connect-timeout", "5", "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 3,
        )
        bytes_ = int((out.stdout or "0").strip() or 0)
    except Exception:
        bytes_ = 0
    return bytes_ >= min_bytes, bytes_


def _real_traffic_check(profile: str) -> str:
    """Живая проверка живым curl по доменам профиля из domain_pool --
    раньше "Ручное переключение стратегии" верило только коду возврата
    set_strategy_cli.sh/systemctl restart (см. main.py докстринг про
    circular_locked без TTL), а не тому, реально ли работает трафик --
    та же дыра, что уже нашли и закрыли в Zenith auto_promoter.py.
    Найдено при аудите перед деплоем на Provider B 2026-08-17. Кнопка на
    /controls -- ТОЛЬКО показывает результат, ничего не откатывает сама
    (в отличие от автопродвижения, тут за рулём человек, он сам решает,
    что делать с результатом)."""
    conn = db.connect()
    try:
        domains = db.get_domains_for_profile(conn, profile)
    finally:
        conn.close()
    if not domains:
        return f"{profile}: нет доменов в domain_pool для этого профиля -- нечем проверить."
    results = []
    all_ok = True
    for d in domains:
        ok, bytes_ = _probe_real(d["host"], d["path"], d["min_bytes"])
        all_ok = all_ok and ok
        mark = "OK" if ok else "ПРОВАЛ"
        results.append(f"{d['host']}{d['path']}: {mark} ({bytes_} байт, нужно {d['min_bytes']}+)")
    prefix = "живой трафик работает" if all_ok else "!!! ЖИВОЙ ТРАФИК НЕ РАБОТАЕТ"
    return f"{profile}: {prefix} -- " + "; ".join(results)


def _zapret2_status() -> str | None:
    """ActiveEnterTimestamp/SubState -- после restart нужно ПОДТВЕРДИТЬ,
    что он реально только что произошёл (см. promote.py "circular_locked
    держит max strategy= в памяти процесса без TTL" -- живой инцидент
    2026-08-07, restart, который тихо не сработал, оставляет старую
    стратегию залоченной в памяти без единой ошибки в логе)."""
    try:
        out = subprocess.run(
            ["sudo", "-n", "systemctl", "show", "zapret2", "--property=ActiveEnterTimestamp", "--property=SubState"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else None
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
def knowledge_page(
    request: Request,
    user: str = Depends(auth.require_login),
    profile: str = "",
    min_avg: float = 0.0,
    min_providers: int = 1,
):
    conn = db.connect()
    try:
        rollup = db.knowledge_family_rollup(conn)
    finally:
        conn.close()
    # Фильтруем в Python, не в SQL -- таблица небольшая (десятки строк,
    # не тысячи), а запрос и так уже агрегирует всё разом; отдельный SQL
    # с динамическим WHERE/HAVING ради этого не стоит усложнения.
    # min_avg по умолчанию 0.0 -- показывает всё, включая полностью
    # провальные паттерны (avg_score=0.0): это НЕ мусор, а полезный
    # антирейтинг -- показывает, что уже опробовано и не работает, чтобы
    # не гонять то же самое вручную повторно (алгоритм подбора это уже
    # учитывает через pulls/total_reward в UCB, см. main.py::pick_operator_ucb
    # и pick_parent_ucb в zenith/orchestrator) -- фильтры тут только чтобы
    # человеку было проще увидеть рабочие паттерны, не подменяют собой
    # то, что уже реально хранится в БД.
    filtered = [
        r for r in rollup
        if (not profile or r["profile"] == profile)
        and (r["avg_score"] or 0) >= min_avg
        and r["distinct_providers"] >= min_providers
    ]
    return templates.TemplateResponse(
        request, "knowledge.html",
        {
            "user": user, "rollup": filtered,
            "profiles": runner.RUNNABLE_PROFILES,
            "profile": profile, "min_avg": min_avg, "min_providers": min_providers,
            "total_count": len(rollup), "filtered_count": len(filtered),
        },
    )


def _controls_context(
    user: str, run_error: str | None = None, daemon_error: str | None = None,
    strategy_error: str | None = None, strategy_ok: str | None = None,
    autorun_error: str | None = None, promoter_error: str | None = None,
    autoupdate_error: str | None = None, autoupdate_ok: str | None = None,
) -> dict:
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
        "run_profiles": runner.RUNNABLE_PROFILES,
        "run_error": run_error,
        "daemon_error": daemon_error,
        "strategy_error": strategy_error,
        "strategy_ok": strategy_ok,
        "strategy_profiles": list(PROFILE_NUMBERS.keys()),
        "zapret2_status": _zapret2_status(),
        "autorun_error": autorun_error,
        "promoter_error": promoter_error,
        "autoupdate_projects": autoupdate_ctl.project_statuses(),
        "autoupdate_timer_status": autoupdate_ctl.timer_status(),
        "autoupdate_error": autoupdate_error,
        "autoupdate_ok": autoupdate_ok,
    }


@app.get("/controls")
def controls_page(request: Request, user: str = Depends(auth.require_login)):
    return templates.TemplateResponse(request, "controls.html", _controls_context(user))


@app.post("/controls/strategy/set")
def controls_strategy_set(
    request: Request, user: str = Depends(auth.require_login),
    profile: str = Form(...), strategy: int = Form(...),
):
    """Ручное переключение стратегии -- то же самое, что z0r пункт 21 или
    голый set_strategy_cli.sh set, просто кнопкой (см. main.py докстринг
    "Границы ответственности панели" -- осознанно изменено 2026-08-15).
    НЕ горячее применение само по себе -- ниже отдельная кнопка restart
    zapret2, её нужно нажать следом (та же оговорка, что в выводе
    promote.py)."""
    error = None
    if profile not in PROFILE_NUMBERS:
        error = "Неизвестный профиль."
    elif strategy < 1:
        error = "Номер стратегии должен быть положительным."
    else:
        # Верхняя граница -- раньше её не было вообще, только "strategy >= 1",
        # так что опечатка (5 вместо 500, копипаста не того числа) писалась
        # в locked.tsv без единой проверки -- ровно тот numbering gap, от
        # которого явно предостерегает CLAUDE.md z2r_autobench ("Never
        # introduce a numbering gap... every tool silently wastes cycles on
        # the nonexistent numbers"). Найдено при аудите перед деплоем на
        # Provider B 2026-08-17. Сверяем с РЕАЛЬНЫМ текущим max для профиля --
        # если геном только что добавили руками в конфиг, max уже это
        # видит (config_profile_max_strategy читает файл заново каждый раз),
        # так что легитимные случаи это не блокирует.
        max_out = _run_cli("max", str(PROFILE_NUMBERS[profile]))
        if max_out is not None and max_out.isdigit() and strategy > int(max_out):
            error = (
                f"strategy={strategy} больше текущего max={max_out} для этого профиля -- "
                "похоже на опечатку. Переключение на номер, которого ещё нет в конфиге, "
                "оставит профиль без рабочей стратегии. Если это НОВАЯ стратегия -- "
                "сперва добавь её в /opt/zapret2/config (эта кнопка только переключает "
                "уже существующие)."
            )
    ok = None
    if not error:
        num = PROFILE_NUMBERS[profile]
        proto = PROFILE_PROTO.get(profile, "tls")
        result = _run_cli("set", str(num), proto, str(strategy))
        if result is None:
            error = f"set_strategy_cli.sh set {num} {proto} {strategy} завершился с ошибкой."
        else:
            ok = f"{profile}: strategy={strategy} записана в locked.tsv. Не забудь restart zapret2 ниже — set сам по себе не горячее применение."
    return templates.TemplateResponse(
        request, "controls.html", _controls_context(user, strategy_error=error, strategy_ok=ok),
    )


@app.post("/controls/zapret2/restart")
def controls_zapret2_restart(request: Request, user: str = Depends(auth.require_login)):
    try:
        out = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", "zapret2"],
            capture_output=True, text=True, timeout=15,
        )
        error = None if out.returncode == 0 else (out.stderr.strip() or "systemctl restart zapret2 завершился с ошибкой.")
    except Exception as e:
        error = str(e)
    ok = None if error else "zapret2 перезапущен — проверь ActiveEnterTimestamp ниже, должен быть только что."
    return templates.TemplateResponse(
        request, "controls.html", _controls_context(user, strategy_error=error, strategy_ok=ok),
    )


@app.post("/controls/strategy/verify")
def controls_strategy_verify(
    request: Request, user: str = Depends(auth.require_login),
    profile: str = Form(...),
):
    """Живая проверка ПОСЛЕ set+restart -- см. _real_traffic_check()
    докстринг. Отдельная кнопка, не автоматическая -- за рулём человек,
    он сам решает, что делать с результатом (в отличие от
    zenith-promoter, который откатывает сам)."""
    if profile not in PROFILE_NUMBERS:
        result = "Неизвестный профиль."
    else:
        result = _real_traffic_check(profile)
    return templates.TemplateResponse(
        request, "controls.html", _controls_context(user, strategy_ok=result),
    )


@app.post("/controls/run")
def controls_run(
    request: Request, user: str = Depends(auth.require_login),
    profile: str = Form(...), rounds: int = Form(20), domain: str = Form(""),
):
    if profile not in runner.RUNNABLE_PROFILES:
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
    error = daemon_ctl.autotune_daemon.start()
    return templates.TemplateResponse(request, "controls.html", _controls_context(user, daemon_error=error))


@app.post("/controls/daemon/stop")
def controls_daemon_stop(request: Request, user: str = Depends(auth.require_login)):
    error = daemon_ctl.autotune_daemon.stop()
    return templates.TemplateResponse(request, "controls.html", _controls_context(user, daemon_error=error))


@app.get("/controls/daemon/status")
def controls_daemon_status(user: str = Depends(auth.require_login)):
    return {"active": daemon_ctl.autotune_daemon.is_active(), "log": daemon_ctl.autotune_daemon.log_tail()}


@app.post("/controls/zenith-autorun/start")
def controls_zenith_autorun_start(request: Request, user: str = Depends(auth.require_login)):
    error = daemon_ctl.zenith_autorun.start()
    return templates.TemplateResponse(request, "controls.html", _controls_context(user, autorun_error=error))


@app.post("/controls/zenith-autorun/stop")
def controls_zenith_autorun_stop(request: Request, user: str = Depends(auth.require_login)):
    error = daemon_ctl.zenith_autorun.stop()
    return templates.TemplateResponse(request, "controls.html", _controls_context(user, autorun_error=error))


@app.get("/controls/zenith-autorun/status")
def controls_zenith_autorun_status(user: str = Depends(auth.require_login)):
    return {"active": daemon_ctl.zenith_autorun.is_active(), "log": daemon_ctl.zenith_autorun.log_tail()}


@app.post("/controls/zenith-promoter/start")
def controls_zenith_promoter_start(request: Request, user: str = Depends(auth.require_login)):
    error = daemon_ctl.zenith_promoter.start()
    return templates.TemplateResponse(request, "controls.html", _controls_context(user, promoter_error=error))


@app.post("/controls/zenith-promoter/stop")
def controls_zenith_promoter_stop(request: Request, user: str = Depends(auth.require_login)):
    error = daemon_ctl.zenith_promoter.stop()
    return templates.TemplateResponse(request, "controls.html", _controls_context(user, promoter_error=error))


@app.get("/controls/zenith-promoter/status")
def controls_zenith_promoter_status(user: str = Depends(auth.require_login)):
    return {"active": daemon_ctl.zenith_promoter.is_active(), "log": daemon_ctl.zenith_promoter.log_tail()}


@app.post("/controls/autoupdate/toggle")
def controls_autoupdate_toggle(
    request: Request, user: str = Depends(auth.require_login),
    project: str = Form(...), enabled: str = Form(...),
):
    """Переключает автообновление для ОДНОГО проекта -- пишет тот же
    /etc/z2r_autobench/autoupdate.conf, что читает и z0r пункт 26 (см.
    autoupdate_ctl.py). Если таймер ещё не установлен на этом сервере,
    флаг всё равно пишется (пригодится, когда таймер поставят через
    z0r) -- страница явно это подскажет через autoupdate_timer_status."""
    error = autoupdate_ctl.set_project(project, enabled == "1")
    ok = None
    if not error:
        label = autoupdate_ctl.PROJECT_LABELS.get(project, project)
        ok = f"{label}: автообновление {'включено' if enabled == '1' else 'выключено'}."
    return templates.TemplateResponse(
        request, "controls.html", _controls_context(user, autoupdate_error=error, autoupdate_ok=ok),
    )


@app.post("/controls/autoupdate/run")
def controls_autoupdate_run(request: Request, user: str = Depends(auth.require_login)):
    error = autoupdate_ctl.run_now()
    ok = None if error else "Обновление запущено -- результат через несколько секунд, см. лог ниже (обнови страницу)."
    return templates.TemplateResponse(
        request, "controls.html", _controls_context(user, autoupdate_error=error, autoupdate_ok=ok),
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
