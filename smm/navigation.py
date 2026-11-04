"""Per-user screen navigation; never replay an action when returning back."""
from __future__ import annotations

from html import escape as html_escape
from .telegram import keyboard
from .net import NetError
from .manager_ui import ManagerUI

ROOT = [["📰 СММ", "📣 Менеджер"], ["👥 Совладельцы", "🌐 Веб-панель"]]
SMM = [["📥 Предложения", "✅ Очередь"], ["🗂 Каналы", "🎛 Фильтры"],
       ["🔎 Поиск постов"],
       ["📊 Лимиты", "🔐 Подключение ИИ"], ["🛑 Стоп", "▶️ Продолжить"],
       ["⬅️ Назад", "🏠 Главное меню"]]
MANAGER = [["📝 Кампании", "➕ Создать кампанию"], ["🤝 Партнёры", "🔌 Подключение аккаунта"],
           ["🛑 Остановить всё"],
           ["⬅️ Назад", "🏠 Главное меню"]]
TEAM = [["➕ Пригласить", "➖ Удалить совладельца"], ["⬅️ Назад", "🏠 Главное меню"]]


class PanelUI(ManagerUI):
    def web_login_message(self, msg):
        text=msg.get('text','').strip()
        if text.split('@',1)[0] == '/web' or text == '🌐 Веб-панель':
            import os
            from .webapp import issue_code
            code=issue_code(self.store,self.owner,msg.get('from',{}).get('username',''))
            url=os.environ.get('SMM_WEB_URL','').strip()
            self.send('🌐 <b>Вход в веб-панель</b>\n'+
                (html_escape(url)+'\n' if url else 'Открой адрес сайта из панели хостинга.\n')+
                'Логин: <code>'+html_escape(self.store.get('web_username:'+str(self.owner),'') or str(self.owner))+'</code>\nОдноразовый код на 5 минут:\n<code>'+code+'</code>\nКод даёт полный доступ. Не передавай его другим.')
            return
        return super().message(msg)

    def nav(self):
        return self.store.get("navigation:"+str(self.owner), {"section": "root", "stack": ["root"]})

    def visit(self, route, section=None, reset=False):
        # Background deliveries must not navigate a user's panel or replace its keyboard.
        if getattr(self._actor, "background", False):
            return
        state = self.nav()
        stack = state["stack"]
        if section and section != state["section"]:
            stack = ["root"] if section == "root" else ["root", section]
        if reset:
            stack = ["root"] if route == "root" else ["root", route]
        elif stack[-1] != route:
            # Return to an existing screen instead of making navigation cycles.
            stack = stack[:stack.index(route)+1] if route in stack else (stack+[route])[-25:]
        self.store.put("navigation:"+str(self.owner), {"section": section or state["section"], "stack": stack})

    def reply_keyboard(self):
        rows = {"root": ROOT, "smm": SMM, "manager": MANAGER, "team": TEAM}[self.nav()["section"]]
        return {"keyboard": [[{"text": label} for label in row] for row in rows],
                "resize_keyboard": True, "is_persistent": True}

    def send(self, text, rows=None, menu=False):
        if getattr(self._actor, "background", False):
            # Inline-only notification: it cannot alter any active reply keyboard.
            actions = list(rows or [])+[[('⬅️ К своему разделу', 'nav:current'), ('🏠 Главное меню', 'nav:home')]]
            return self.tg.send(self.owner, text, keyboard(actions))
        state = self.nav()
        section = state["section"]
        key = "keyboard_section:"+str(self.owner)
        if menu:
            result = self.tg.send(self.owner, text, self.reply_keyboard())
            self.store.put(key, section)
            return result
        if self.store.get(key) != section:
            self.tg.send(self.owner, {"root": "🏠 Центр управления", "smm": "📰 СММ", "manager": "📣 Менеджер", "team": "👥 Совладельцы"}[section], self.reply_keyboard())
            self.store.put(key, section)
            self._actor.edit_message = None
        rows = [list(row) for row in (rows or [])]
        # Every screen, empty state, form and error has the same navigation footer.
        rows = [[b for b in row if b[1] not in ('workspace:home', 'nav:home', 'nav:back', 'nav:cancel')] for row in rows]
        rows = [row for row in rows if row]
        if self.store.session(self.owner):
            rows.append([('✖️ Отменить ввод', 'nav:cancel')])
        if len(state['stack']) > 1:
            rows.append([('⬅️ Назад', 'nav:back'), ('🏠 Главное меню', 'nav:home')])
        else:
            rows.append([('🏠 Главное меню', 'nav:home')])
        mid = getattr(self._actor, 'edit_message', None)
        if mid:
            try:
                return self.tg.call('editMessageText', chat_id=self.owner, message_id=mid,
                                    text=text, parse_mode='HTML', reply_markup=keyboard(rows))
            except NetError:
                pass
        return self.tg.send(self.owner, text, keyboard(rows))

    def home(self):
        if getattr(self._actor, 'smm_home', False):
            return self.smm()
        self.store.clear_session(self.owner)
        self.visit('root', 'root', reset=True)
        self.send('<b>🏠 Центр управления</b>\n\n📰 СММ — контент и публикации\n📣 Менеджер — рекламные кампании и партнёры\n👥 Совладельцы — доступ к управлению', menu=True)

    def smm(self):
        self.visit('smm', 'smm', reset=True)
        paused = self.store.get('paused', True)
        self.send('<b>📰 СММ</b>\n'+('⏸ Подготовка и публикации на паузе.' if paused else '🟢 Работает по расписанию.')+
                  '\nДо двух предложений три раза в день. Время каждому посту назначаешь ты. Поиск можно запустить для канала в любой момент. Все часы — московские.', menu=True)

    def manager(self):
        return super().manager()

    def team(self):
        self.visit('team', 'team', reset=True)
        return super().team()

    def channel_list(self, filters=False):
        self.visit('filter_list' if filters else 'channels', 'smm')
        return super().channel_list(filters)

    def search_screen(self):
        self.visit('searches','smm')
        return super().search_screen()

    def channel_screen(self, cid):
        self.visit('channel:'+str(cid), 'smm')
        return super().channel_screen(cid)

    def filters(self, cid):
        self.visit('filters:'+str(cid), 'smm')
        return super().filters(cid)

    def pending(self, page=0, queue=False):
        self.visit(f'pending:{page}:{int(queue)}', 'smm')
        return super().pending(page, queue)

    def show_post(self, pid):
        self.visit('post:'+str(pid), 'smm')
        # A post preview sends multiple messages. Keep its caption separate from controls.
        old = getattr(self._actor, 'edit_message', None)
        self._actor.edit_message = None
        try:
            return super().show_post(pid)
        finally:
            self._actor.edit_message = old

    def limits(self, edit=False):
        self.visit('limits:'+str(int(edit)), 'smm')
        return super().limits(edit)

    def campaign_step(self, payload):
        self.store.session(self.owner, 'campaign_wizard', payload)
        prompts = ['<b>Кампания · 1 из 3</b>\nПришли название (до 100 знаков).',
                   '<b>Кампания · 2 из 3</b>\nПришли текст рекламы (до 3500 знаков).',
                   '<b>Кампания · 3 из 3</b>\nПришли группы через запятую или с новой строки: @group_one, https://t.me/group_two.\nДобавляй группы, где разрешена твоя реклама.']
        self.send(prompts[payload['step']])

    def cancel(self):
        self.store.clear_session(self.owner)
        return self.back()

    def back(self):
        session = self.store.session(self.owner)
        if session and session['action'] == 'campaign_wizard':
            import json
            payload = json.loads(session['payload'])
            if payload['step'] > 0:
                payload['step'] -= 1
                return self.campaign_step(payload)
        self.store.clear_session(self.owner)
        state = self.nav()
        if len(state['stack']) > 1:
            state['stack'].pop()
        route = state['stack'][-1]
        self.store.put('navigation:'+str(self.owner), state)
        return self.render_route(route)

    def render_route(self, route):
        if route in ('root', 'smm', 'manager', 'team'):
            return {'root': self.home, 'smm': self.smm, 'manager': self.manager, 'team': self.team}[route]()
        if route == 'channels': return self.channel_list()
        if route == 'filter_list': return self.channel_list(True)
        if route=='searches': return self.search_screen()
        parts = route.split(':')
        if parts[0] == 'channel': return self.channel_screen(int(parts[1]))
        if parts[0] == 'filters': return self.filters(int(parts[1]))
        if parts[0] == 'pending': return self.pending(int(parts[1]), bool(int(parts[2])))
        if parts[0] == 'post': return self.show_post(int(parts[1]))
        if parts[0] == 'limits': return self.limits(bool(int(parts[1])))
        return self.callback(route)

    def callback(self, data):
        if data == 'nav:current':
            self.store.clear_session(self.owner)
            route = self.nav()['stack'][-1]
            return self.back() if route == 'form' else self.render_route(route)
        if data in ('nav:home', 'workspace:home'): return self.home()
        if data == 'nav:back': return self.back()
        if data == 'nav:cancel': return self.cancel()
        # Clicking a different action always cancels unfinished input, including old buttons.
        self.store.clear_session(self.owner)
        if data == 'workspace:smm': return self.smm()
        if data == 'home': return self.smm()
        if data == 'workspace:manager': return self.manager()
        if data == 'workspace:team': return self.team()
        action = data.split(':')[0]
        if action == 'mgr':
            item=data.split(':')[1]
            # Only screens enter history: Back must never replay Start/Stop/Search.
            if item in ('edit','partner_edit'):
                self.visit('mgr:form','manager')
            elif item in ('preview','targets','timing','log','partners','partner_filters','choose_seed','contacts'):
                self.visit(data,'manager')
            elif item in ('start','stop','resolve'):
                if item in ('start','stop'): self.visit('workspace:campaign_'+data.split(':')[2],'manager')
            else:
                self.visit(self.nav()['stack'][-1] if self.nav()['section']=='manager' else 'manager','manager')
            return super().callback(data)
        if not data.startswith('workspace:') and self.nav()['section'] != 'smm':
            self.visit('smm', 'smm', reset=True)
        if action in ('approve','draft','reject','products_ok','resolve_sent','resolve_draft','at','unschedule'):
            # An action on a notification belongs to the corresponding SMM post.
            self.visit('post:'+data.split(':')[1], 'smm')
        if data.startswith('workspace:'):
            item = data.split(':', 1)[1]
            self.visit(data, 'team' if item in ('invite','remove') else 'manager')
        elif action in ('field','limit','target','rename','add','text','image','schedule'):
            self.visit('form', 'smm')
        elif action in ('auth','catalog_help','mode','prompt','resume'):
            self.visit(data, 'smm')
        elif action in ('halt','resume_yes'):
            self.visit('smm', 'smm')
        if data == 'workspace:new_campaign':
            return self.campaign_step({'step': 0})
        # Require an explicit confirmation before deleting a saved campaign.
        if data.startswith('workspace:delete_'):
            cid = data.split('delete_',1)[1]
            return self.send('Удалить кампанию? Это действие нельзя отменить.',
                             [[('🗑 Да, удалить', 'workspace:confirm_delete_'+cid)]])
        if data.startswith('workspace:confirm_delete_'):
            # Confirmation itself must never become a destination of Back.
            state = self.nav()
            state['stack'] = [r for r in state['stack'] if not r.startswith(('workspace:delete_', 'workspace:confirm_delete_', 'workspace:campaign_'))]
            self.store.put('navigation:'+str(self.owner), state)
            result=super().callback('workspace:delete_'+data.split('confirm_delete_',1)[1])
            self.visit('workspace:campaigns','manager')
            return result
        if action == 'resume_yes':
            self._actor.smm_home = True
            try:
                return super().callback(data)
            finally:
                self._actor.smm_home = False
        return super().callback(data)

    def message(self, msg):
        text = msg.get('text','').strip()
        if text.split('@',1)[0] == '/web' or text == '🌐 Веб-панель':
            return self.web_login_message(msg)
        routes = {'🏠 Главное меню': self.home, '/start': self.home, '/menu': self.home,
                  '⬅️ Назад': self.back, '/back': self.back, '/cancel': self.cancel,
                  '📰 СММ': self.smm, '📣 Менеджер': self.manager, '👥 Совладельцы': self.team}
        callbacks = {'📝 Кампании':'workspace:campaigns', '➕ Создать кампанию':'workspace:new_campaign',
                     '🤝 Партнёры':'workspace:partners', '🔌 Подключение аккаунта':'workspace:account',
                     '🛑 Остановить всё':'mgr:stop_all',
                     '➕ Пригласить':'workspace:invite', '➖ Удалить совладельца':'workspace:remove',
                     '🔎 Поиск постов':'searches'}
        if text in routes:
            self.store.clear_session(self.owner)
            return routes[text]()
        if text in callbacks: return self.callback(callbacks[text])
        if text == '🔐 Подключение ИИ': return self.callback('auth')
        if text in ('📥 Предложения','✅ Очередь','🗂 Каналы','🎛 Фильтры','📊 Лимиты','🛑 Стоп','▶️ Продолжить'):
            self.store.clear_session(self.owner)
            self.visit('smm','smm',reset=True)
        session = self.store.session(self.owner)
        if not session and text not in ('📥 Предложения','✅ Очередь','🗂 Каналы','🎛 Фильтры','📊 Лимиты','🛑 Стоп','▶️ Продолжить'):
            return self.send('Выбери действие в меню. Для возврата доступны «Назад» и «Главное меню».')
        if session and session['action'] == 'campaign_wizard':
            import json
            import re
            import secrets
            payload = json.loads(session['payload'])
            step = payload['step']
            if step in (0, 1):
                maximum = 100 if step == 0 else 3500
                if not text or len(text) > maximum:
                    raise ValueError(f'Пришли текст от 1 до {maximum} знаков.')
                payload['name' if step == 0 else 'text'] = text
                payload['step'] += 1
                return self.campaign_step(payload)
            from .manager import parse_targets
            targets=parse_targets(text)
            self.manager_service.create(payload['name'],payload['text'],targets)
            self.store.clear_session(self.owner)
            state = self.nav()
            state['stack'] = [r for r in state['stack'] if r != 'workspace:new_campaign']
            self.store.put('navigation:'+str(self.owner), state)
            self.send('✅ Кампания сохранена. Проверь пост, группы и интервалы. Кнопка «Старт» запустит проверку и рассылку после подключения аккаунта.')
            return self.callback('workspace:campaigns')
        result = super().message(msg)
        if session and not self.store.session(self.owner):
            # After saving a form, remove its route and offer a concrete next step.
            state = self.nav()
            state['stack'] = [r for r in state['stack'] if r != 'form' and r not in
                              ('workspace:invite','workspace:remove','workspace:new_campaign','workspace:add_partner')]
            self.store.put('navigation:'+str(self.owner),state)
            if session['action'] == 'workspace':
                parent = {'invite':'workspace:team','remove':'workspace:team',
                          'new_campaign':'workspace:campaigns','add_partner':'workspace:partners'}
                import json
                return self.callback(parent[json.loads(session['payload'])['action']])
        return result

    def deliver(self):
        self._actor.background = True
        try:
            return super().deliver()
        finally:
            self._actor.background = False
