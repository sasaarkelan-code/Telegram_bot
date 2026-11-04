"""Telegram is an access key, never a second control panel or notification sink."""
from __future__ import annotations

import html
import os
import secrets
import time
from urllib.parse import urlsplit

from .net import NetError
from .webapp import issue_code

ACCESS_BUTTON = '🔑 Вход на сайт'
ACCESS_KEYBOARD = {'keyboard': [[{'text': ACCESS_BUTTON}]],
                   'resize_keyboard': True, 'is_persistent': True}


class WebAccess:
    def __init__(self, store, telegram, env=None):
        self.store, self.tg = store, telegram
        self.env = os.environ if env is None else env

    def configure(self):
        self.tg.call('setMyCommands', commands=[
            {'command': 'web', 'description': 'Сайт и одноразовый код входа'}])

    def portal(self, actor, text='Управление и уведомления теперь на сайте.'):
        url = self.env.get('SMM_WEB_URL', '').strip().rstrip('/')
        parsed = urlsplit(url)
        if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password:
            text += '\n<a href="'+html.escape(url, quote=True)+'">Открыть веб-панель</a>'
        else:
            text += '\nОткрой адрес сайта из панели хостинга.'
        text += '\nНажми «🔑 Вход на сайт» или отправь /web для получения кода.'
        return self.tg.send(actor, text, ACCESS_KEYBOARD)

    def accept_invite(self, actor, code):
        with self.store.tx() as c:
            invite = self.store.get('team_invite', {}, c)
            if (invite.get('user') != actor or invite.get('expires', 0) <= time.time()
                    or not secrets.compare_digest(str(invite.get('code', '')), code)):
                return False
            users = self.store.get('coowners', [], c)
            if actor != self.store.get('owner', 0, c) and actor not in users:
                self.store.put('coowners', users+[actor], c)
            self.store.put('team_invite', {}, c)
            return True

    def handle(self, update):
        query = update.get('callback_query')
        message = query.get('message', {}) if query else update.get('message', {})
        sender = query.get('from', {}) if query else message.get('from', {})
        actor = sender.get('id')
        chat = message.get('chat', {})
        if type(actor) is not int or actor <= 0 or chat.get('type') != 'private' or chat.get('id') != actor:
            return
        text = message.get('text', '').strip() if not query else ''
        parts = text.split(maxsplit=1)
        command = parts[0].split('@',1)[0] if parts else ''
        if not query and command == '/join' and len(parts) == 2:
            if self.accept_invite(actor, parts[1]):
                return self.portal(actor, 'Приглашение совладельца принято.')
            return self.tg.send(actor, 'Приглашение неверно или истекло.')
        if not self.store.get('owner', 0) and not query and command == '/start' and len(parts) == 2:
            if self.store.pair(actor, parts[1]):
                return self.portal(actor, 'Владелец привязан.')
            return
        if actor not in [self.store.get('owner', 0)] + self.store.get('coowners', []):
            if query:
                self.tg.call('answerCallbackQuery', callback_query_id=query['id'], text='Нет доступа.')
            return
        if query:
            # Historical admin buttons are inert, even if their messages remain.
            self.tg.call('answerCallbackQuery', callback_query_id=query['id'],
                         text='Управление перенесено на сайт.')
            return self.portal(actor)
        self.store.clear_session(actor)
        if command == '/web' or text in (ACCESS_BUTTON, '🌐 Веб-панель'):
            code = issue_code(self.store, actor, sender.get('username', ''))
            username = self.store.get('web_username:'+str(actor), '') or str(actor)
            return self.portal(actor,
                '<b>🔑 Вход на сайт</b>\nЛогин: <code>'+html.escape(username)+
                '</code>\nОдноразовый код на 5 минут:\n<code>'+code+
                '</code>\nКод действует один раз. Не передавай его другим.')
        return self.portal(actor)
