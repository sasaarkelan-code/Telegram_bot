from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .auth import Auth, AuthError
from .config import atomic_json, data_dir, read_config
from .net import NetError
from .store import Store


class ProcessLock:
    def __init__(self, path):
        self.path, self.file = path, None

    def __enter__(self):
        self.file = open(self.path, "a+b")
        self.file.seek(0)
        self.file.write(b"1")
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise ValueError("Бот уже запущен. Не открывай второй START.bat.") from None
        return self

    def __exit__(self, *args):
        if self.file:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()


def setup(root, store):
    old = read_config(root)
    print("SMM Manager — настройка\nСоздай бота через @BotFather → /newbot.")
    print("Токен вводится только здесь, на твоём компьютере, и не отображается.")
    token = getpass.getpass("Токен Telegram (Enter — оставить сохранённый): ").strip() or old.get("telegram_token", "")
    if not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,100}", token):
        raise ValueError("Не похоже на токен BotFather.")
    from .telegram import Telegram
    bot = Telegram(token).call("getMe")
    atomic_json(root / "config.json", {"telegram_token": token})
    print("Бот: @"+bot.get("username", ""))
    if not store.get("owner", 0):
        code = store.pair_code()
        print("\nТеперь запусти START.bat, открой своего бота и отправь:")
        print("/start "+code)
        print("Код действует 30 минут. Чтобы выпустить новый: python -m smm pair")
    else:
        print("Владелец уже привязан. Вход на сайт — через /web.")
    print("\nДалее: LOGIN.bat → вход ChatGPT → на сайте «Каналы» и «Продолжить».")
    print("Твои данные сохраняются в:", root)


def choose_model(root, store):
    auth = Auth(root)
    models = auth.login()
    print("\nДоступные твоей подписке модели:")
    for i, m in enumerate(models, 1):
        print(f'{i}. {m["name"]} ({m["slug"]})')
    print("Выбери экономичную быструю модель из списка. Автоматической замены на дорогую не будет.")
    number = input("Номер модели: ").strip()
    if not number.isdigit() or not 1 <= int(number) <= len(models):
        raise ValueError("Модель не выбрана. Вход сохранён; запусти python -m smm model.")
    store.put("model", models[int(number)-1]["slug"])
    store.put("model_catalog", models)
    print("Готово. Модель:", models[int(number)-1]["name"])
    print("Ни одного генеративного запроса для входа и выбора модели не выполнялось.")


def select_model(root, store):
    models = Auth(root).models()
    for i, m in enumerate(models, 1):
        print(f'{i}. {m["name"]} ({m["slug"]})')
    n = input("Номер модели: ").strip()
    if not n.isdigit() or not 1 <= int(n) <= len(models):
        raise ValueError("Некорректный номер.")
    store.put("model", models[int(n)-1]["slug"])
    print("Модель сохранена.")


def run(root, store):
    from .ai import AI
    from .engine import Engine
    from .telegram import Telegram
    from .web_access import WebAccess

    config = read_config(root)
    token = config.get("telegram_token")
    if not token:
        raise ValueError("Сначала выполни SETUP.bat.")
    with ProcessLock(root / "bot.lock"):
        store.recover()
        auth, telegram = Auth(root), Telegram(token, media_root=root / "media")
        engine = Engine(store, telegram, AI(store, auth))
        access = WebAccess(store, telegram)
        from .account import AccountGateway
        from .manager import Manager
        manager=Manager(store,AccountGateway(root))
        web_server = None
        if os.environ.get('SMM_WEB_ENABLED', '1') == '0':
            raise ValueError('Управление перенесено на сайт. Для запуска включи SMM_WEB_ENABLED=1.')
        from .webapp import start_server
        web_server = start_server(root,store,engine,auth,telegram,manager)
        try:
            access.configure()
        except NetError:
            print('Не удалось обновить список команд Telegram. Вход через /web доступен после восстановления связи.')
        scheduler = threading.Thread(target=engine.loop, daemon=True)
        scheduler.start()
        publisher = threading.Thread(target=engine.publisher_loop, daemon=True)
        publisher.start()
        account_worker=threading.Thread(target=manager.loop,args=(engine.shutdown,),daemon=True)
        account_worker.start()
        print("SMM Manager", __version__, "запущен. Управление и уведомления — только на сайте. Telegram — ключ для входа.")
        print("Это окно нужно оставить открытым. Ctrl+C — остановка.")
        print("Автоподбор до двух постов трижды в день. Время публикации каждому посту назначает владелец, МСК.")
        if not store.get("owner", 0):
            print("Владелец ещё не привязан. Отправь боту /start с кодом настройки.")
        import signal
        def terminate(signum, frame):
            raise KeyboardInterrupt
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, terminate)
        try:
            while True:
                try:
                    updates = telegram.updates(store.get("offset", 0))
                    for u in updates:
                        access.handle(u)
                        store.put("offset", u["update_id"]+1)
                except NetError:
                    # Telegram reconnects never create model requests.
                    print("Telegram временно недоступен. Ожидание соединения, без вызовов ИИ.")
                    time.sleep(5)
                except Exception as e:
                    store.notice('web-access-error:'+str(int(time.time())//60), 'Ошибка получения кода Telegram ('+type(e).__name__+'). Проверь подключение бота. Управление доступно на сайте.')
                    print('Ошибка Telegram-входа:', type(e).__name__)
                    time.sleep(2)
        except KeyboardInterrupt:
            manager.stop(reason='Процесс остановлен владельцем.')
            store.halt("Процесс остановлен на компьютере.")
            engine.shutdown.set()
            engine.wake.set()
            scheduler.join(timeout=3)
            if web_server: web_server.close()
            print("\nОстановлено. Счётчики и очередь сохранены.")


def doctor(root, store, network=False):
    import importlib.util
    from .budget import Budget
    print("SMM Manager", __version__)
    print("Python:", sys.version.split()[0])
    print("Данные:", root)
    print("Библиотека входа:", "есть" if importlib.util.find_spec("cryptography") else "установи requirements.txt")
    print("Telegram:", "настроен" if read_config(root).get("telegram_token") else "не настроен")
    print("Владелец:", "привязан" if store.get("owner", 0) else "нужна привязка")
    print("ChatGPT:", "вход сохранён" if Auth(root).saved() else "нужен LOGIN.bat")
    print("Модель:", store.get("model") or "не выбрана")
    print("Пауза:", store.get("paused", True))
    print("Расход:", json.dumps(Budget(store).usage(), ensure_ascii=False))
    if network:
        from .telegram import Telegram
        token = read_config(root).get("telegram_token")
        if token:
            Telegram(token).call("getMe")
            print("Telegram отвечает.")
        if Auth(root).saved():
            print("Каталог моделей доступен, моделей:", len(Auth(root).models()))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="SMM Manager — веб-студия управления каналами")
    parser.add_argument("--data", help="Отдельная папка данных")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("setup", "pair", "run", "stop", "model"):
        sub.add_parser(name)
    auth = sub.add_parser("auth")
    auth.add_argument("action", choices=["login", "status", "logout"])
    catalog = sub.add_parser("catalog")
    catalog.add_argument("path")
    doc = sub.add_parser("doctor")
    doc.add_argument("--network", action="store_true")
    args = parser.parse_args()
    if args.data:
        os.environ["SMM_DATA_DIR"] = args.data
    root = data_dir()
    store = Store(root / "smm.sqlite")
    try:
        if args.command == "setup":
            setup(root, store)
        elif args.command == "pair":
            if store.get("owner", 0):
                raise ValueError("Владелец уже привязан. Новый код не нужен.")
            print("В течение 30 минут отправь боту: /start "+store.pair_code())
        elif args.command == "auth":
            if args.action == "login":
                choose_model(root, store)
            elif args.action == "status":
                print("Вход сохранён." if Auth(root).saved() else "Вход не выполнен.")
            else:
                store.halt("Подключение ИИ отключено на компьютере.")
                ok = Auth(root).logout()
                store.put("model", "")
                print("Вход удалён. " + ("Отзыв подключения подтверждён." if ok else "Проверь отключение приложения в ChatGPT → Настройки → Безопасность."))
        elif args.command == "model":
            select_model(root, store)
        elif args.command == "run":
            run(root, store)
        elif args.command == "stop":
            store.halt("Аварийная остановка с компьютера.")
            print("ИИ и публикации остановлены. Для продолжения используй кнопку на сайте.")
        elif args.command == "catalog":
            from .sources import Sources
            print("Реальных товаров импортировано:", Sources(store).import_products(args.path))
        elif args.command == "doctor":
            doctor(root, store, args.network)
    except (ValueError, AuthError, NetError, OSError) as e:
        print("⚠️ "+str(e))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
