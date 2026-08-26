"""Локальный лаунчер прогонов Zenith orchestrator/main.py из панели --
ТОЛЬКО для локальной ноды (той же машины, что панель, см. main.py
controls_run) -- нажатие кнопки просто запускает существующий main.py тем
же способом, что человек руками из терминала (sudo venv/bin/python3
main.py --profile ...), тот же venv, тот же CLI, лог в файл. Один прогон
одновременно -- второй запрос, пока первый не завершился, получает отказ,
панель НЕ ставит запросы в очередь (см. README "Границы ответственности
панели" -- панель ничего не решает и не применяет сама, тут просто самый
тонкий возможный враппер над ручной командой).

Popen-объект и путь к логу держим в памяти процесса панели -- uvicorn
однопроцессный для этой панели (см. run.sh). САМ факт "прогон идёт" при
этом переживает рестарт панели даже так -- см. RUN_LOCK_FILE ниже:
запускаемая команда обёрнута в `flock -n`, лок держит ДОЧЕРНИЙ процесс
(sudo/python3), не память этого Python-процесса, так что рестарт панели
не может привести к ДВУМ одновременным прогонам (второй click "запустить"
после рестарта, пока старый прогон ещё жив, получит честный отказ от
самого flock, не от _state, которое после рестарта пустое). Статус-
эндпоинт после рестарта панели всё ещё не знает о старом прогоне, пока
он не допишет лог и не завершится -- это отдельный, чисто
информационный пробел (см. status()), не safety-риск, флок закрывает
именно риск дублирующегося запуска (найдено при аудите перед деплоем на
Provider B 2026-08-17)."""
import datetime
import os
import re
import subprocess
import threading

import config

# Подмножество z2r-профилей, которое Zenith'овский orchestrator/main.py
# реально умеет запускать -- см. Zenith/orchestrator/genome.py::
# PROFILE_FILTER_TYPE/PROFILE_FILTERS, там определены ТОЛЬКО эти 4 (ни
# GV_TLS, ни fallback/dev-профили из main.py::PROFILE_NUMBERS там не
# описаны -- main.py --profile GV_TLS не упадёт сразу, но результат ничем
# не подкреплён реальным боевым фильтром песочницы). Живёт здесь, а не в
# main.py -- используется и кнопкой "запустить подбор" (main.py), и
# планировщиком периодического автозапуска (autorun.py), а main.py
# импортировать из autorun.py означало бы цикл импорта (main.py уже
# импортирует и runner, и autorun).
RUNNABLE_PROFILES = ["YT_TLS", "RKN_TLS", "DS_TLS", "VOICE_UDP"]

# Фиксированный путь, НЕ по имени профиля/раунда -- один общий лок на "хоть
# какой-то прогон main.py сейчас идёт" (тот же принцип "один прогон
# одновременно", что и раньше, просто теперь на уровне ОС, а не только
# памяти процесса панели, см. докстринг выше). z0r::ensure_panel_runtime_grants
# грантует sudoers ИМЕННО на этот литеральный путь, не на wildcard.
RUN_LOCK_FILE = os.path.join(config.ZENITH_ORCHESTRATOR_DIR, ".run.lock")

_lock = threading.Lock()
_state = {
    "proc": None,
    "profile": None,
    "domain": None,
    "rounds": None,
    "started_at": None,
    "finished_at": None,
    "exit_code": None,
    "log_path": None,
}

LOG_DIR = os.path.join(config.PANEL_DIR, "run_logs")

# main.py печатает раунд двумя строками (см. orchestrator/main.py::run) --
# заголовок с геномом, затем результат. Парсим их, чтобы показывать в
# панели не сырой лог целиком, а только успешные попытки, лучшие по
# байтам первыми -- сырой построчный вывод для однопроцессного локального
# запуска малоинформативен, важны только рабочие кандидаты.
_ROUND_RE = re.compile(r"^\[(\d+)/(\d+)\] (\S+) op=(\S+) -> (.+)$")
_RESULT_RE = re.compile(r"^\s*-> (OK|fail) \((\d+) bytes, (\d+)ms\), домен=(.+)$")
ERROR_TAIL_LINES = 25


def _is_running_locked() -> bool:
    proc = _state["proc"]
    if proc is None:
        return False
    if proc.poll() is None:
        return True
    if _state["exit_code"] is None:
        _state["exit_code"] = proc.returncode
        _state["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    return False


def stop() -> str | None:
    """None при успешной отправке SIGTERM, иначе текст ошибки. proc --
    это сам процесс sudo (Popen отслеживает его, не дочерний main.py
    напрямую) -- sudo по умолчанию перехватывает и пробрасывает SIGTERM
    дальше в свою команду, отдельно убивать main.py не нужно. main.py
    сигнал не ловит -- прерывается сразу же, как при ручном Ctrl+C в
    терминале, ничего специально не подчищая (тот же риск, что и при
    ручном прерывании, не новый)."""
    with _lock:
        if not _is_running_locked():
            return "Прогон не идёт — нечего останавливать."
        _state["proc"].terminate()
        return None


def start(profile: str, rounds: int, domain: str | None) -> str | None:
    """None при успешном старте, иначе текст ошибки для показа в панели."""
    with _lock:
        if _is_running_locked():
            return "Прогон уже идёт — дождись завершения текущего."

        try:
            os.makedirs(LOG_DIR, exist_ok=True)
        except OSError as e:
            return f"Не удалось создать {LOG_DIR}: {e}"
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = os.path.join(LOG_DIR, f"{ts}-{profile}.log")

        # Абсолютный путь к main.py, не относительный -- sudoers матчит
        # ТОЛЬКО командную строку буквально, cwd= ниже ему не видно (см.
        # z0r::ensure_panel_runtime_grants, найдено при аудите перед
        # деплоем на Provider B 2026-08-17). Раньше был просто "main.py" +
        # cwd=ZENITH_ORCHESTRATOR_DIR -- работало только потому, что ЭТОТ
        # единственный call site всегда правильно ставил cwd; абсолютный
        # путь не полагается на это допущение.
        cmd = [
            "sudo", "-n", "flock", "-n", RUN_LOCK_FILE,
            config.ZENITH_VENV_PYTHON,
            os.path.join(config.ZENITH_ORCHESTRATOR_DIR, "main.py"),
            "--profile", profile, "--rounds", str(rounds),
        ]
        if domain:
            cmd += ["--domain", domain]

        try:
            log_f = open(log_path, "w")
        except OSError as e:
            return f"Не удалось открыть лог-файл: {e}"

        try:
            proc = subprocess.Popen(
                cmd, cwd=config.ZENITH_ORCHESTRATOR_DIR,
                stdout=log_f, stderr=subprocess.STDOUT,
            )
        except OSError as e:
            log_f.close()
            return f"Не удалось запустить прогон: {e}"
        finally:
            log_f.close()

        _state.update({
            "proc": proc, "profile": profile, "domain": domain, "rounds": rounds,
            "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "finished_at": None, "exit_code": None, "log_path": log_path,
        })
        return None


def _parse_log(text: str):
    lines = text.splitlines()
    successes = []
    last_round = None
    for i in range(len(lines) - 1):
        m1 = _ROUND_RE.match(lines[i])
        if not m1:
            continue
        last_round = {"round": int(m1.group(1)), "rounds": int(m1.group(2))}
        m2 = _RESULT_RE.match(lines[i + 1])
        if m2 and m2.group(1) == "OK":
            successes.append({
                "round": int(m1.group(1)), "rounds": int(m1.group(2)),
                "op": m1.group(4), "args": m1.group(5),
                "bytes": int(m2.group(2)), "latency_ms": int(m2.group(3)), "domain": m2.group(4),
            })
    successes.sort(key=lambda r: r["bytes"], reverse=True)
    return successes, last_round, lines


def status() -> dict:
    with _lock:
        running = _is_running_locked()
        snapshot = dict(_state)

    successes, last_round, lines = [], None, []
    if snapshot["log_path"] and os.path.exists(snapshot["log_path"]):
        with open(snapshot["log_path"], errors="replace") as f:
            successes, last_round, lines = _parse_log(f.read())

    # Лог целиком неинформативен (сплошные fail) -- но если прогон упал с
    # ошибкой (не 0 и не None), successes часто пуст, а разбираться, ПОЧЕМУ
    # упало, всё равно нужно -- хвост сырого вывода тогда единственная
    # зацепка (там же питоновский traceback, если main.py упал сам).
    error_tail = None
    if snapshot["exit_code"] not in (None, 0):
        error_tail = "\n".join(lines[-ERROR_TAIL_LINES:])

    return {
        "running": running,
        "profile": snapshot["profile"],
        "domain": snapshot["domain"],
        "rounds": snapshot["rounds"],
        "started_at": snapshot["started_at"],
        "finished_at": snapshot["finished_at"],
        "exit_code": snapshot["exit_code"],
        "last_round": last_round,
        "successes": successes,
        "error_tail": error_tail,
    }
