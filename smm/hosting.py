from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from .config import atomic_json, read_config
from . import __version__
from .store import Store


def configure(environ=None):
    env = os.environ if environ is None else environ
    root = Path(env.get("SMM_DATA_DIR", "/data/smm")).expanduser()
    if not root.is_absolute():
        raise ValueError("SMM_DATA_DIR должен быть абсолютным путём, например /data/smm.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = read_config(root)
    token = env.get("BOT_TOKEN", "").strip() or config.get("telegram_token", "")
    if not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,100}", token):
        raise ValueError("Укажи BOT_TOKEN из BotFather в ENV проекта.")
    raw_owner = env.get("OWNER_ID", "").strip()
    if raw_owner and (not raw_owner.isascii() or not raw_owner.isdigit() or not 0 < int(raw_owner) < 2**63):
        raise ValueError("OWNER_ID должен содержать твой положительный числовой Telegram ID.")
    store = Store(root / "smm.sqlite")
    previous_owner = store.get("owner", 0)
    owner = int(raw_owner) if raw_owner else previous_owner
    if not owner:
        raise ValueError("Для первого запуска укажи OWNER_ID — свой числовой Telegram ID.")
    if previous_owner and owner != previous_owner:
        raise ValueError("OWNER_ID отличается от сохранённого владельца. Существующая привязка не изменена.")
    # Check every value before persisting a token or assigning the owner.
    if config.get("telegram_token") != token:
        atomic_json(root / "config.json", dict(config, telegram_token=token))
    if not previous_owner:
        store.put("owner", owner)
    return root, store


def main():
    if sys.version_info < (3, 11):
        print("Нужен Python 3.11 или новее.", flush=True)
        return 1
    try:
        root, store = configure()
    except (ValueError, OSError) as error:
        print("Ошибка настройки: " + str(error), flush=True)
        return 1
    print("Python worker: Telegram polling. Данные: " + str(root), flush=True)
    print("SMM Manager " + __version__ + ": два предложения и публикации в выбранное тобой время.", flush=True)
    if not (root / "credentials.json").exists():
        print("Вход ChatGPT ещё не перенесён. Сайт доступен; ИИ требует отдельного подключения.", flush=True)
    if os.environ.get('SMM_WEB_ENABLED','1') != '0' and not store.get('web_backup_2',False):
        from .backup import backup_data
        try:
            snapshot=backup_data(root)
        except (OSError, ValueError) as error:
            print('Не удалось сделать резервную копию перед обновлением. Проверь место и права; данные не удалены.',flush=True)
            return 1
        store.put('web_backup_2',str(snapshot))
        print('Резервная копия перед веб-обновлением: '+str(snapshot),flush=True)
    from .__main__ import run
    run(root, store)
    return 0
