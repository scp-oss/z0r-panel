"""Автопродвижение Zenith-геномов в /opt/zapret2/config БЕЗ участия
человека -- по прямому запросу "цель: автономная работа без
человеческого вмешательства". Порог тот же, что у Zenith'овского
orchestrator/promote.py::pick_best() -- 100% успехов, минимум
ZENITH_PROMOTER_MIN_PULLS прогонов, лучший avg_score (см.
db.pick_promotable_genome). Просто автоматизирует шаг, который человек и
так делал бы руками по тем же правилам, не придумывает новый критерий.

Раз в POLL_SECONDS проверяет, не пора ли сработать (интервал -- тот же
ZENITH_AUTORUN_INTERVAL_MINUTES, что и генератор -- нет смысла продвигать
чаще, чем генератор в принципе может найти что-то новое), по одному
профилю за раз (из runner.RUNNABLE_PROFILES, ровно тех 4, для которых
есть проверенный PROFILE_HEADERS ниже). На каждое срабатывание:

  1. Спрашивает БД лучшего непродвинутого кандидата.
  2. Нет такого -- пропускает, следующая проверка через интервал.
  3. Иначе: читает текущий max strategy= (set_strategy_cli.sh max),
     зовёт promote_apply_cli.sh apply -- добавляет НОВЫЙ strategy=N блок
     в конец существующего блока профиля (backup делает сам скрипт, см.
     его докстринг), переключает на него (set_strategy_cli.sh set),
     restart zapret2, проверяет is-active (через уже гранченный
     `systemctl show --property=SubState`) и что get вернул именно N.
  4. Любая из проверок в п.3 не прошла -- promote_apply_cli.sh restore
     (откат конфига из backup) + set обратно на старый номер + restart,
     кандидат остаётся непродвинутым для следующей попытки. Никогда не
     оставляет прод в непроверенном состоянии без отката.
  5. Успех -- db.set_promoted_strategy(...), геном больше не кандидат на
     повторное продвижение.

Включено/выключено -- отдельный файл-флаг (STATE_FILE), тот же паттерн,
что autorun.py -- переживает рестарт панели. ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНО --
самая рискованная автоматика в проекте (пишет боевой конфиг и
рестартует zapret2 без человека), включать явно с панели после того, как
оператор убедился, что promote_apply_cli.sh корректно матчит реальный
/opt/zapret2/config на конкретном сервере (см. README "Автопродвижение"
-- рекомендация прогнать вручную один раз перед тем, как включать
автоматику)."""
import json
import os
import subprocess
import threading
import time

import config
import db
import runner

STATE_FILE = os.path.join(config.PANEL_DIR, "promoter_state.json")
LOG_FILE = os.path.join(config.PANEL_DIR, "run_logs", "promoter.log")
POLL_SECONDS = 60
SETTLE_SECONDS = 3  # пауза после restart перед health-check, тот же порядок, что BASE_SETTLE_SECONDS в Zenith main.py

# Дублирует Zenith/orchestrator/genome.py::PROFILE_FILTERS -- та же
# сознательная дупликация, что и PROFILE_NUMBERS в main.py (разные
# процессы/репозитории, общего python-модуля между ними нет). Меняешь
# PROFILE_FILTERS в Zenith -- поменяй и здесь, иначе
# promote_apply_cli.sh не найдёт блок в конфиге (откажет безопасно, не
# сломает файл -- см. её же докстринг про строгое совпадение HEADER).
PROFILE_HEADERS = {
    "YT_TLS": [
        "--filter-tcp=443 --filter-l7=tls",
        "--hostlist=/opt/zapret2/extra_strats/TCP_YT_list.txt",
        "--hostlist-exclude=/opt/zapret2/lists/netrogat.txt",
        "--payload=tls_client_hello,http_req,http_reply,unknown,tls_server_hello",
    ],
    "RKN_TLS": [
        "--filter-tcp=80,443,2053,2083,2087,2096,8443 --filter-l7=tls",
        "--hostlist=/opt/zapret2/extra_strats/TCP_RKN_list.txt",
        "--hostlist=/opt/zapret2/extra_strats/TCP_Custom.txt",
        "--hostlist-exclude=/opt/zapret2/extra_strats/TCP_Discord.txt",
        "--hostlist-exclude=/opt/zapret2/lists/netrogat.txt",
        "--payload=tls_client_hello,http_req,http_reply,unknown,tls_server_hello",
    ],
    "DS_TLS": [
        "--filter-tcp=80,443,2053,2083,2087,2096,8443",
        "--hostlist=/opt/zapret2/extra_strats/TCP_Discord.txt",
        "--hostlist-exclude=/opt/zapret2/lists/netrogat.txt",
        "--payload=tls_client_hello,http_req,http_reply,unknown,tls_server_hello",
    ],
    "VOICE_UDP": [
        "--filter-udp=443,2053,2083,2087,2096,8443,50000-50099,1400,3478-3481,5349,19294-19344",
        "--filter-l7=discord,stun",
        "--payload=discord_ip_discovery,stun",
    ],
}

# Совпадает с main.py::PROFILE_NUMBERS/PROFILE_PROTO для этого же
# подмножества -- дублируется по той же причине (разные процессы,
# общего модуля нет).
PROFILE_NUMBERS = {"YT_TLS": 1, "RKN_TLS": 3, "DS_TLS": 4, "VOICE_UDP": 6}
PROFILE_PROTO = {"VOICE_UDP": "udp"}

_rotate_idx = 0
_last_fired_at = 0.0
_last_result = ""  # человекочитаемая строка -- что произошло на последнем срабатывании, для /controls


def _load_enabled() -> bool:
    try:
        with open(STATE_FILE) as f:
            return bool(json.load(f).get("enabled", False))
    except (OSError, ValueError):
        return False


def is_enabled() -> bool:
    return _load_enabled()


def set_enabled(value: bool) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump({"enabled": value}, f)


def last_result() -> str:
    return _last_result


def _log(msg: str) -> None:
    global _last_result
    _last_result = msg
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _sudo(cmd: list, timeout: int = 20, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sudo", "-n", *cmd], capture_output=True, text=True, timeout=timeout, input=input_text,
    )


def _get_strategy(num: int, proto: str) -> str | None:
    out = _sudo(["bash", config.SET_STRATEGY_CLI, "get", str(num), proto])
    return out.stdout.strip() if out.returncode == 0 else None


def _max_strategy(num: int) -> str | None:
    out = _sudo(["bash", config.SET_STRATEGY_CLI, "max", str(num)])
    return out.stdout.strip() if out.returncode == 0 else None


def _set_strategy(num: int, proto: str, strategy: str) -> bool:
    return _sudo(["bash", config.SET_STRATEGY_CLI, "set", str(num), proto, strategy]).returncode == 0


def _restart_zapret2() -> bool:
    return _sudo(["systemctl", "restart", "zapret2"], timeout=30).returncode == 0


def _zapret2_running() -> bool:
    out = _sudo(["systemctl", "show", "zapret2", "--property=ActiveEnterTimestamp", "--property=SubState"])
    return out.returncode == 0 and "SubState=running" in out.stdout


def _try_promote_profile(profile: str) -> None:
    num = PROFILE_NUMBERS[profile]
    proto = PROFILE_PROTO.get(profile, "tls")
    header = PROFILE_HEADERS[profile]

    conn = db.connect()
    try:
        local_env_id = db.get_or_create_local_environment(conn)
        candidate = db.pick_promotable_genome(conn, profile, local_env_id, config.ZENITH_PROMOTER_MIN_PULLS)
        if not candidate:
            _log(f"{profile}: нет кандидата с {config.ZENITH_PROMOTER_MIN_PULLS}+ прогонами и 100% успехом -- пропуск.")
            return
        if candidate["promoted_strategy"] is not None:
            _log(f"{profile}: лучший кандидат {candidate['id'][:12]} уже продвинут как strategy={candidate['promoted_strategy']} -- пропуск.")
            return

        current_max = _max_strategy(num)
        if current_max is None or not current_max.isdigit():
            _log(f"{profile}: не удалось прочитать текущий max strategy= -- пропуск.")
            return
        old_locked = _get_strategy(num, proto)

        spec = "HEADER\n" + "\n".join(header) + "\nBODY\n" + "\n".join(candidate["rendered_args"].split("\n")) + "\n"
        apply_out = _sudo(
            ["bash", config.PROMOTE_APPLY_CLI, "apply", current_max, config.ZAPRET2_CONFIG_PATH, config.PROMOTE_BACKUP_DIR],
            input_text=spec,
        )
        if apply_out.returncode != 0:
            _log(f"{profile}: promote_apply_cli.sh apply отказал -- {apply_out.stderr.strip()}")
            return

        strategy_n = apply_out.stdout.strip()
        backup_line = next((l for l in apply_out.stderr.splitlines() if l.startswith("backup: ")), "")
        backup_path = backup_line[len("backup: "):].strip()

        if not _set_strategy(num, proto, strategy_n) or not _restart_zapret2():
            _log(f"{profile}: strategy={strategy_n} добавлена в конфиг, но set/restart не прошли -- откатываю.")
            _rollback(profile, num, proto, backup_path, old_locked)
            return

        time.sleep(SETTLE_SECONDS)
        if not _zapret2_running() or _get_strategy(num, proto) != strategy_n:
            _log(f"{profile}: после restart zapret2 не в running или get не подтвердил strategy={strategy_n} -- откатываю.")
            _rollback(profile, num, proto, backup_path, old_locked)
            return

        db.set_promoted_strategy(conn, candidate["id"], local_env_id, int(strategy_n))
        _log(f"{profile}: геном {candidate['id'][:12]} продвинут как strategy={strategy_n} (avg_score={candidate['avg_score']}, pulls={candidate['pulls']}).")
    finally:
        conn.close()


def _rollback(profile: str, num: int, proto: str, backup_path: str, old_locked: str | None) -> None:
    if not backup_path:
        _log(f"{profile}: ОТКАТ НЕВОЗМОЖЕН -- путь к backup не распознан из вывода apply. Нужно вмешательство человека.")
        return
    restore_out = _sudo(["bash", config.PROMOTE_APPLY_CLI, "restore", backup_path, config.ZAPRET2_CONFIG_PATH])
    if restore_out.returncode != 0:
        _log(f"{profile}: ОТКАТ КОНФИГА НЕ УДАЛСЯ -- {restore_out.stderr.strip()}. Нужно вмешательство человека, backup: {backup_path}")
        return
    if old_locked and old_locked.isdigit():
        _set_strategy(num, proto, old_locked)
    _restart_zapret2()
    _log(f"{profile}: откат выполнен -- конфиг восстановлен из {backup_path}, strategy вернута на {old_locked}.")


def _loop():
    global _rotate_idx, _last_fired_at
    while True:
        time.sleep(POLL_SECONDS)
        if not is_enabled():
            continue
        if time.monotonic() - _last_fired_at < config.ZENITH_AUTORUN_INTERVAL_MINUTES * 60:
            continue
        profile = runner.RUNNABLE_PROFILES[_rotate_idx % len(runner.RUNNABLE_PROFILES)]
        _rotate_idx += 1
        _last_fired_at = time.monotonic()
        try:
            _try_promote_profile(profile)
        except Exception as e:
            _log(f"{profile}: необработанное исключение -- {e}")


_thread = threading.Thread(target=_loop, daemon=True, name="zenith-promoter")
_thread.start()
