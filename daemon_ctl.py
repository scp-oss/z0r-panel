"""Тонкий контроль над systemd-юнитом с панели -- start/stop/is-active/
journalctl, ничего больше (см. z0r::ensure_panel_runtime_grants -- sudoers
сужен буквально до этих 4 команд НА КАЖДЫЙ юнит, без единого "*").

Используется для трёх демонов, ни один из которых НЕ является частью
панели и НЕ зависит от неё -- панель тут ТОЛЬКО пульт (start/stop/log)
поверх уже существующего systemd-юнита, по прямому запросу "панель это
модуль... функционал должен быть в CLI и без панели":

  - autotune-daemon -- z2r_autobench/autotune_daemon.sh (health-check +
    точечный re-tune).
  - zenith-autorun -- Zenith/zenith_autorun.sh (периодическая генерация
    кандидатов).
  - zenith-promoter -- Zenith/orchestrator/auto_promoter.py --loop
    (автопродвижение в /opt/zapret2/config БЕЗ участия человека).

Вся логика этих демонов живёт в их СОБСТВЕННЫХ репозиториях/юнитах --
если панель выключена/не установлена, все три продолжают работать как
ни в чём не бывало."""
import subprocess


class SystemdServiceCtl:
    def __init__(self, service: str, log_lines: int = 200):
        self.service = service
        self.log_lines = log_lines

    def _run(self, cmd: list, timeout: int = 10) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return subprocess.CompletedProcess(cmd, 1, "", str(e))

    def is_active(self) -> str:
        """'active'/'inactive'/'failed'/'unknown' -- systemctl is-active
        печатает ровно одно слово что в success, что в fail-случае."""
        out = self._run(["sudo", "-n", "systemctl", "is-active", self.service])
        return (out.stdout or out.stderr or "unknown").strip()

    def start(self) -> str | None:
        out = self._run(["sudo", "-n", "systemctl", "start", self.service])
        return None if out.returncode == 0 else (out.stderr.strip() or "systemctl start завершился с ошибкой")

    def stop(self) -> str | None:
        out = self._run(["sudo", "-n", "systemctl", "stop", self.service])
        return None if out.returncode == 0 else (out.stderr.strip() or "systemctl stop завершился с ошибкой")

    def restart(self) -> str | None:
        """Отдельно от start/stop -- start на уже запущенном юните просто
        no-op (не циклит процесс), а тут именно нужно перезапустить
        зависший демон одним кликом, не жать stop+start по очереди (живой
        запрос 2026-08-31)."""
        out = self._run(["sudo", "-n", "systemctl", "restart", self.service])
        return None if out.returncode == 0 else (out.stderr.strip() or "systemctl restart завершился с ошибкой")

    def log_tail(self) -> str:
        out = self._run(
            ["sudo", "-n", "journalctl", "-u", self.service, "-n", str(self.log_lines), "--no-pager"],
            timeout=15,
        )
        return out.stdout or out.stderr or ""


autotune_daemon = SystemdServiceCtl("autotune-daemon")
zenith_autorun = SystemdServiceCtl("zenith-autorun")
zenith_promoter = SystemdServiceCtl("zenith-promoter")


def check_git_updates(repo_dir: str) -> str:
    """Read-only: git fetch + сравнение HEAD с origin/main -- НИЧЕГО не
    применяет (сам git pull делает z0r-скрипт/автообновление, см.
    z2r_autobench::_check_git_updates, ЭТА функция -- панельный аналог
    того же самого, специально не дублирующий логику обновления, только
    показывающий, есть ли смысл её запускать)."""
    def _run(cmd: list, timeout: int = 15) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return subprocess.CompletedProcess(cmd, 1, "", str(e))

    fetch = _run(["sudo", "-n", "git", "-C", repo_dir, "fetch", "origin", "main"])
    if fetch.returncode != 0:
        return f"Не удалось получить origin/main: {(fetch.stderr or fetch.stdout or '').strip()}"
    count = _run(["sudo", "-n", "git", "-C", repo_dir, "rev-list", "--count", "HEAD..origin/main"])
    behind = (count.stdout or "").strip()
    if not behind.isdigit():
        return f"Не удалось сравнить с origin/main: {(count.stderr or '').strip()}"
    if behind == "0":
        return "Актуально — HEAD совпадает с origin/main."
    log = _run(["sudo", "-n", "git", "-C", repo_dir, "log", "--oneline", "HEAD..origin/main"])
    log_text = (log.stdout or "").strip()
    return f"Отстаёт от origin/main на {behind} коммит(ов):\n{log_text}" if log_text else f"Отстаёт от origin/main на {behind} коммит(ов)."
