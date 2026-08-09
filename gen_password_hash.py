#!/usr/bin/env python3
"""Генерирует PANEL_ADMIN_PASSWORD_HASH для .env -- пароль в открытом виде
никогда не хранится и не логируется, только pbkdf2-хэш (см. auth.py)."""
import getpass

import auth

if __name__ == "__main__":
    password = getpass.getpass("Пароль для панели: ")
    confirm = getpass.getpass("Повтори: ")
    if password != confirm:
        raise SystemExit("Пароли не совпали")
    print()
    print("Добавь в .env:")
    print(f"PANEL_ADMIN_PASSWORD_HASH={auth.hash_password(password)}")
