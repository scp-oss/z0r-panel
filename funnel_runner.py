"""Локальный лаунчер rank_strategies.sh --domain ... --funnel из панели --
ОТДЕЛЬНЫЙ от runner.py (который запускает Zenith orchestrator/main.py, а
тот работает ТОЛЬКО в изолированной sandbox, никогда не трогает боевой
config). rank_strategies.sh наоборот НАПРЯМУЮ переключает боевую стратегию
профиля на каждого кандидата и пробует её живым трафиком (см. его же
докстринг про "побочный эффект: пока идёт тест, стратегия профиля временно
меняется для ВСЕХ доменов под этим профилем") — но у него уже есть СВОЙ
лок (acquire_tune_lock/TUNE_LOCK_FILE в z2r_autobench_lib.sh, общий с
autotune_daemon.sh/rank_quic.sh/rank_voice.sh/test_custom_domain.sh), так
что никакого дополнительного `flock`-обёртывания командной строки не
нужно — в отличие от runner.py::RUN_LOCK_FILE, который добавлен ИМЕННО
потому, что у main.py такого лока нет.

Домен -> профиль определяется САМИМ rank_strategies.sh (--domain
переопределяет --profile целиком, см. его же комментарий и
z2r_detect_governing_profile в z2r_autobench_lib.sh) — панель не
дублирует эту логику, только передаёт домен как есть."""
import datetime
import os
import re
import subprocess
import threading

import config

_lock = threading.Lock()
_state = {
    "proc": None,
    "domain": None,
    "passes": None,
    "started_at": None,
    "finished_at": None,
    "exit_code": None,
    "log_path": None,
}

LOG_DIR = os.path.join(config.PANEL_DIR, "run_logs")

# Форматы строк, которые печатает именно rank_strategies.sh (см. его
# исходник) -- отдельные от runner.py::_ROUND_RE/_RESULT_RE, которые
# парсят СОВСЕМ другой формат вывода main.py.
_DOMAIN_RESOLVED_RE = re.compile(r"^Домен (\S+) -> профиль (\d+) \((.+)\)$")
_ROUND_START_RE = re.compile(r"^--- Проход (\d+)/(\d+) \(кандидатов: (\d+)\) ---$")
_SUCCESS_RE = re.compile(r"^\s*strategy=(\d+) -> OK \((\d+) bytes, проход (\d+)/(\d+)\)$")
_ROUND_DONE_RE = re.compile(r"^\s*проход (\d+) завершён, выжило кандидатов: (\d+)$")
_ORDERED_RE = re.compile(r"^ORDERED_SUCCESS: ?(.*)$")
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
    """Тот же принцип, что runner.stop() -- proc это сам процесс sudo,
    SIGTERM пробрасывается им дальше в bash/rank_strategies.sh, у
    которого уже есть свой TERM-трап (handle_interrupt_signal) --
    откатывает locked.tsv к исходной стратегии перед выходом сам, ничего
    подчищать здесь не нужно."""
    with _lock:
        if not _is_running_locked():
            return "Прогон не идёт — нечего останавливать."
        _state["proc"].terminate()
        return None


def start(domain: str, passes: int, settle: int | None, attempts: int | None) -> str | None:
    """None при успешном старте, иначе текст ошибки для показа в панели."""
    with _lock:
        if _is_running_locked():
            return "Прогон уже идёт — дождись завершения текущего."
        domain = domain.strip()
        if not domain:
            return "Нужен домен."

        try:
            os.makedirs(LOG_DIR, exist_ok=True)
        except OSError as e:
            return f"Не удалось создать {LOG_DIR}: {e}"
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_domain = re.sub(r"[^a-zA-Z0-9.-]", "_", domain)[:60]
        log_path = os.path.join(LOG_DIR, f"{ts}-funnel-{safe_domain}.log")

        # Абсолютный путь к rank_strategies.sh -- sudoers матчит командную
        # строку буквально (см. z0r::ensure_panel_runtime_grants,
        # sudoers_funnel_cmd), cwd= ниже ему не видно, тот же принцип, что
        # у sudoers_run_cmd/runner.py.
        cmd = [
            "sudo", "-n", "bash", config.RANK_STRATEGIES_CLI,
            "--domain", domain, "--funnel", "--passes", str(passes),
        ]
        if settle:
            cmd += ["--settle", str(settle)]
        if attempts:
            cmd += ["--attempts", str(attempts)]

        try:
            log_f = open(log_path, "w")
        except OSError as e:
            return f"Не удалось открыть лог-файл: {e}"

        try:
            proc = subprocess.Popen(
                cmd, cwd=config.Z2R_AUTOBENCH_DIR,
                stdout=log_f, stderr=subprocess.STDOUT,
            )
        except OSError as e:
            log_f.close()
            return f"Не удалось запустить прогон: {e}"
        finally:
            log_f.close()

        _state.update({
            "proc": proc, "domain": domain, "passes": passes,
            "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "finished_at": None, "exit_code": None, "log_path": log_path,
        })
        return None


def _parse_log(text: str):
    """profile_info -- {"domain","profile","title"} после строки "Домен ->
    профиль ..." (печатает сам rank_strategies.sh при --domain), либо None,
    пока прогон не дошёл до этой строки.

    rounds -- список ЗАВЕРШЁННЫХ проходов
    [{"round":1,"rounds":3,"candidates":12,"survivors":5}, ...].

    pending_round -- проход СЕЙЧАС считается (заголовок "--- Проход N/M
    (кандидатов: X) ---" уже напечатан, а "проход N завершён..." ещё нет)
    -- та же идея разрыва, что runner.py::pending_round, здесь разрыв --
    сам перебор кандидатов текущего прохода (каждый -- переключение
    стратегии + settle-пауза + проба), может занимать заметное время.

    successes -- реалтайм-список сработавших кандидатов В ПОРЯДКЕ
    появления в логе (не отсортированы) -- именно это даёт "если
    появляется успешный, тест ещё идёт -- показывается, что тест
    продолжается, и появляются ещё" из живого запроса.

    ordered -- финальный список ORDERED_SUCCESS -- появляется только
    когда весь прогон (все проходы + агрегация) уже завершён."""
    lines = text.splitlines()
    profile_info = None
    rounds = []
    successes = []
    pending_round = None
    ordered = None
    for line in lines:
        m = _DOMAIN_RESOLVED_RE.match(line)
        if m:
            profile_info = {"domain": m.group(1), "profile": m.group(2), "title": m.group(3)}
            continue
        m = _ROUND_START_RE.match(line)
        if m:
            pending_round = {"round": int(m.group(1)), "rounds": int(m.group(2)), "candidates": int(m.group(3))}
            continue
        m = _SUCCESS_RE.match(line)
        if m:
            successes.append({
                "strategy": m.group(1), "bytes": int(m.group(2)),
                "round": int(m.group(3)), "rounds": int(m.group(4)),
            })
            continue
        m = _ROUND_DONE_RE.match(line)
        if m:
            rnd = int(m.group(1))
            if pending_round and pending_round["round"] == rnd:
                entry = dict(pending_round)
            else:
                entry = {"round": rnd, "rounds": None, "candidates": None}
            entry["survivors"] = int(m.group(2))
            rounds.append(entry)
            pending_round = None
            continue
        m = _ORDERED_RE.match(line)
        if m:
            ordered = [s for s in m.group(1).split() if s]
            continue
    return profile_info, rounds, successes, pending_round, ordered, lines


def status() -> dict:
    with _lock:
        running = _is_running_locked()
        snapshot = dict(_state)

    profile_info, rounds, successes, pending_round, ordered, lines = None, [], [], None, None, []
    if snapshot["log_path"] and os.path.exists(snapshot["log_path"]):
        with open(snapshot["log_path"], errors="replace") as f:
            profile_info, rounds, successes, pending_round, ordered, lines = _parse_log(f.read())

    # Тот же смысл, что error_tail в runner.py -- если прогон упал не с
    # кодом 0/None, хвост сырого вывода (там же bash traceback/сообщение
    # об ошибке acquire_tune_lock, если кто-то другой уже крутит
    # стратегии) -- единственная зацепка, почему.
    error_tail = None
    if snapshot["exit_code"] not in (None, 0):
        error_tail = "\n".join(lines[-ERROR_TAIL_LINES:])

    return {
        "running": running,
        "domain": snapshot["domain"],
        "passes": snapshot["passes"],
        "started_at": snapshot["started_at"],
        "finished_at": snapshot["finished_at"],
        "exit_code": snapshot["exit_code"],
        "profile_info": profile_info,
        "rounds": rounds,
        "pending_round": pending_round,
        "successes": successes,
        "ordered": ordered,
        "error_tail": error_tail,
    }
