from __future__ import annotations

import html
import json
import secrets
import time

from .ui import UI
from .telegram import keyboard


class WorkspaceUI(UI):
    def send(self, text, rows=None, menu=False):
        mid = getattr(self._actor, "edit_message", None)
        if mid and rows and not menu:
            from .net import NetError
            try:
                return self.tg.call("editMessageText", chat_id=self.owner, message_id=mid,
                                    text=text, parse_mode="HTML", reply_markup=keyboard(rows))
            except NetError:
                pass
        return super().send(text, rows, menu)

    def handle(self, update):
        message = update.get("callback_query", {}).get("message", {})
        self._actor.edit_message = message.get("message_id") if message.get("text") else None
        try:
            return super().handle(update)
        finally:
            self._actor.edit_message = None

    def home(self):
        self.store.clear_session(self.owner)
        self.send("<b>Центр управления</b>\nВыбери рабочий раздел.", [
            [("📰 СММ", "workspace:smm"), ("📣 Менеджер", "workspace:manager")],
            [("👥 Совладельцы", "workspace:team")],
            [("📊 Расход ИИ", "limits_edit")]])

    def manager(self):
        campaigns = self.store.get("manager_campaigns", [])
        self.send("<b>📣 Менеджер рекламы</b>\nКампаний: " + str(len(campaigns)) +
                  "\nАккаунт отправителя ещё не подключён. Кампании сохраняются как черновики.", [
            [("📝 Кампании", "workspace:campaigns")],
            [("➕ Создать кампанию", "workspace:new_campaign")],
            [("🤝 Партнёры", "workspace:partners")],
            [("🔌 Подключение аккаунта", "workspace:account")],
            [("🏠 Главное меню", "workspace:home")]])

    def team(self):
        ids = [self.store.get("owner", 0)] + self.store.get("coowners", [])
        self.send("<b>Совладельцы</b>\nВсе участники имеют полный доступ к разделам и настройкам.\n" +
                  "\n".join(str(x) for x in ids), [
            [("➕ Пригласить по Telegram ID", "workspace:invite")],
            [("➖ Удалить совладельца", "workspace:remove")],
            [("🏠 Главное меню", "workspace:home")]])

    def callback(self, data):
        if not data.startswith("workspace:"):
            return super().callback(data)
        action = data.split(":", 1)[1]
        if action == "home":
            return self.home()
        if action == "smm":
            return super().home()
        if action == "manager":
            return self.manager()
        if action == "team":
            return self.team()
        if action == "campaigns":
            entries = self.store.get("manager_campaigns", [])
            rows = [[(x["name"][:45], "workspace:campaign_"+x["id"])] for x in entries]
            rows.append([("➕ Создать кампанию", "workspace:new_campaign")])
            return self.send("<b>Кампании</b>\nВыбери черновик.", rows)
        if action.startswith("campaign_"):
            cid = action[len("campaign_"):]
            item = next((x for x in self.store.get("manager_campaigns", []) if x["id"] == cid), None)
            if not item:
                raise ValueError("Кампания не найдена.")
            return self.send("<b>"+html.escape(item["name"])+"</b>\n"+html.escape(item["text"])+
                             "\n\nГруппы: "+html.escape(", ".join(item["targets"]))+
                             "\nИнтервал: 30 минут. Статус: черновик; отправка отключена.",
                             [[("🗑 Удалить", "workspace:delete_"+cid)], [("↩ Кампании", "workspace:campaigns")]])
        if action.startswith("delete_"):
            cid = action[len("delete_"):]
            self.store.put("manager_campaigns", [x for x in self.store.get("manager_campaigns", []) if x["id"] != cid])
            return self.callback("workspace:campaigns")
        if action == "account":
            return self.send("<b>Подключение аккаунта</b>\nСейчас не подключён.\nПозже на компьютере понадобятся api_id, api_hash и отдельный Telegram-аккаунт. "
                             "Не отправляй код входа и пароль в чат. Настройка: MANAGER_ACCOUNT.json.example в архиве. "
                             "Эта версия сохраняет кампании и партнёров; отправка с аккаунта пока не реализована.")
        if action == "partners":
            items = self.store.get("manager_partners", [])
            return self.send("<b>Партнёры</b>\n"+html.escape("\n".join(items) or "Список пуст.")[:3000],
                             [[("➕ Добавить контакт", "workspace:add_partner")]])
        prompts = {
            "invite": "Пришли числовой Telegram ID будущего совладельца. Он должен сам принять приглашение в этом боте.",
            "remove": "Пришли Telegram ID совладельца для удаления. Основная привязка владельца сохраняется.",
            "new_campaign": "Пришли кампанию тремя частями через |: название | текст рекламы | @группа1, @группа2. Добавляй только группы, где разрешена твоя реклама.",
            "add_partner": "Пришли контакт партнёра и заметку одной строкой."}
        if action in prompts:
            self.store.session(self.owner, "workspace", {"action": action})
            return self.send(prompts[action]+"\n/cancel — отменить.")
        raise ValueError("Неизвестное действие.")

    def _handle(self, update):
        message = update.get("message", {})
        text = message.get("text", "").strip()
        user = message.get("from", {}).get("id")
        if message.get("chat", {}).get("type") == "private" and message.get("chat", {}).get("id") == user and text.startswith("/join "):
            invite = self.store.get("team_invite", {})
            if invite.get("user") == user and invite.get("expires", 0) > time.time() and secrets.compare_digest(invite.get("code", ""), text.split(maxsplit=1)[1]):
                users = self.store.get("coowners", [])
                if user not in users and user != self.store.get("owner", 0):
                    users.append(user)
                    self.store.put("coowners", users)
                self.store.put("team_invite", {})
                self._actor.user = user
                return self.home()
            self.tg.send(user, "Приглашение неверно или истекло.")
            return
        return super()._handle(update)

    def message(self, msg):
        text = msg.get("text", "").strip()
        if text in ("🏠 Главное меню", "/start", "/menu", "/cancel"):
            return self.home()
        session = self.store.session(self.owner)
        if not session or session["action"] != "workspace":
            return super().message(msg)
        action = json.loads(session["payload"])["action"]
        if action == "invite":
            if not text.isdigit() or int(text) <= 0:
                raise ValueError("Нужен числовой Telegram ID.")
            code = secrets.token_urlsafe(18)
            self.store.put("team_invite", {"user": int(text), "code": code, "expires": time.time()+1800})
            self.send("Передай этому человеку команду для нашего бота (действует 30 минут):\n<code>/join "+code+"</code>")
        elif action == "remove":
            if not text.isdigit():
                raise ValueError("Нужен числовой Telegram ID.")
            self.store.put("coowners", [x for x in self.store.get("coowners", []) if x != int(text)])
            self.send("Доступ совладельца удалён.")
        elif action == "new_campaign":
            parts = [x.strip() for x in text.split("|", 2)]
            if len(parts) != 3 or not all(parts):
                raise ValueError("Формат: название | текст | @группа1, @группа2")
            entries = self.store.get("manager_campaigns", [])
            if len(entries) >= 50:
                raise ValueError("Максимум 50 кампаний.")
            entries.append({"id": secrets.token_hex(5), "name": parts[0][:100], "text": parts[1][:3500],
                            "targets": [x.strip() for x in parts[2].split(",") if x.strip()][:100], "interval_minutes": 30, "state": "draft"})
            self.store.put("manager_campaigns", entries)
            self.send("Кампания сохранена. Отправка появится после реализации подключения аккаунта.")
        elif action == "add_partner":
            items = self.store.get("manager_partners", [])
            if len(items) >= 100:
                raise ValueError("Максимум 100 контактов.")
            items.append(text[:500])
            self.store.put("manager_partners", items)
            self.send("Контакт сохранён.")
        self.store.clear_session(self.owner)
