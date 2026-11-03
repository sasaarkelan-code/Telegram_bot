from __future__ import annotations

import html
import json
import re
import time
import threading
from datetime import datetime

from .auth import AuthError
from .budget import Budget
from .editor import image_prompt, render, utf16
from .net import NetError, public_url
from .store import TZ
from .telegram import MENU, keyboard

FIELDS = {
    "topic": "Тематика и что искать",
    "style": "Стиль текста",
    "times": "Время публикаций",
    "sources": "Источники RSS / JSON Feed",
    "include": "Ключевые слова",
    "exclude": "Стоп-слова и запреты",
    "max_age_hours": "Возраст материалов, часы",
    "budgets": "Бюджеты образов",
}
LIMIT_FIELDS = {
    "daily_tokens": ("Токенов за день", 12000, 200000),
    "rolling_tokens": ("Токенов за 5 часов", 12000, 100000),
    "channel_daily_tokens": ("На канал за день", 12000, 100000),
    "daily_requests": ("Запросов за день", 1, 40),
    "hourly_requests": ("Запросов за час", 1, 12),
    "lead_minutes": ("Минут до публикации", 30, 40),
}


def when(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M МСК")


def parse_times(text):
    values = re.split(r"[\s,;]+", text.strip())
    if not 1 <= len(values) <= 8 or len(set(values)) != len(values):
        raise ValueError("Укажи от 1 до 8 разных времён через пробел.")
    if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", x) for x in values):
        raise ValueError("Формат времени: 09:00 15:00 21:00.")
    return sorted(values)


class UI:
    def __init__(self, store, telegram, auth, engine):
        self.store, self.tg, self.auth, self.engine = store, telegram, auth, engine
        self._actor = threading.local()

    @property
    def owner(self):
        return getattr(self._actor, "user", None) or self.store.get("owner", 0)

    def send(self, text, rows=None, menu=False):
        return self.tg.send(self.owner, text, MENU if menu else (keyboard(rows) if rows else None))

    def home(self):
        paused = self.store.get("paused", True)
        self.send("<b>SMM Manager</b>\n\n"
                  + ("⏸ На паузе: "+html.escape(self.store.get("pause_reason", "")) if paused else "🟢 Работает по расписанию")
                  + "\nВсе часы — московские. Публикуются только одобренные посты.\n\n"
                    "📥 Предложения — выбрать и проверить пост\n"
                    "✅ Очередь — что и когда выйдет\n"
                    "🎛 Фильтры — тематика, источники и ограничения\n"
                    "🛑 Стоп — остановить ИИ и публикации", menu=True)

    def channel_list(self, filters=False):
        rows = [[(f'{"🟢" if ch["enabled"] else "⚪"} {ch["name"]}', f'{"filters" if filters else "channel"}:{ch["id"]}')]
                for ch in self.store.channels()]
        rows.append([("➕ Добавить канал", "add")])
        if filters:
            rows.append([("🛡 Общие ограничения ИИ", "limits_edit")])
        self.send("<b>"+("Фильтры по каналам" if filters else "Мои каналы")+"</b>\nВыбери канал.", rows)

    def channel_screen(self, cid):
        ch = self.need_channel(cid)
        cfg = ch["config"]
        self.send(f'<b>{html.escape(ch["name"])}</b>\n\n'
                  f'Подключён: {"да" if ch["target"] else "нет"}\n'
                  f'Режим: {cfg["mode"]}\n'
                  f'Выходы: {", ".join(cfg["times"])} МСК\n'
                  f'Статус: {"работает" if ch["enabled"] else "выключен"}',
                  [[("🔗 Подключить канал", f"target:{cid}")],
                   [(("⏸ Выключить" if ch["enabled"] else "▶️ Включить"), f"toggle:{cid}")],
                   [("🎛 Все фильтры", f"filters:{cid}"), ("📝 Переименовать", f"rename:{cid}")],
                   [("📰 Режим канала", f"mode:{cid}"), ("🔄 Подготовить в текущем окне", f"prepare:{cid}")]])

    def need_channel(self, cid):
        ch = self.store.channel(int(cid))
        if not ch:
            raise ValueError("Канал не найден.")
        return ch

    def filters(self, cid):
        ch = self.need_channel(cid)
        cfg = ch["config"]
        topic = html.escape(cfg["topic"][:450])
        rows = [[(label, f"field:{cid}:{key}")] for key, label in FIELDS.items()]
        rows += [[(f'Картинка обязательна: {"да" if cfg["require_photo"] else "нет"}', f"photo_required:{cid}")]]
        if cfg["mode"] == "fashion":
            rows += [[(f'Веб-поиск WB/Ozon: {"включён" if cfg["web_search"] else "выключен"}', f"web:{cid}")],
                     [("📦 Как загрузить каталог", "catalog_help")]]
        self.send(f'<b>🎛 {html.escape(ch["name"])}</b>\n\n{topic}\n\n'
                  f'Время: {", ".join(cfg["times"])}\n'
                  f'Свежесть: до {cfg["max_age_hours"]} ч.\n'
                  f'Источников: {len(cfg["sources"])}\n'
                  f'Ключевые: {html.escape(", ".join(cfg["include"])) or "вся тематика"}\n'
                  f'Исключить: {html.escape(", ".join(cfg["exclude"])) or "не задано"}\n\n'
                  "Изменение фильтров отменяет одобрения ещё не вышедших постов этого канала.", rows)

    def limits(self, edit=False):
        limits = self.store.get("limits")
        u = Budget(self.store).usage()
        self.send("<b>📊 Расход и защита</b>\n\n"
                  f'День: {u["tokens"]:,} / {limits["daily_tokens"]:,} токенов\n'
                  f'Последние 5 ч: {u["rolling"]:,} / {limits["rolling_tokens"]:,}\n'
                  f'Запросы за день: {u["requests"]} / {limits["daily_requests"]}\n'
                  f'Запросы за час: {u["hourly"]} / {limits["hourly_requests"]}\n'
                  f'Неопределённых/незавершённых расходов: {u["uncertain"]}\n\n'
                  "Учитываются input + output из usage. Если usage не пришёл, засчитывается резерв, и бот останавливается.\n"
                  "Это расход только этого бота, не остаток всей подписки. Запрос в работе может потратить больше резерва: "
                  "официальный вход не поддерживает серверный max_output_tokens.\n"
                  "Счётчики после перезапуска и кнопки «Продолжить» сохраняются.",
                  [[(value[0], "limit:"+key)] for key, value in LIMIT_FIELDS.items()] if edit else
                  [[("⚙️ Настроить ограничения", "limits_edit")],
                   [("🛑 Аварийная остановка", "halt")]])

    def pending(self, page=0, queue=False):
        states = ("approved", "unknown") if queue else ("pending", "draft")
        marks = ",".join("?" for _ in states)
        rows = self.store.rows(f"""SELECT p.id,p.title,p.state,s.due,ch.name FROM candidates p JOIN slots s ON s.id=p.slot_id
            JOIN channels ch ON ch.id=s.channel_id WHERE p.state IN ({marks}) ORDER BY s.due DESC,p.id DESC LIMIT 9 OFFSET ?""",
            (*states, page*8))
        buttons = [[(f'#{p["id"]} · {p["name"][:20]} · {datetime.fromtimestamp(p["due"],TZ).strftime("%H:%M")}', f'post:{p["id"]}')] for p in rows[:8]]
        if page:
            buttons.append([("← Назад", f'list:{int(queue)}:{page-1}')])
        if len(rows) > 8:
            buttons.append([("Дальше →", f'list:{int(queue)}:{page+1}')])
        self.send("<b>"+("✅ Очередь публикаций" if queue else "📥 Предложения и черновики")+"</b>\n"
                  + ("Выбери пост. После редактирования нужно одобрить заново." if rows else "Пока пусто. Подготовка начнётся за 40 минут до выхода у включённых каналов."),
                  buttons)

    def show_post(self, pid):
        p = self.store.candidate(pid)
        if not p:
            raise ValueError("Пост не найден.")
        content = render(p)
        if p["media"]:
            try:
                self.tg.photo(self.owner, p["media"], content)
            except NetError:
                self.send(content)
                self.send("⚠️ Telegram не загрузил картинку. Перед одобрением прикрепи другую.")
                if p["state"] in ("pending", "approved", "draft"):
                    self.store.edit_candidate(pid, media="")
                    p = self.store.candidate(pid)
        else:
            self.send(content)
        lines = [f'<b>{html.escape(p["channel_name"])}</b> · #{pid}',
                 when(p["due"]), "Статус: "+html.escape(p["state"])]
        meta = p["meta"]
        if meta.get("mode") == "fashion" and meta.get("unverified"):
            lines += ["⚠️ Цены или доступность не подтверждены публичной карточкой. Сначала проверь ссылки и нажми «Товары проверены»."]
        for ref in meta.get("sources", []):
            lines.append(f'<a href="{html.escape(ref["url"], quote=True)}">{html.escape(ref["title"][:100])}</a>')
        actions = []
        if p["slot_state"] in ("pending", "queued") and p["state"] in ("pending", "approved", "draft"):
            actions += [[("✅ Одобрить на "+datetime.fromtimestamp(p["due"],TZ).strftime("%H:%M"), f"approve:{pid}")],
                        [("✍️ Изменить текст", f"text:{pid}"), ("🖼 Прикрепить картинку", f"image:{pid}")],
                        [("📂 В черновики", f"draft:{pid}"), ("❌ Отклонить", f"reject:{pid}")]]
            if meta.get("mode") == "fashion":
                actions += [[("📋 Промпт для моделей", f"prompt:{pid}")]]
                if meta.get("unverified"):
                    actions += [[("🔎 Проверил товары и цены", f"products_ok:{pid}")]]
        if p["state"] == "unknown":
            actions += [[("✅ Проверил: пост уже в канале", f"resolve_sent:{pid}")],
                        [("📂 Проверил: поста нет, оставить черновиком", f"resolve_draft:{pid}")]]
        self.send("\n".join(lines), actions)

    def handle(self, update):
        query = update.get("callback_query")
        message = query.get("message", {}) if query else update.get("message", {})
        user = query.get("from", {}).get("id") if query else message.get("from", {}).get("id")
        allowed = [self.store.get("owner", 0)] + self.store.get("coowners", [])
        if user in allowed and user:
            self._actor.user = user
        try:
            return self._handle(update)
        finally:
            self._actor.user = None

    def _handle(self, update):
        query = update.get("callback_query")
        message = query.get("message", {}) if query else update.get("message", {})
        user = query.get("from", {}).get("id") if query else message.get("from", {}).get("id")
        if message.get("chat", {}).get("type") != "private":
            return
        text = message.get("text", "").strip()
        if not self.owner:
            if not query and text.startswith("/start "):
                if self.store.pair(user, text.split(maxsplit=1)[1]):
                    self.home()
                else:
                    self.tg.send(user, "Неверный или просроченный код. Новый код выдаёт настройка на компьютере.")
            return
        if user != self.owner or message.get("chat", {}).get("id") != self.owner:
            return
        if query:
            try:
                self.tg.call("answerCallbackQuery", callback_query_id=query["id"])
            except NetError:
                pass
        try:
            if query:
                self.callback(query.get("data", ""))
            else:
                self.message(message)
        except (ValueError, KeyError, AuthError, NetError) as e:
            self.send("⚠️ "+html.escape(str(e))[:1500])

    def message(self, msg):
        text = msg.get("text", "").strip()
        menus = {
            "/start": self.home, "/menu": self.home, "/cancel": self.home,
            "📥 Предложения": self.pending, "✅ Очередь": lambda: self.pending(queue=True),
            "🗂 Каналы": self.channel_list, "🎛 Фильтры": lambda: self.channel_list(True),
            "📊 Лимиты": self.limits, "🛑 Стоп": lambda: self.callback("halt"),
            "▶️ Продолжить": lambda: self.callback("resume"),
            "🔐 Подключение ИИ": lambda: self.callback("auth"),
        }
        if text in menus:
            self.store.clear_session(self.owner)
            menus[text]()
            return
        session = self.store.session(self.owner)
        if not session:
            self.send("Выбери действие кнопкой в меню. /cancel — отменить ввод.", menu=True)
            return
        payload = json.loads(session["payload"])
        action = session["action"]
        if action == "image":
            if not msg.get("photo"):
                raise ValueError("Пришли картинку как фото одним сообщением. /cancel — отмена.")
            self.store.edit_candidate(payload["pid"], media=msg["photo"][-1]["file_id"])
            self.store.clear_session(self.owner)
            self.show_post(payload["pid"])
        elif action == "text":
            p = self.store.candidate(payload["pid"])
            if not text or len(text) > 650:
                raise ValueError("Пришли текст от 1 до 650 знаков.")
            p["body"] = text
            if utf16(render(p)) > 1000:
                raise ValueError("Слишком длинная подпись с учётом ссылок. Сократи текст.")
            self.store.edit_candidate(payload["pid"], body=text)
            self.store.clear_session(self.owner)
            self.show_post(payload["pid"])
        elif action == "add":
            if not 2 <= len(text) <= 80 or len(self.store.channels()) >= 30:
                raise ValueError("Название — от 2 до 80 знаков. Максимум 30 каналов.")
            cid = self.store.add_channel(text)
            self.store.clear_session(self.owner)
            self.send("Канал добавлен выключенным. Подключи его, задай фильтры и включи.")
            self.channel_screen(cid)
        elif action == "target":
            if not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}|-100\d{6,20}", text):
                raise ValueError("Пришли @username канала или числовой ID вида -100…")
            target = self.tg.connect_channel(text)
            self.store.update_channel(payload["cid"], target=target)
            self.store.clear_session(self.owner)
            self.channel_screen(payload["cid"])
        elif action == "rename":
            if not 2 <= len(text) <= 80:
                raise ValueError("Название — от 2 до 80 знаков.")
            self.store.update_channel(payload["cid"], name=text)
            self.store.clear_session(self.owner)
            self.channel_screen(payload["cid"])
        elif action == "field":
            ch = self.need_channel(payload["cid"])
            cfg, field = ch["config"], payload["field"]
            if field in ("topic", "style"):
                if not 5 <= len(text) <= 1000:
                    raise ValueError("Пришли от 5 до 1000 знаков.")
                cfg[field] = text
            elif field == "times":
                cfg[field] = parse_times(text)
                for t in cfg[field]:
                    if cfg["mode"] == "fashion":
                        cfg["budgets"].setdefault(t, 10000)
            elif field == "sources":
                urls = [] if text == "-" else [public_url(x) for x in text.split()]
                if len(urls) > 5:
                    raise ValueError("Для экономии максимум 5 RSS/JSON Feed на канал.")
                cfg[field] = list(dict.fromkeys(urls))
            elif field in ("include", "exclude"):
                cfg[field] = [] if text == "-" else [w.strip().lower() for w in text.split(",") if w.strip()][:20]
                if any(len(x) > 60 for x in cfg[field]):
                    raise ValueError("Каждый фильтр — до 60 знаков.")
            elif field == "max_age_hours":
                if not text.isdigit() or not 1 <= int(text) <= 720:
                    raise ValueError("Допустимо от 1 до 720 часов.")
                cfg[field] = int(text)
            elif field == "budgets":
                budgets = {}
                for part in text.split():
                    t, amount = part.split("=")
                    parse_times(t)
                    if t not in cfg["times"] or not amount.isdigit() or not 1000 <= int(amount) <= 100000:
                        raise ValueError("Укажи существующее время и бюджет от 1000 до 100000 ₽.")
                    budgets[t] = int(amount)
                if set(budgets) != set(cfg["times"]):
                    raise ValueError("Укажи бюджет для каждого времени.")
                cfg[field] = budgets
            self.store.update_channel(ch["id"], config=cfg)
            self.store.clear_session(self.owner)
            self.filters(ch["id"])
        elif action == "limit":
            field = payload["field"]
            label, low, high = LIMIT_FIELDS[field]
            if not text.isdigit() or not low <= int(text) <= high:
                raise ValueError(f"{label}: число от {low} до {high}.")
            limits = self.store.get("limits")
            limits[field] = int(text)
            if any(limits[k] < limits["request_reserve"] for k in ("daily_tokens", "rolling_tokens", "channel_daily_tokens")):
                raise ValueError("Лимит токенов должен вмещать резерв хотя бы одного запроса.")
            self.store.put("limits", limits)
            self.store.clear_session(self.owner)
            self.limits(True)
        else:
            self.store.clear_session(self.owner)
            self.home()

    def callback(self, data):
        parts = data.split(":")
        action = parts[0]
        value = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if action == "halt":
            self.store.halt("Остановлено владельцем.")
            self.send("🛑 ИИ и публикации остановлены. Меню продолжает работать.", menu=True)
        elif action == "resume":
            self.send("Продолжить подготовку и отправку уже одобренных постов по расписанию?\n"
                      "Счётчики расхода сохранятся. Пропущенные выходы задним числом не отправляются.",
                      [[("▶️ Да, продолжить", "resume_yes"), ("Оставить паузу", "home")]])
        elif action == "resume_yes":
            if not self.store.get("model") or not self.auth.saved():
                raise ValueError("Сначала выполни LOGIN.bat и выбери модель.")
            if self.store.rows("SELECT id FROM slots WHERE state='unknown'"):
                raise ValueError("Сначала проверь отправки с неизвестным результатом в «Очереди».")
            self.store.put("paused", False)
            self.store.put("pause_reason", "")
            self.engine.wake.set()
            self.home()
        elif action == "home":
            self.home()
        elif action == "channel":
            self.channel_screen(value)
        elif action == "filters":
            self.filters(value)
        elif action == "limits_edit":
            self.limits(True)
        elif action == "limit":
            key = parts[1]
            if key not in LIMIT_FIELDS:
                raise ValueError("Неизвестный лимит.")
            self.store.session(self.owner, "limit", {"field": key})
            label, low, high = LIMIT_FIELDS[key]
            self.send(f"{label}: пришли число от {low} до {high}. /cancel — отмена.")
        elif action == "field":
            key = parts[2]
            if key not in FIELDS:
                raise ValueError("Неизвестный фильтр.")
            self.store.session(self.owner, "field", {"cid": value, "field": key})
            hints = dict(times="Например: 09:00 15:00 21:00",
                         sources="Ссылки RSS/JSON Feed через пробел. Максимум 5. Знак - очищает список.",
                         include="Через запятую; подходят материалы хотя бы с одним словом. Знак - отключает фильтр.",
                         exclude="Через запятую. Знак - очищает список.",
                         budgets="Например: 15:00=10000 21:00=20000")
            self.send(FIELDS[key]+".\n"+hints.get(key, "Пришли новое значение.")+"\n/cancel — отмена.")
        elif action in ("target", "rename"):
            self.need_channel(value)
            self.store.session(self.owner, action, {"cid": value})
            self.send("Пришли @username или ID канала. Сначала добавь бота администратором." if action == "target" else "Пришли новое название.")
        elif action == "add":
            self.store.session(self.owner, "add")
            self.send("Пришли название нового канала. Затем можно настроить все фильтры.")
        elif action == "toggle":
            ch = self.need_channel(value)
            if not ch["enabled"] and not ch["target"]:
                raise ValueError("Сначала подключи Telegram-канал.")
            self.store.update_channel(value, enabled=not ch["enabled"])
            self.channel_screen(value)
        elif action == "mode":
            self.send("Какой контент готовить?", [[("📰 Новости", f"setmode:{value}:news")],
                                                [("🎬 Кино", f"setmode:{value}:cinema")],
                                                [("🧥 Образы", f"setmode:{value}:fashion")]])
        elif action == "setmode":
            mode = parts[2]
            if mode not in ("news", "cinema", "fashion"):
                raise ValueError("Неизвестный режим.")
            ch = self.need_channel(value)
            ch["config"]["mode"] = mode
            self.store.update_channel(value, config=ch["config"])
            self.filters(value)
        elif action in ("photo_required", "web"):
            ch = self.need_channel(value)
            key = "require_photo" if action == "photo_required" else "web_search"
            ch["config"][key] = not ch["config"][key]
            self.store.update_channel(value, config=ch["config"])
            self.filters(value)
        elif action == "post":
            self.show_post(value)
        elif action == "list":
            self.pending(int(parts[2]), bool(value))
        elif action == "approve":
            self.store.approve(value, time.time())
            self.send("✅ Одобрено. Пост выйдет "+when(self.store.candidate(value)["due"])+".")
        elif action in ("draft", "reject"):
            p = self.store.candidate(value)
            if not p or p["state"] not in ("pending", "approved", "draft"):
                raise ValueError("Этот вариант уже обработан.")
            with self.store.tx() as c:
                changed = c.execute("UPDATE candidates SET state=? WHERE id=? AND state IN ('pending','approved','draft')",
                                    ("draft" if action == "draft" else "rejected", value)).rowcount
                if not changed:
                    raise ValueError("Пост уже отправляется; изменение не выполнено.")
                c.execute("UPDATE slots SET state='pending',selected=NULL WHERE id=? AND selected=?", (p["slot_id"], value))
            self.send("Сохранено в черновиках." if action == "draft" else "Отклонено. Автоматической замены с новым запросом ИИ не будет.")
        elif action in ("text", "image"):
            if not self.store.candidate(value):
                raise ValueError("Пост не найден.")
            self.store.session(self.owner, action, {"pid": value})
            self.send("Пришли готовую картинку как фото. Одобрение после изменения нужно повторить." if action == "image" else
                      "Пришли новый основной текст, до 650 знаков. Заголовок и ссылки сохранятся. ИИ для этой правки не вызывается.")
        elif action == "prompt":
            p = self.store.candidate(value)
            if not p or p["meta"].get("mode") != "fashion":
                raise ValueError("Это не подборка одежды.")
            text = image_prompt(p)
            self.send("<b>Скопируй в ChatGPT и приложи фотографии вещей</b>\n\n"+html.escape(text)[:3500])
        elif action == "products_ok":
            p = self.store.candidate(value)
            if not p or p["meta"].get("mode") != "fashion" or p["state"] not in ("pending", "draft"):
                raise ValueError("Эта подборка уже обработана.")
            p["meta"]["unverified"] = False
            p["meta"]["manually_checked_at"] = int(time.time())
            self.store.execute("UPDATE candidates SET meta=? WHERE id=?", (json.dumps(p["meta"], ensure_ascii=False), value))
            self.send("Отмечено, что ты проверил соответствие вещей, цены и доступность. Теперь прикрепи картинку и одобри пост.")
        elif action in ("resolve_sent", "resolve_draft"):
            p = self.store.candidate(value)
            if not p or p["state"] != "unknown":
                raise ValueError("Нет неопределённой отправки.")
            with self.store.tx() as c:
                c.execute("UPDATE candidates SET state=? WHERE id=?", ("sent" if action == "resolve_sent" else "draft", value))
                c.execute("UPDATE slots SET state=? WHERE id=?", ("sent" if action == "resolve_sent" else "expired", p["slot_id"]))
            self.send("Результат сохранён. Автоматического повторного отправления нет.")
        elif action == "prepare":
            if self.store.get("paused", True):
                raise ValueError("Сначала сними общую паузу.")
            self.engine.create_slots(time.time())
            slots = self.store.rows("SELECT id FROM slots WHERE channel_id=? AND due>? AND due<? ORDER BY due LIMIT 1",
                                    (value, int(time.time())+180, int(time.time())+self.store.get("limits")["lead_minutes"]*60))
            if not slots:
                raise ValueError("Сейчас нет окна подготовки этого канала.")
            sid = slots[0]["id"]
            if not self.engine.lock.acquire(blocking=False):
                raise ValueError("Подготовка уже идёт. Дождись её окончания.")
            import threading
            def worker():
                try:
                    self.engine.prepare(sid, manual=True)
                except ValueError as e:
                    self.store.notice(f"manual-error:{sid}:{int(time.time())}", str(e))
                finally:
                    self.engine.lock.release()
            threading.Thread(target=worker, daemon=True).start()
            self.send("Подготовка начата. Защита и лимит попыток действуют и для ручного запуска.")
        elif action == "auth":
            saved = self.auth.saved()
            self.send("<b>🔐 Подключение ИИ</b>\n\n"
                      + ("Вход сохранён. " if saved else "Вход ещё не выполнен. ")
                      + "Модель: "+html.escape(self.store.get("model") or "не выбрана")+
                      "\nДля входа или смены модели запусти LOGIN.bat на компьютере с ботом.\n"
                      "Пароль и токены в Telegram присылать не нужно. Платный API автоматически не подключается.")
        elif action == "catalog_help":
            self.send("Каталог нужен, если WB/Ozon не отдают публичные цены или веб-поиск недоступен.\n"
                      "В папке examples есть products.csv с заголовками. Заполни реальными товарами и выполни:\n"
                      "<code>python -m smm catalog путь_к_файлу.csv</code>\n"
                      "checked_at — дата проверки ISO, category — outerwear/top/bottom/shoes/hat/bag, "
                      "winter=true. Можно также включить ограниченный веб-поиск: максимум два поиска за запрос.")
        else:
            raise ValueError("Неизвестная кнопка. Открой /menu.")

    def deliver(self):
        if not self.owner:
            return
        now = int(time.time())
        for n in self.store.rows("SELECT * FROM notices WHERE delivered=0 ORDER BY id LIMIT 10"):
            delay = min(1800, 5*(2**min(n["attempts"], 9)))
            if n["last_attempt"]+delay > now:
                continue
            try:
                self.send(html.escape(n["text"]))
                self.store.execute("UPDATE notices SET delivered=1 WHERE id=?", (n["id"],))
            except NetError:
                self.store.execute("UPDATE notices SET attempts=attempts+1,last_attempt=? WHERE id=?", (now, n["id"]))
                return
        for p in self.store.rows("""SELECT p.id FROM candidates p JOIN slots s ON s.id=p.slot_id
                                  WHERE p.notified=0 AND p.state='pending' AND s.state='pending' ORDER BY p.id LIMIT 4"""):
            try:
                self.show_post(p["id"])
                self.store.execute("UPDATE candidates SET notified=1 WHERE id=?", (p["id"],))
            except NetError:
                return
