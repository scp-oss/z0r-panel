"""Периодический автозапуск Zenith'овского orchestrator/main.py -- по
кругу перебирает runner.RUNNABLE_PROFILES, по одному профилю за
срабатывание (не все разом -- см. config.py::ZENITH_AUTORUN_*
докстринг про риск эскалации DPI-инспекции при плотном потоке
неудачных хендшейков, CLAUDE.md "Ban/rate-limit avoidance"). Использует
тот же runner.start(), что и кнопка "запустить подбор" -- если в
момент срабатывания уже идёт чей-то прогон (ручной или предыдущий
автозапуск ещё тянется), runner.start() просто вернёт ошибку "прогон
уже идёт", это срабатывание тихо пропускается, следующее будет через
ZENITH_AUTORUN_INTERVAL_MINUTES.

Включено/выключено -- состояние в файле (STATE_FILE), не только в
памяти процесса панели, чтобы пережить `systemctl restart zenith-panel`
(иначе неожиданный рестарт панели тихо выключил бы фоновый подбор без
какого-либо сигнала оператору). Планировщик -- один daemon-поток,
стартует сам при импорте модуля (см. main.py) и поллит STATE_FILE
каждые POLL_SECONDS -- отдельного systemd-юнита/sudoers для этого не
нужно, всё работает через уже существующий runner.py."""
import json
import os
import threading
import time

import config
import runner

STATE_FILE = os.path.join(config.PANEL_DIR, "autorun_state.json")
POLL_SECONDS = 30

_rotate_idx = 0
_last_fired_at = 0.0


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
        runner.start(profile, config.ZENITH_AUTORUN_ROUNDS, None)


_thread = threading.Thread(target=_loop, daemon=True, name="zenith-autorun")
_thread.start()
