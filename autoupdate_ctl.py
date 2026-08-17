"""Пульт для автообновления экосистемы (z2r_autobench/Zenith/z0r-panel/
Zenith-TG) -- то же самое, что z0r пункт 26, просто кнопками. Состояние
(включено/выключено на проект) живёт в /etc/z2r_autobench/autoupdate.conf
-- ОДИН файл на CLI и панель, читают/пишут его оба, не дублируют
состояние отдельно (см. z2r_autobench/autoupdate.sh докстринг).

Границы те же, что у daemon_ctl.py: панель тут ТОЛЬКО пульт. Установку
самого таймера (копирование .service/.timer, daemon-reload) панель
делать не должна -- это шаг через z0r/SSH, см. sudoers-грант в
z0r::ensure_panel_runtime_grants (сознательно не включает установку)."""
import subprocess

AUTOUPDATE_SCRIPT = "/opt/z2r_autobench/autoupdate.sh"
CONF_PATH = "/etc/z2r_autobench/autoupdate.conf"
LOG_DIR = "/opt/z2r_autobench/logs/autoupdate"

PROJECTS = ("z2r_autobench", "zenith", "panel", "tgrelay")
PROJECT_LABELS = {
    "z2r_autobench": "z2r_autobench (сам инструмент + autotune-daemon)",
    "zenith": "Zenith (docker compose up -d --build)",
    "panel": "z0r-panel (pip install + systemctl restart zenith-panel)",
    "tgrelay": "Zenith-TG (pip install + systemctl restart tg-transparent-relay)",
}


def _run(cmd: list, timeout: int = 10) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def _read_conf() -> dict:
    """Читает autoupdate.conf НАПРЯМУЮ (не через sudo -- файл 644,
    читать может кто угодно, sudo нужен только на ЗАПИСЬ)."""
    flags = {}
    try:
        with open(CONF_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                flags[key] = value
    except FileNotFoundError:
        pass
    return flags


def project_enabled(project: str) -> bool:
    key = f"AUTOUPDATE_{project.upper()}"
    return _read_conf().get(key) == "1"


def project_last_update(project: str) -> str | None:
    try:
        with open(f"{LOG_DIR}/{project}.last_update") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def project_statuses() -> list[dict]:
    return [
        {
            "project": p,
            "label": PROJECT_LABELS[p],
            "enabled": project_enabled(p),
            "last_update": project_last_update(p),
        }
        for p in PROJECTS
    ]


def set_project(project: str, enabled: bool) -> str | None:
    """None -- успех, иначе текст ошибки для показа в форме."""
    if project not in PROJECTS:
        return f"Неизвестный проект: {project!r}"
    out = _run(["sudo", "-n", "/usr/bin/bash", AUTOUPDATE_SCRIPT, "--set", project, "1" if enabled else "0"])
    return None if out.returncode == 0 else (out.stderr.strip() or "autoupdate.sh --set завершился с ошибкой")


def run_now() -> str | None:
    """Триггерит один прогон z2r-autoupdate.service (oneshot) прямо
    сейчас, не дожидаясь таймера. Требует, чтобы юнит уже был установлен
    через z0r -- если нет, sudo -n сам вернёт понятную ошибку от
    systemctl (\"Unit ... not found\"), отдельно не проверяем тут."""
    out = _run(["sudo", "-n", "systemctl", "start", "z2r-autoupdate.service"], timeout=5)
    return None if out.returncode == 0 else (out.stderr.strip() or "systemctl start z2r-autoupdate.service завершился с ошибкой")


def timer_status() -> str:
    """'enabled'/'disabled'/'not-found' -- показывает, установлен ли
    таймер вообще (панель сама его не ставит, см. докстринг файла)."""
    out = _run(["sudo", "-n", "systemctl", "is-enabled", "z2r-autoupdate.timer"], timeout=5)
    text = (out.stdout or out.stderr or "unknown").strip()
    return text if text else "unknown"
