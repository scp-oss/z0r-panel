"""Управление autotune-daemon.service с панели -- фоновая служба
автоподстройки z2r (health-check + re-tune через rank_strategies.sh, см.
autotune_daemon.sh в z2r_autobench), в отличие от runner.py она НЕ
разовый прогон, а постоянно работающий systemd-юнит. Кнопки на /controls
делают ровно то же самое, что человек руками через z0r пункт 13 --
start/stop/is-active/journalctl, ничего больше (см. z0r::
ensure_panel_runtime_grants -- sudoers сужен буквально до этих 4 команд,
без единого "*")."""
import subprocess

SERVICE = "autotune-daemon"
LOG_LINES = 200


def _run(cmd: list, timeout: int = 10) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def is_active() -> str:
    """'active'/'inactive'/'failed'/'unknown' -- systemctl is-active
    печатает ровно одно слово что в success, что в fail-случае."""
    out = _run(["sudo", "-n", "systemctl", "is-active", SERVICE])
    return (out.stdout or out.stderr or "unknown").strip()


def start() -> str | None:
    out = _run(["sudo", "-n", "systemctl", "start", SERVICE])
    return None if out.returncode == 0 else (out.stderr.strip() or "systemctl start завершился с ошибкой")


def stop() -> str | None:
    out = _run(["sudo", "-n", "systemctl", "stop", SERVICE])
    return None if out.returncode == 0 else (out.stderr.strip() or "systemctl stop завершился с ошибкой")


def log_tail() -> str:
    out = _run(["sudo", "-n", "journalctl", "-u", SERVICE, "-n", str(LOG_LINES), "--no-pager"], timeout=15)
    return out.stdout or out.stderr or ""
