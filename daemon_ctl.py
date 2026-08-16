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

    def log_tail(self) -> str:
        out = self._run(
            ["sudo", "-n", "journalctl", "-u", self.service, "-n", str(self.log_lines), "--no-pager"],
            timeout=15,
        )
        return out.stdout or out.stderr or ""


autotune_daemon = SystemdServiceCtl("autotune-daemon")
zenith_autorun = SystemdServiceCtl("zenith-autorun")
zenith_promoter = SystemdServiceCtl("zenith-promoter")
