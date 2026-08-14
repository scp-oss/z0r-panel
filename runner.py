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
однопроцессный для этой панели (см. run.sh), пережить рестарт панели не
обязано (тот же прогон переживёт рестарт как процесс, лог на диске
никуда не денется, просто статус-эндпоинт после рестарта панели не будет
знать о нём, пока идёт -- достаточно для однопроцессной ручной кнопки)."""
import datetime
import os
import subprocess
import threading

import config

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
LOG_TAIL_BYTES = 32768


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


def start(profile: str, rounds: int, domain: str | None) -> str | None:
    """None при успешном старте, иначе текст ошибки для показа в панели."""
    with _lock:
        if _is_running_locked():
            return "Прогон уже идёт — дождись завершения текущего."

        os.makedirs(LOG_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = os.path.join(LOG_DIR, f"{ts}-{profile}.log")

        cmd = [
            "sudo", "-n", config.ZENITH_VENV_PYTHON, "main.py",
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


def status() -> dict:
    with _lock:
        running = _is_running_locked()
        snapshot = dict(_state)

    log_tail = ""
    if snapshot["log_path"] and os.path.exists(snapshot["log_path"]):
        with open(snapshot["log_path"], "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            log_tail = f.read().decode(errors="replace")

    return {
        "running": running,
        "profile": snapshot["profile"],
        "domain": snapshot["domain"],
        "rounds": snapshot["rounds"],
        "started_at": snapshot["started_at"],
        "finished_at": snapshot["finished_at"],
        "exit_code": snapshot["exit_code"],
        "log_tail": log_tail,
    }
