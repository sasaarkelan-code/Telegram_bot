"""Manager screens: campaigns and partner discovery, isolated from SMM."""
from __future__ import annotations

import html
import json
import time

from .manager import Manager, parse_targets
from .ui import when
from .workspace_ui import WorkspaceUI

STATES={'draft':'черновик','checking':'проверяю группы','active':'работает','stopped':'остановлена'}


class ManagerUI(WorkspaceUI):
    @property
    def manager_service(self):
        if not hasattr(self,'_manager_service'): self._manager_service=Manager(self.store)
        return self._manager_service

    def manager(self):
        self.visit('manager','manager',reset=True)
        campaigns=self.manager_service.entries()
        account=self.store.get('manager_account',{})
        self.send('<b>📣 Менеджер</b>\n\n'+
                  ('🟢 Аккаунт подключён' if account.get('connected') else '⚪ Аккаунт ещё не подключён')+
                  f'\nКампаний: {len(campaigns)} · работают: {sum(x["state"]=="active" for x in campaigns)}\n\n'
                  'Рассылка — только по твоему списку групп. Стандарт: 2 минуты между группами, новый круг через 30 минут.\n'
                  'Партнёры — поиск каналов и ссылки для твоего просмотра. Переписку ведёшь ты.',menu=True)

    def campaigns_screen(self):
        entries=self.manager_service.entries()
        self.send('<b>📝 Кампании</b>\nВыбери рассылку или создай новую.',
                  [[(f'{"🟢" if x["state"]=="active" else "⚪"} {x["name"][:35]}', 'workspace:campaign_'+x['id'])] for x in entries]+
                  [[('➕ Создать кампанию','workspace:new_campaign')],[('🛑 Остановить всё','mgr:stop_all')]])

    def campaign_screen(self,cid):
        item=self.manager_service.campaign(cid)
        count=len(item['resolved']) or len(item['targets'])
        cycle=max(item['cycle_minutes'],count*item['gap_minutes'])
        next_at=when(item['next_at']) if item['state']=='active' and item['next_at'] else 'после запуска и проверки групп'
        self.send('<b>'+html.escape(item['name'])+'</b>\n\n'+
                  f'Статус: {STATES.get(item["state"],item["state"])}\n'
                  f'Групп: {count} · пауза: {item["gap_minutes"]} мин.\n'
                  f'Круг: каждые {cycle} мин. · предел: {item["daily_limit"]} отправок/день\n'
                  f'Следующая отправка: {next_at}\n'
                  f'Позиция: {item["cursor"]+1}/{max(1,count)} · версия поста: {item["version"]}\n'+
                  ('\nПричина остановки: '+html.escape(item['reason'])+'\n' if item['reason'] else '')+
                  '\nТолько группы, в которых разрешена твоя реклама. При первом старте проверю членство и возможность писать.',
                  [[('▶️ Старт','mgr:start:'+cid),('⏹ Стоп','mgr:stop:'+cid)],
                   [('📝 Изменить пост','mgr:edit:text:'+cid),('👁 Посмотреть пост','mgr:preview:'+cid)],
                   [('👥 Группы','mgr:targets:'+cid),('⏱ Интервалы и предел','mgr:timing:'+cid)],
                   [('📋 Журнал','mgr:log:'+cid+':0'),('🗑 Удалить','workspace:delete_'+cid)]])

    def account_screen(self):
        account=self.store.get('manager_account',{})
        cooldown=self.store.get('manager_cooldown',0)
        self.send('<b>🔌 Аккаунт отправителя</b>\n\n'+
                  ('🟢 Подключён: '+html.escape(account.get('name','Аккаунт')) if account.get('connected') else '⚪ Не подключён')+
                  '\n'+html.escape(account.get('note','Настройка ещё не выполнена.'))+
                  ('\nПауза Telegram до '+when(cooldown) if cooldown>time.time() else '')+
                  '\n\nДля подключения нужны MANAGER_ENABLED=1, MANAGER_API_ID и MANAGER_API_HASH в ENV сервера. '
                  'Затем останови бота и выполни <code>python main.py manager-login</code> в защищённом интерактивном терминале. '
                  'После входа запусти бота. Код и пароль вводятся в терминале; в этот чат их присылать не нужно.\n\n'
                  'Без подключения кампании можно готовить, но отправка и поиск не запускаются.',
                  [[('🔄 Проверить подключение','mgr:account_check')]])

    def partner_screen(self,page=0,favorites=False):
        criteria=self.store.get('manager_partner_filter',{})
        job=self.store.get('manager_search',{})
        rows=self.store.rows('SELECT * FROM manager_channels WHERE hidden=0'+(' AND favorite=1' if favorites else '')+' ORDER BY found DESC,peer LIMIT 6 OFFSET ?',(page*5,))
        lines=['<b>🤝 Партнёры</b>', 'Тема: '+html.escape(', '.join(criteria.get('queries',[])) or 'не задана'),
               f'Подписчики: {criteria.get("min",0)} — {criteria.get("max",0) or "без верхней границы"}',
               'Поиск: '+{'queued':'в очереди','running':'идёт','done':'завершён','failed':'прерван','cancelled':'отменён'}.get(job.get('state'),'не запускался')]
        if job.get('error'): lines.append(html.escape(job['error']))
        buttons=[]
        for row in rows[:5]:
            subscribers=str(row['subscribers']) if row['subscribers'] is not None else 'неизвестно'
            lines+=['',f'<a href="{html.escape(row["url"],quote=True)}">{html.escape(row["title"])}</a> · {subscribers} подписчиков',
                    html.escape(row['about'][:120]),html.escape(row['origin']),
                    'Последний пост: '+(when(row['last_post']) if row['last_post'] else 'неизвестно')]
            buttons.append([(f'{"★" if row["favorite"] else "☆"} {row["title"][:18]}','mgr:favorite:'+row['peer']),('Скрыть','mgr:hide:'+row['peer'])])
        if not rows: lines+=['','Здесь будут до 10 новых каналов за поиск. Подходят ли они для сотрудничества — решаешь ты.']
        if page: buttons.append([('← Предыдущие','mgr:partners:'+str(page-1)+':'+str(int(favorites)))])
        if len(rows)>5: buttons.append([('Следующие →','mgr:partners:'+str(page+1)+':'+str(int(favorites)))])
        buttons += [[('🔎 Найти ещё до 10','mgr:search'),('⚙️ Фильтры','mgr:partner_filters')],
                    [('★ Избранное','mgr:partners:0:1'),('Все находки','mgr:partners:0:0')]]
        buttons.append([('📒 Контакты и заметки','mgr:contacts:0'),('➕ Добавить контакт','workspace:add_partner')])
        self.send('\n'.join(lines),buttons)

    def manager_callback(self,data):
        parts=data.split(':')
        action=parts[1]
        cid=parts[2] if len(parts)>2 else ''
        service=self.manager_service
        if action in ('start','stop'):
            service.start(cid) if action=='start' else service.stop(cid)
            return self.campaign_screen(cid)
        if action=='stop_all':
            service.stop()
            self.send('⏹ Все рассылки остановлены, поиск отменён. Уже начатая отправка может завершиться; следующих отправок не будет.')
            return self.manager()
        if action=='preview':
            item=service.campaign(cid)
            return self.send('<b>Предпросмотр поста · версия '+str(item['version'])+'</b>\n\n'+html.escape(item['text']),[[('📝 Изменить','mgr:edit:text:'+cid)]])
        if action=='targets':
            item=service.campaign(cid)
            return self.send('<b>👥 Группы рассылки</b>\n'+html.escape('\n'.join(item['targets']))+
                             '\n\nДубликаты удаляются. Аккаунт должен уже состоять в группах; автоматического вступления нет.',
                             [[('✍️ Заменить список','mgr:edit:targets:'+cid)]])
        if action=='timing':
            item=service.campaign(cid)
            return self.send(f'<b>⏱ Расписание</b>\nМежду группами: {item["gap_minutes"]} мин.\nКруг: от {item["cycle_minutes"]} мин.\nПредел: {item["daily_limit"]} отправок за день.\n\n'
                             'Если все группы не помещаются в круг, он удлиняется. Пропущенные отправки не догоняются пачкой.',
                             [[('Пауза между группами','mgr:edit:gap_minutes:'+cid)],
                              [('Период круга','mgr:edit:cycle_minutes:'+cid)],[('Предел отправок за день','mgr:edit:daily_limit:'+cid)]])
        if action=='edit':
            field,cid=parts[2:4]
            service.campaign(cid)
            hints={'text':'Пришли новый общий текст до 3500 знаков. Он применяется к ещё не начатым отправкам; уже отправленные сообщения не редактируются.',
                   'targets':'Пришли группы через пробел, запятую или с новой строки: @username, https://t.me/group или ID. Замена остановит рассылку; затем нажми Старт.',
                   'gap_minutes':'Пауза между группами, от 2 до 60 минут. Изменение остановит рассылку.',
                   'cycle_minutes':'Период круга, от 30 до 1440 минут. Изменение остановит рассылку.',
                   'daily_limit':'Предел отправок за день на аккаунт, от 1 до 1000. Изменение остановит рассылку.'}
            if field not in hints: raise ValueError('Неизвестная настройка.')
            self.store.session(self.owner,'manager_form',dict(field=field,cid=cid))
            return self.send(hints[field])
        if action=='account_check':
            self.store.put('manager_check_account',True)
            return self.send('Проверка подключения поставлена в очередь. Через несколько секунд открой «Подключение аккаунта».')
        if action=='log':
            page=int(parts[3])
            rows=self.store.rows('SELECT * FROM manager_deliveries WHERE campaign=? ORDER BY id DESC LIMIT 9 OFFSET ?',(cid,page*8))
            statuses={'done':'✅ отправлено','unknown':'❓ проверь группу','blocked':'⛔ не отправлено','sending':'⏳ отправляется','cancelled':'⏹ отменено','skipped':'⏭ пропущено'}
            lines=['<b>📋 Журнал рассылки</b>']
            buttons=[]
            for row in rows[:8]:
                lines+=['',f'#{row["id"]} · {when(row["started"])} · {html.escape(row["target"])}',
                        statuses[row['state']]+f' · пост v{row["version"]}']
                if row['error']: lines.append(html.escape(row['error']))
                if row['state']=='unknown':
                    buttons.append([('✅ Уже в группе','mgr:resolve:'+str(row['id'])+':1'),('⏭ Пропустить','mgr:resolve:'+str(row['id'])+':0')])
            if not rows: lines.append('Отправок ещё не было.')
            if page: buttons.append([('← Предыдущие','mgr:log:'+cid+':'+str(page-1))])
            if len(rows)>8: buttons.append([('Следующие →','mgr:log:'+cid+':'+str(page+1))])
            return self.send('\n'.join(lines),buttons)
        if action=='resolve':
            row=self.store.rows('SELECT campaign FROM manager_deliveries WHERE id=?',(int(cid),))
            service.resolve(int(cid),parts[3]=='1')
            return self.campaign_screen(row[0]['campaign'])
        if action=='partners': return self.partner_screen(int(cid),parts[3]=='1')
        if action=='contacts':
            page=int(cid)
            items=self.store.get('manager_partners',[])
            buttons=[]
            if page: buttons.append([('← Предыдущие','mgr:contacts:'+str(page-1))])
            if len(items)>page*5+5: buttons.append([('Следующие →','mgr:contacts:'+str(page+1))])
            return self.send('<b>📒 Контакты и заметки</b>\n'+html.escape('\n\n'.join(items[page*5:page*5+5]) or 'Пока пусто.'),buttons)
        if action in ('favorite','hide'):
            if action=='hide': self.store.execute('UPDATE manager_channels SET hidden=1 WHERE peer=?',(cid,))
            else: self.store.execute('UPDATE manager_channels SET favorite=1-favorite WHERE peer=?',(cid,))
            return self.partner_screen()
        if action=='search':
            service.queue_search()
            return self.partner_screen()
        if action=='partner_filters':
            f=self.store.get('manager_partner_filter',{})
            return self.send('<b>⚙️ Поиск партнёров</b>\n'+html.escape(', '.join(f.get('queries',[])) or 'Укажи тему поиска.')+
                             '\nНаш канал: '+html.escape(f.get('seed') or 'не выбран')+
                             '\nРежим: '+('упоминания сотрудничества в описании' if f.get('kind')=='vp' else 'похожие каналы')+
                             '\n\nИщу публичные каналы через поиск Telegram и рекомендации. Это не полный список всех каналов. Готовность к ВП не гарантируется.',
                             [[('Темы поиска','mgr:partner_edit:queries'),('Число подписчиков','mgr:partner_edit:range')],
                              [('Выбрать наш канал','mgr:choose_seed')],[('Похожие каналы','mgr:kind:any'),('Есть отметка о ВП','mgr:kind:vp')]])
        if action=='partner_edit':
            if cid not in ('queries','range'): raise ValueError('Неизвестный фильтр.')
            self.store.session(self.owner,'manager_partner_form',dict(field=cid))
            return self.send('Пришли до трёх поисковых фраз через запятую, например: нейросети, ai tools, технологии.' if cid=='queries' else 'Пришли минимум и максимум подписчиков через пробел. Например: 1000 50000. 0 0 — без ограничения.')
        if action=='choose_seed':
            return self.send('Выбери свой канал для рекомендаций. Сам канал и связанные с ним аккаунты не получают сообщений.',
                             [[(ch['name'][:35],'mgr:seed:'+str(ch['id']))] for ch in self.store.channels() if ch['target']])
        if action=='seed':
            ch=self.store.channel(int(cid))
            if not ch or not ch['target']: raise ValueError('Сначала подключи свой канал в разделе СММ.')
            f=self.store.get('manager_partner_filter',{})
            f['seed']=ch['target']
            self.store.put('manager_partner_filter',f)
            return self.manager_callback('mgr:partner_filters')
        if action=='kind':
            if cid not in ('any','vp'): raise ValueError('Неизвестный режим.')
            f=self.store.get('manager_partner_filter',{})
            f['kind']=cid
            self.store.put('manager_partner_filter',f)
            return self.manager_callback('mgr:partner_filters')
        raise ValueError('Неизвестное действие менеджера.')

    def callback(self,data):
        if data.startswith('mgr:'): return self.manager_callback(data)
        if data=='workspace:campaigns': return self.campaigns_screen()
        if data.startswith('workspace:campaign_'): return self.campaign_screen(data.split('campaign_',1)[1])
        if data=='workspace:account': return self.account_screen()
        if data=='workspace:partners': return self.partner_screen()
        if data.startswith('workspace:delete_'):
            self.manager_service.delete(data.split('delete_',1)[1])
            return self.callback('workspace:campaigns')
        return super().callback(data)

    def message(self,msg):
        session=self.store.session(self.owner)
        if session and session['action'] in ('manager_form','manager_partner_form'):
            p=json.loads(session['payload'])
            text=msg.get('text','').strip()
            if session['action']=='manager_form':
                self.manager_service.edit(p['cid'],p['field'],text)
                self.store.clear_session(self.owner)
                state=self.nav(); state['stack']=[r for r in state['stack'] if r!='mgr:form']
                self.store.put('navigation:'+str(self.owner),state)
                return self.callback('workspace:campaign_'+p['cid'])
            f=self.store.get('manager_partner_filter',{})
            if p['field']=='queries':
                values=list(dict.fromkeys(x.strip() for x in text.split(',') if x.strip()))
                if not 1<=len(values)<=3 or any(not 3<=len(x)<=80 for x in values): raise ValueError('От 1 до 3 фраз по 3–80 знаков.')
                f['queries']=values
            else:
                values=text.split()
                if len(values)!=2 or any(not x.isascii() or not x.isdigit() for x in values): raise ValueError('Два числа, например 1000 50000.')
                low,high=map(int,values)
                if max(low,high)>100000000 or (high and high<low): raise ValueError('Верхняя граница должна быть не меньше нижней.')
                f.update(min=low,max=high)
            self.store.put('manager_partner_filter',f)
            self.store.clear_session(self.owner)
            state=self.nav(); state['stack']=[r for r in state['stack'] if r!='mgr:form']
            self.store.put('navigation:'+str(self.owner),state)
            return self.callback('mgr:partner_filters')
        return super().message(msg)
