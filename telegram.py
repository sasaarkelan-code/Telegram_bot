from __future__ import annotations

import threading
import time

from .net import NetError, api_json


def button(text, data):
    return {"text": text, "callback_data": data}


def keyboard(rows):
    return {"inline_keyboard": [[button(label, data) for label, data in row] for row in rows]}


MENU = {"keyboard": [
    [{"text": "🏠 Главное меню"}],
    [{"text": "📥 Предложения"}, {"text": "✅ Очередь"}],
    [{"text": "🗂 Каналы"}, {"text": "🎛 Фильтры"}],
    [{"text": "📊 Лимиты"}, {"text": "🔐 Подключение ИИ"}],
    [{"text": "🛑 Стоп"}, {"text": "▶️ Продолжить"}],
], "resize_keyboard": True, "is_persistent": True}


class Telegram:
    def __init__(self, token):
        self.base = "https://api.telegram.org/bot"+token+"/"
        self._rates = {}
        self._lock = threading.Lock()

    def call(self, method, **data):
        if method.startswith("send"):
            chat = str(data.get("chat_id"))
            with self._lock:
                previous = self._rates.get(chat, 0)
                delay = max(0, previous+.95-time.monotonic())
                self._rates[chat] = time.monotonic()+delay
            if delay:
                time.sleep(delay)
        body = api_json(self.base+method, data, timeout=30 if method == "getUpdates" else 20)
        if not body.get("ok"):
            raise NetError("Telegram не подтвердил действие.", body.get("error_code", 0))
        return body["result"]

    def send(self, owner, text, markup=None):
        data = dict(chat_id=owner, text=text, parse_mode="HTML", link_preview_options={"is_disabled": True})
        if markup:
            data["reply_markup"] = markup
        return self.call("sendMessage", **data)

    def photo(self, owner, media, caption):
        return self.call("sendPhoto", chat_id=owner, photo=media, caption=caption, parse_mode="HTML")

    def publish(self, target, text, media):
        return self.photo(target, media, text) if media else self.send(target, text)

    def updates(self, offset):
        return self.call("getUpdates", offset=offset, timeout=20, allowed_updates=["message", "callback_query"])

    def connect_channel(self, target):
        me = self.call("getMe")
        chat = self.call("getChat", chat_id=target)
        if chat.get("type") != "channel":
            raise ValueError("Нужен именно Telegram-канал.")
        member = self.call("getChatMember", chat_id=chat["id"], user_id=me["id"])
        if member.get("status") not in ("administrator", "creator") or (member.get("status") == "administrator" and not member.get("can_post_messages")):
            raise ValueError("Сначала добавь бота администратором канала с правом публикации.")
        return str(chat["id"])
