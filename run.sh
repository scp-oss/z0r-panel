#!/usr/bin/env bash
# Запуск панели. Отдельный venv от orchestrator/ (разные зависимости --
# FastAPI/uvicorn тут не нужны в orchestrator, и наоборот). См.
# zenith-panel.service для systemd.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
exec venv/bin/python3 main.py
