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
    человек делает через z0r пункт 12, просто кнопкой вместо SSH,
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
from urllib.parse import quote

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
import funnel_runner
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


def _run_rkn_cli(*args) -> tuple[str | None, str | None]:
    """(stdout, error) -- в отличие от _run_cli() выше, тут нужен текст
    ошибки для показа человеку (см. rkn_list_cli.sh -- диагностика вроде
    "уже в списке" или sudoers-отказ идёт в stderr), не просто None при
    любой неудаче."""
    try:
        out = subprocess.run(
            ["sudo", "-n", "bash", config.RKN_LIST_CLI, *args],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        return None, str(e)
    if out.returncode != 0:
        return None, (out.stderr or "").strip() or f"rkn_list_cli.sh завершился с кодом {out.returncode}"
    return out.stdout, None


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
    # Все 9 профилей, в каноническом порядке (PROFILE_NUMBERS, тот же,
    # что и везде в панели/z0r) -- не только те, для которых уже есть
    # данные. Раньше секции шли в алфавитном порядке SQL-запроса
    # (ORDER BY g.profile) и профили без данных вообще не показывались --
    # непонятно было, добавлен ли профиль в Zenith или просто ещё не
    # успел накопить прогоны. runner.RUNNABLE_PROFILES отличает эти два
    # случая в шаблоне: профиль без genome-теста в Zenith вообще
    # (YT_QUIC_UDP/GAMES_UDP/FB_TLS/FB_HTTP) против уже поддерживаемого,
    # но пока без накопленных прогонов.
    by_profile = {p: [] for p in PROFILE_NUMBERS}
    for row in rows:
        by_profile.setdefault(row["profile"], []).append(row)

    # Живая проверка "продвинутая метка ещё актуальна?" -- ТОЛЬКО для
    # локальной ноды этого хоста (set_strategy_cli.sh читает ИМЕННО этот
    # сервер, сравнивать с ним promoted_strategy удалённой ноды бессмысленно
    # -- у неё свой собственный боевой конфиг). Живой случай 2026-08-26:
    # геном был продвинут как strategy=44, автопромоутер с тех пор продвинул
    # ЕЩЁ 10+ раз подряд (48,50,52...64), но старый геном как был "лучшим
    # по avg_score", так и остался показываться с меткой "strategy=44" --
    # promoted_strategy ставится ТОЛЬКО на вновь продвигаемый геном,
    # никогда не снимается со старого при следующем продвижении, так что
    # метка тихо устаревает и вводит в заблуждение, будто именно ЭТОТ
    # геном сейчас боевой.
    live_locked = {}
    for profile, num in PROFILE_NUMBERS.items():
        proto = PROFILE_PROTO.get(profile, "tls")
        live_locked[profile] = _run_cli("get", str(num), proto)

    return templates.TemplateResponse(
        request, "overview.html",
        {
            "user": user, "by_profile": by_profile,
            "runnable_profiles": set(runner.RUNNABLE_PROFILES),
            "local_env_name": config.LOCAL_ENVIRONMENT_NAME,
            "live_locked": live_locked,
        },
    )


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


@app.get("/rkn")
def rkn_page(
    request: Request, user: str = Depends(auth.require_login),
    production_error: str = "",
):
    # Боевой список (TCP_RKN_list.txt/TCP_Custom.txt на самом сервере,
    # НЕ БД) -- см. rkn_list_cli.sh докстринг. Читаем его при КАЖДОМ
    # заходе на страницу, БЕЗУСЛОВНО -- даже если production_error уже
    # пришёл через query от неудачного add/remove (см. эти роуты выше).
    # Раньше это было "if not production_error: читать список" -- баг:
    # после любой ошибки формы таблица молча показывала пустой список
    # вместо реального (в TCP_RKN_list.txt может быть тысяча строк),
    # выглядело так, будто весь список пропал. Ошибка чтения списка
    # (genuinely не удалось выполнить list) важнее ошибки формы --
    # перезаписывает её, а не наоборот, иначе человек увидит "Пустой
    # домен" и пустую таблицу без объяснения, почему она пустая.
    #
    # ТЕСТОВЫЙ список (domain_pool) для RKN_TLS сюда больше НЕ подмешан --
    # переехал на универсальную /domains?profile=RKN_TLS (2026-08-31, по
    # прямому запросу: "у каждого профиля свой тестер", домены YT_TLS
    # НЕ должны попадать в РКН-механику и наоборот -- эта страница
    # держит только то, что реально относится к RKN_TLS-хостлисту).
    production_list = []
    out, list_err = _run_rkn_cli("list")
    if list_err:
        production_error = list_err
    else:
        for line in (out or "").splitlines():
            if not line.strip():
                continue
            source, _, host = line.partition("\t")
            production_list.append({"source": source, "host": host})

    return templates.TemplateResponse(
        request, "rkn.html",
        {
            "user": user,
            "production_list": production_list, "production_error": production_error,
        },
    )


# Профили, для которых имеет смысл тестовый domain_pool -- curl-проба
# по host/path (см. tester.probe в Zenith), поэтому только TCP TLS/HTTP,
# не UDP-профили (VOICE_UDP/GAMES_UDP/YT_QUIC_UDP тестируются иначе, см.
# genome.PROFILE_FILTER_TYPE в Zenith).
DOMAIN_LIST_PROFILES = ["YT_TLS", "GV_TLS", "RKN_TLS", "DS_TLS", "FB_TLS", "FB_HTTP"]


def _run_domain_sync_cli(*args) -> tuple[str | None, str | None, int]:
    try:
        out = subprocess.run(
            ["sudo", "-n", "bash", config.DOMAIN_LIST_SYNC_CLI, *args],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        return None, str(e), 1
    return out.stdout, out.stderr, out.returncode


@app.get("/domains")
def domains_page(
    request: Request, user: str = Depends(auth.require_login),
    profile: str = "YT_TLS", error: str = "", ok: str = "",
):
    """Каждый профиль -- свой независимый тестовый список доменов (по
    прямому запросу: "мы же с тобой договорились давно уже что для
    каждого профиля свой тестер... YouTube весьма специфичный, мы не
    будем его вносить в РКН список"). Один шаблон/роут на ЛЮБОЙ профиль
    из DOMAIN_LIST_PROFILES, а не отдельная страница на каждый -- та же
    db.list_domains_for_profile()/get_or_create_domain()/delete_domain(),
    что раньше жили только на /rkn (см. db.py::list_domains_for_profile
    докстринг -- она уже была написана профиль-параметризованной именно
    "для любого будущего списка доменов профиля в панели")."""
    if profile not in DOMAIN_LIST_PROFILES:
        profile = "YT_TLS"
    conn = db.connect()
    try:
        domains = db.list_domains_for_profile(conn, profile)
    finally:
        conn.close()

    # Список профилей, для которых есть готовый официальный курированный
    # список на диске (см. domain_list_sync.sh -- источник правды ТАМ,
    # не дублируем маппинг профиль->файл здесь второй раз) -- определяет,
    # показывать ли кнопку "Синхронизировать" вместо/вместе с ручным вводом.
    sync_out, _, sync_rc = _run_domain_sync_cli("--list-profiles")
    sync_profiles = {p.strip() for p in (sync_out or "").splitlines() if p.strip()} if sync_rc == 0 else set()

    return templates.TemplateResponse(
        request, "domains.html",
        {
            "user": user, "profile": profile, "profiles": DOMAIN_LIST_PROFILES,
            "domains": domains, "error": error, "ok": ok,
            "sync_available": profile in sync_profiles,
        },
    )


@app.post("/domains/sync")
def domains_sync(
    request: Request, user: str = Depends(auth.require_login),
    profile: str = Form(...), min_bytes: int = Form(65536),
):
    """Читает официальный курированный список (напр.
    /opt/zator/lists/russia-youtube.txt для YT_TLS) через
    domain_list_sync.sh и заводит недостающие домены в domain_pool через
    тот же get_or_create_domain, что и ручное/bulk добавление -- идемпотентно,
    уже существующие строки не дублирует и не трогает (get_or_create_domain
    сама так устроена, см. её вызовы выше)."""
    if profile not in DOMAIN_LIST_PROFILES:
        return RedirectResponse(url="/domains?error=Неизвестный+профиль", status_code=303)
    out, err, rc = _run_domain_sync_cli(profile)
    if rc != 0:
        msg = (err or "").strip() or f"domain_list_sync.sh завершился с кодом {rc}"
        return RedirectResponse(url=f"/domains?profile={profile}&error={quote(msg)}", status_code=303)
    count = 0
    conn = db.connect()
    try:
        for line in (out or "").splitlines():
            line = line.strip()
            if not line:
                continue
            if _add_one_domain(conn, profile, line, min_bytes):
                count += 1
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/domains?profile={profile}&ok=Синхронизировано+{count}", status_code=303)


def _add_one_domain(conn, profile: str, raw: str, min_bytes: int) -> bool:
    """Тот же разбор host[/path], что runner.py передаёт в main.py
    --domain -- см. Zenith/orchestrator/main.py::run(). Пустой host
    (только "/path" или пустая строка) ловим явно -- иначе
    get_or_create_domain тихо создаст мусорную строку с host=''. False,
    если строка пуста после разбора (вызывающий сам решает, считать это
    ошибкой или просто пропустить, см. bulk-add)."""
    host, _, path = raw.strip().partition("/")
    if not host:
        return False
    db.get_or_create_domain(conn, host, "/" + path if path else "/", profile, max(1, min_bytes))
    return True


@app.post("/domains/add")
def domains_add(
    request: Request, user: str = Depends(auth.require_login),
    profile: str = Form(...), domain: str = Form(...), min_bytes: int = Form(65536),
):
    if profile not in DOMAIN_LIST_PROFILES:
        return RedirectResponse(url="/domains?error=Неизвестный+профиль", status_code=303)
    conn = db.connect()
    try:
        added = _add_one_domain(conn, profile, domain, min_bytes)
        if added:
            conn.commit()
    finally:
        conn.close()
    if not added:
        return RedirectResponse(url=f"/domains?profile={profile}&error=Пустой+домен", status_code=303)
    return RedirectResponse(url=f"/domains?profile={profile}", status_code=303)


@app.post("/domains/bulk-add")
def domains_bulk_add(
    request: Request, user: str = Depends(auth.require_login),
    profile: str = Form(...), domains: str = Form(...), min_bytes: int = Form(65536),
):
    """Один домен на строку -- для разового заброса готового курированного
    списка (напр. russia-youtube.txt для YT_TLS) вместо добавления по
    одному через /domains/add. Пустые строки и строки, начинающиеся с #,
    молча пропускаются (даёт вставить список как есть, с комментариями)."""
    if profile not in DOMAIN_LIST_PROFILES:
        return RedirectResponse(url="/domains?error=Неизвестный+профиль", status_code=303)
    count = 0
    conn = db.connect()
    try:
        for line in domains.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if _add_one_domain(conn, profile, line, min_bytes):
                count += 1
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/domains?profile={profile}&ok=Добавлено+{count}", status_code=303)


@app.post("/domains/{domain_id}/delete")
def domains_delete(
    request: Request, domain_id: int, user: str = Depends(auth.require_login),
    profile: str = Form(...),
):
    if profile not in DOMAIN_LIST_PROFILES:
        profile = "YT_TLS"
    conn = db.connect()
    try:
        db.delete_domain(conn, domain_id)
    finally:
        conn.close()
    return RedirectResponse(url=f"/domains?profile={profile}", status_code=303)


@app.post("/rkn/add-production")
def rkn_add_production(
    request: Request, user: str = Depends(auth.require_login),
    domain: str = Form(...),
):
    # Пишет в TCP_Custom.txt на самом сервере (см. rkn_list_cli.sh) --
    # НЕ в domain_pool (та форма выше, /rkn/add, отдельная и намеренно
    # независимая). Это боевой хостлист, по нему nfqws2 реально решает,
    # чей трафик идёт через RKN_TLS -- поэтому текст ошибки (sudoers-
    # отказ, пустой домен и т.п.) показываем как есть, не глотаем.
    domain = domain.strip()
    if not domain:
        return RedirectResponse(url="/rkn?production_error=Пустой+домен", status_code=303)
    _, err = _run_rkn_cli("add", domain)
    if err:
        return RedirectResponse(url=f"/rkn?production_error={quote(err)}", status_code=303)
    return RedirectResponse(url="/rkn", status_code=303)


@app.post("/rkn/remove-production")
def rkn_remove_production(
    request: Request, user: str = Depends(auth.require_login),
    domain: str = Form(...),
):
    # ТОЛЬКО TCP_Custom.txt -- rkn_list_cli.sh remove само отказывает,
    # если домен только в официальном TCP_RKN_list.txt (см. его же
    # докстринг), страница со своей стороны вообще не рисует кнопку
    # удаления для строк с source="официальный" (см. rkn.html), это
    # просто вторая линия защиты на случай прямого POST мимо формы.
    domain = domain.strip()
    if not domain:
        return RedirectResponse(url="/rkn?production_error=Пустой+домен", status_code=303)
    _, err = _run_rkn_cli("remove", domain)
    if err:
        return RedirectResponse(url=f"/rkn?production_error={quote(err)}", status_code=303)
    return RedirectResponse(url="/rkn", status_code=303)


def _run_custom_domain_cli(*args) -> tuple[str | None, str | None, int]:
    """(stdout, stderr, returncode) -- custom_domain_cli.sh печатает
    ТАБЛИЦУ (list) в stdout, но ВСЮ диагностику add/remove (включая само
    превью нового блока конфига) в stderr (см. его же докстринг) --
    возвращаем оба потока отдельно и код возврата, вызывающий сам решает,
    что показать. rc не сворачиваем в None-при-ошибке, как в _run_cli --
    add без --yes нарочно возвращает 0 для успешного превью (ничего не
    записано) и 1 для отказа (уже управляется другим профилем и т.п.),
    разница видна вызывающему только по rc, не по exception."""
    try:
        out = subprocess.run(
            ["sudo", "-n", "bash", config.CUSTOM_DOMAIN_CLI, *args],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        return None, str(e), 1
    return out.stdout, out.stderr, out.returncode


def _custom_domains_context(user: str, message: str | None = None, message_is_error: bool = False, preview_domain: str | None = None) -> dict:
    out, err, rc = _run_custom_domain_cli("list")
    domains = []
    list_error = None
    if rc == 0:
        lines = (out or "").splitlines()
        if lines and not lines[0].startswith("(пусто"):
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 4:
                    domains.append({"domain": parts[0], "profile": parts[1], "strategy": parts[2], "created": parts[3]})
    else:
        list_error = (err or "").strip() or f"custom_domain_cli.sh list завершился с кодом {rc}"
    return {
        "user": user, "domains": domains, "list_error": list_error,
        "message": message, "message_is_error": message_is_error,
        "preview_domain": preview_domain,
    }


@app.get("/custom-domains")
def custom_domains_page(request: Request, user: str = Depends(auth.require_login)):
    return templates.TemplateResponse(request, "custom_domains.html", _custom_domains_context(user))


@app.post("/custom-domains/preview")
def custom_domains_preview(
    request: Request, user: str = Depends(auth.require_login),
    domain: str = Form(...),
):
    """add БЕЗ --yes -- custom_domain_cli.sh либо печатает превью
    будущего блока (rc=0, ничего не записано), либо отказывает с
    предупреждением, если домен уже управляется другим профилем или уже
    зарегистрирован (rc=1) -- в обоих случаях просто показываем stderr,
    подтверждающая форма (POST /custom-domains/add) рисуется в шаблоне
    только когда preview_domain задан (т.е. rc=0)."""
    out, err, rc = _run_custom_domain_cli("add", domain)
    text = (err or "").strip() or (out or "").strip()
    return templates.TemplateResponse(
        request, "custom_domains.html",
        _custom_domains_context(user, message=text, message_is_error=(rc != 0), preview_domain=domain if rc == 0 else None),
    )


@app.post("/custom-domains/add")
def custom_domains_add(
    request: Request, user: str = Depends(auth.require_login),
    domain: str = Form(...),
):
    """Реальная запись -- add --yes дописывает новый блок в
    /opt/zapret2/config (backup перед записью, см. custom_domain_cli.sh)
    и регистрирует домен. Restart zapret2 после этого -- РУЧНОЙ шаг (см.
    напоминание в самом тексте ответа скрипта), панель его не делает."""
    out, err, rc = _run_custom_domain_cli("add", domain, "--yes")
    text = (err or "").strip() or (out or "").strip()
    return templates.TemplateResponse(
        request, "custom_domains.html",
        _custom_domains_context(user, message=text, message_is_error=(rc != 0)),
    )


@app.post("/custom-domains/remove")
def custom_domains_remove(
    request: Request, user: str = Depends(auth.require_login),
    domain: str = Form(...),
):
    """НЕ удаляет блок из конфига (см. custom_domain_cli.sh докстринг --
    тот же класс риска, что и создание блока, только опаснее делать это
    на живом файле, который уже читает работающий nfqws2) -- только
    опустошает домен-специфичный hostlist, блок остаётся мёртвым и
    безвредным."""
    out, err, rc = _run_custom_domain_cli("remove", domain)
    text = (err or "").strip() or (out or "").strip()
    return templates.TemplateResponse(
        request, "custom_domains.html",
        _custom_domains_context(user, message=text, message_is_error=(rc != 0)),
    )


def _controls_context(
    user: str, run_error: str | None = None, daemon_error: str | None = None,
    strategy_error: str | None = None, strategy_ok: str | None = None,
    autorun_error: str | None = None, promoter_error: str | None = None,
    autoupdate_error: str | None = None, autoupdate_ok: str | None = None,
    funnel_error: str | None = None,
    daemon_update_status: str | None = None, autorun_update_status: str | None = None,
    promoter_update_status: str | None = None,
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
        "funnel_error": funnel_error,
        "daemon_update_status": daemon_update_status,
        "autorun_update_status": autorun_update_status,
        "promoter_update_status": promoter_update_status,
    }


@app.get("/controls")
def controls_page(request: Request, user: str = Depends(auth.require_login)):
    return templates.TemplateResponse(request, "controls.html", _controls_context(user))


@app.get("/controls/automation")
def automation_page(request: Request, user: str = Depends(auth.require_login)):
    # Раньше жило на одной странице с /controls (7+ карточек подряд --
    # неудобно листать). Тот же _controls_context() (вся логика/данные
    # без изменений), просто другой шаблон, показывающий только
    # автономные systemd-юниты (autorun/promoter/daemon/autoupdate), не
    # ручные "Стратегии"-действия выше по смыслу.
    return templates.TemplateResponse(request, "automation.html", _controls_context(user))


@app.post("/controls/strategy/set")
def controls_strategy_set(
    request: Request, user: str = Depends(auth.require_login),
    profile: str = Form(...), strategy: int = Form(...),
):
    """Ручное переключение стратегии -- то же самое, что z0r пункт 12 или
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


@app.post("/controls/funnel")
def controls_funnel(
    request: Request, user: str = Depends(auth.require_login),
    domain: str = Form(...), passes: int = Form(3),
    settle: int = Form(0), attempts: int = Form(0),
):
    """"Воронка" для конкретного домена -- ОТДЕЛЬНЫЙ инструмент от кнопки
    "запустить подбор" выше (Zenith-геномы в песочнице): напрямую крутит
    БОЕВУЮ стратегию профиля, определённого по домену, через реальный
    трафик (см. funnel_runner.py и rank_strategies.sh --domain
    докстринги). passes=3 по умолчанию -- то же значение, что у самого
    rank_strategies.sh; settle/attempts=0 значит "не передавать флаг,
    взять дефолт скрипта" (SETTLE_SECONDS/ATTEMPTS_PER_STRATEGY env)."""
    error = funnel_runner.start(domain, max(1, min(passes, 10)), settle or None, attempts or None)
    return templates.TemplateResponse(request, "controls.html", _controls_context(user, funnel_error=error))


@app.post("/controls/funnel/stop")
def controls_funnel_stop(request: Request, user: str = Depends(auth.require_login)):
    error = funnel_runner.stop()
    return templates.TemplateResponse(request, "controls.html", _controls_context(user, funnel_error=error))


@app.get("/controls/funnel/status")
def controls_funnel_status(user: str = Depends(auth.require_login)):
    return funnel_runner.status()


@app.post("/controls/daemon/start")
def controls_daemon_start(request: Request, user: str = Depends(auth.require_login)):
    error = daemon_ctl.autotune_daemon.start()
    return templates.TemplateResponse(request, "automation.html", _controls_context(user, daemon_error=error))


@app.post("/controls/daemon/stop")
def controls_daemon_stop(request: Request, user: str = Depends(auth.require_login)):
    error = daemon_ctl.autotune_daemon.stop()
    return templates.TemplateResponse(request, "automation.html", _controls_context(user, daemon_error=error))


@app.post("/controls/daemon/restart")
def controls_daemon_restart(request: Request, user: str = Depends(auth.require_login)):
    error = daemon_ctl.autotune_daemon.restart()
    return templates.TemplateResponse(request, "automation.html", _controls_context(user, daemon_error=error))


@app.get("/controls/daemon/status")
def controls_daemon_status(user: str = Depends(auth.require_login)):
    return {"active": daemon_ctl.autotune_daemon.is_active(), "log": daemon_ctl.autotune_daemon.log_tail()}


@app.post("/controls/daemon/check-updates")
def controls_daemon_check_updates(request: Request, user: str = Depends(auth.require_login)):
    status = daemon_ctl.check_git_updates(config.Z2R_AUTOBENCH_DIR)
    return templates.TemplateResponse(request, "automation.html", _controls_context(user, daemon_update_status=status))


@app.post("/controls/zenith-autorun/start")
def controls_zenith_autorun_start(request: Request, user: str = Depends(auth.require_login)):
    error = daemon_ctl.zenith_autorun.start()
    return templates.TemplateResponse(request, "automation.html", _controls_context(user, autorun_error=error))


@app.post("/controls/zenith-autorun/stop")
def controls_zenith_autorun_stop(request: Request, user: str = Depends(auth.require_login)):
    error = daemon_ctl.zenith_autorun.stop()
    return templates.TemplateResponse(request, "automation.html", _controls_context(user, autorun_error=error))


@app.post("/controls/zenith-autorun/restart")
def controls_zenith_autorun_restart(request: Request, user: str = Depends(auth.require_login)):
    error = daemon_ctl.zenith_autorun.restart()
    return templates.TemplateResponse(request, "automation.html", _controls_context(user, autorun_error=error))


@app.get("/controls/zenith-autorun/status")
def controls_zenith_autorun_status(user: str = Depends(auth.require_login)):
    return {"active": daemon_ctl.zenith_autorun.is_active(), "log": daemon_ctl.zenith_autorun.log_tail()}


@app.post("/controls/zenith-autorun/check-updates")
def controls_zenith_autorun_check_updates(request: Request, user: str = Depends(auth.require_login)):
    status = daemon_ctl.check_git_updates(config.ZENITH_DIR)
    return templates.TemplateResponse(request, "automation.html", _controls_context(user, autorun_update_status=status))


@app.post("/controls/zenith-promoter/start")
def controls_zenith_promoter_start(request: Request, user: str = Depends(auth.require_login)):
    error = daemon_ctl.zenith_promoter.start()
    return templates.TemplateResponse(request, "automation.html", _controls_context(user, promoter_error=error))


@app.post("/controls/zenith-promoter/stop")
def controls_zenith_promoter_stop(request: Request, user: str = Depends(auth.require_login)):
    error = daemon_ctl.zenith_promoter.stop()
    return templates.TemplateResponse(request, "automation.html", _controls_context(user, promoter_error=error))


@app.post("/controls/zenith-promoter/restart")
def controls_zenith_promoter_restart(request: Request, user: str = Depends(auth.require_login)):
    error = daemon_ctl.zenith_promoter.restart()
    return templates.TemplateResponse(request, "automation.html", _controls_context(user, promoter_error=error))


@app.get("/controls/zenith-promoter/status")
def controls_zenith_promoter_status(user: str = Depends(auth.require_login)):
    return {"active": daemon_ctl.zenith_promoter.is_active(), "log": daemon_ctl.zenith_promoter.log_tail()}


@app.post("/controls/zenith-promoter/check-updates")
def controls_zenith_promoter_check_updates(request: Request, user: str = Depends(auth.require_login)):
    status = daemon_ctl.check_git_updates(config.ZENITH_DIR)
    return templates.TemplateResponse(request, "automation.html", _controls_context(user, promoter_update_status=status))


@app.post("/controls/autoupdate/toggle")
def controls_autoupdate_toggle(
    request: Request, user: str = Depends(auth.require_login),
    project: str = Form(...), enabled: str = Form(...),
):
    """Переключает автообновление для ОДНОГО проекта -- пишет тот же
    /etc/z2r_autobench/autoupdate.conf, что читает и z0r пункт 25 (см.
    autoupdate_ctl.py). Если таймер ещё не установлен на этом сервере,
    флаг всё равно пишется (пригодится, когда таймер поставят через
    z0r) -- страница явно это подскажет через autoupdate_timer_status."""
    error = autoupdate_ctl.set_project(project, enabled == "1")
    ok = None
    if not error:
        label = autoupdate_ctl.PROJECT_LABELS.get(project, project)
        ok = f"{label}: автообновление {'включено' if enabled == '1' else 'выключено'}."
    return templates.TemplateResponse(
        request, "automation.html", _controls_context(user, autoupdate_error=error, autoupdate_ok=ok),
    )


@app.post("/controls/autoupdate/run")
def controls_autoupdate_run(request: Request, user: str = Depends(auth.require_login)):
    error = autoupdate_ctl.run_now()
    ok = None if error else "Обновление запущено -- результат через несколько секунд, см. лог ниже (обнови страницу)."
    return templates.TemplateResponse(
        request, "automation.html", _controls_context(user, autoupdate_error=error, autoupdate_ok=ok),
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
