"""Persistent, serial account work. Network requests are never retried blindly."""
from __future__ import annotations

import json
import re
import secrets
import time

from .store import local_day


class AccountError(RuntimeError):
    pass


class Rejected(AccountError):
    """Telegram explicitly rejected the request: it was not delivered."""


class WaitRequired(Rejected):
    def __init__(self, seconds):
        self.seconds = max(1, int(seconds))
        super().__init__(f'Telegram требует паузу {self.seconds} сек. Запуск после ожидания — вручную.')


def parse_targets(text):
    targets = []
    for value in re.split(r'[,\s]+', text.strip()):
        if not value:
            continue
        value = re.sub(r'^https?://(?:t\.me|telegram\.me)/', '@', value).rstrip('/')
        if not re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{3,31}|-\d{5,16}', value):
            raise ValueError('Нужны @username, ссылка https://t.me/группа или ID группы. Для закрытой группы вступи вручную и укажи её ID. Ссылки-приглашения не используются.')
        value = value.lower()
        if value not in targets:
            targets.append(value)
    if not 1 <= len(targets) <= 100:
        raise ValueError('Укажи от 1 до 100 групп.')
    return targets


def normalized(item):
    return dict(dict(state='draft', gap_minutes=2, cycle_minutes=item.get('interval_minutes',30),
                     daily_limit=500, cursor=0, round=0, next_at=0, resolved=[], version=1,
                     reason='', round_start=0), **item)


class Manager:
    def __init__(self, store, gateway=None, clock=None):
        self.store, self.gateway, self.clock = store, gateway, clock or time.time
        with store.tx() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS manager_deliveries(
                id INTEGER PRIMARY KEY, campaign TEXT NOT NULL, idx INTEGER NOT NULL,
                target TEXT NOT NULL, started INTEGER NOT NULL, day TEXT NOT NULL,
                state TEXT NOT NULL, random_id INTEGER UNIQUE NOT NULL, version INTEGER NOT NULL,
                body TEXT NOT NULL, message_id INTEGER, completed INTEGER, error TEXT NOT NULL DEFAULT '')''')
            c.execute('''CREATE TABLE IF NOT EXISTS manager_channels(
                peer TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, about TEXT NOT NULL,
                subscribers INTEGER, last_post INTEGER, origin TEXT NOT NULL, found INTEGER NOT NULL,
                favorite INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0)''')

    def entries(self, c=None):
        return [normalized(x) for x in self.store.get('manager_campaigns', [], c)]

    def campaign(self, cid, c=None):
        item = next((x for x in self.entries(c) if x['id']==cid), None)
        if not item:
            raise ValueError('Кампания не найдена.')
        return item

    def change(self, cid, fn):
        with self.store.tx() as c:
            entries = self.entries(c)
            item = next((x for x in entries if x['id']==cid), None)
            if not item: raise ValueError('Кампания не найдена.')
            fn(item, c)
            self.store.put('manager_campaigns', entries, c)
            return item

    def create(self, name, text, targets):
        if not 1 <= len(name) <= 100 or not 1 <= len(text.encode('utf-16-le'))//2 <= 3500:
            raise ValueError('Название до 100 знаков, текст от 1 до 3500 знаков.')
        targets = parse_targets(','.join(targets))
        item = normalized(dict(id=secrets.token_hex(5),name=name,text=text,targets=targets))
        with self.store.tx() as c:
            entries = self.entries(c)
            if len(entries)>=50: raise ValueError('Максимум 50 кампаний.')
            entries.append(item)
            self.store.put('manager_campaigns', entries, c)
        return item

    def edit(self, cid, field, value):
        if field=='text' and not 1 <= len(value.encode('utf-16-le'))//2 <= 3500:
            raise ValueError('Текст от 1 до 3500 знаков.')
        if field=='targets': value=parse_targets(value)
        if field in ('gap_minutes','cycle_minutes','daily_limit'):
            low, high = {'gap_minutes':(2,60),'cycle_minutes':(30,1440),'daily_limit':(1,1000)}[field]
            if not str(value).isascii() or not str(value).isdigit() or not low<=int(value)<=high:
                raise ValueError(f'Укажи число от {low} до {high}.')
            value=int(value)
        if field not in ('text','targets','gap_minutes','cycle_minutes','daily_limit'):
            raise ValueError('Неизвестная настройка.')
        def update(item,c):
            if field!='text' and c.execute("SELECT 1 FROM manager_deliveries WHERE campaign=? AND state IN ('sending','unknown')",(cid,)).fetchone():
                raise ValueError('Сначала дождись отправки или проверь неизвестный результат в журнале.')
            item[field]=value
            item['version']+=1
            if field!='text':
                item.update(state='stopped',resolved=[],cursor=0,reason='Настройки изменены. Проверь и нажми Старт.')
        return self.change(cid, update)

    def start(self, cid):
        now = int(self.clock())
        def update(item,c):
            if self.store.get('emergency_stop',{},c).get('active'): raise ValueError('Аварийная остановка. Сначала проверь систему на сайте.')
            account=self.store.get('manager_account', {}, c)
            if not account.get('connected'):
                raise ValueError('Сначала подключи аккаунт в разделе «Подключение аккаунта».')
            if self.store.get('manager_cooldown',0,c)>now:
                raise ValueError('Ещё действует пауза Telegram. Время окончания — в разделе аккаунта.')
            if c.execute("SELECT 1 FROM manager_deliveries WHERE state IN ('sending','unknown')").fetchone():
                raise ValueError('Есть незавершённая отправка. Дождись результата или проверь журнал.')
            if any(x['id']!=cid and x['state'] in ('active','checking') for x in self.entries(c)):
                raise ValueError('На аккаунте уже работает другая кампания. Сначала останови её.')
            if item['state'] in ('active','checking'): return
            if not item['text'].strip() or not item['targets']: raise ValueError('Добавь текст и группы.')
            if not item['resolved'] or item.get('account_id')!=account.get('user_id'):
                item.update(state='checking',resolved=[],check_cursor=0,cursor=0,account_id=account.get('user_id'))
            else:
                item.update(state='active',next_at=max(now,item.get('next_at',0),self.store.get('manager_next_send',0,c)))
            item['reason']=''
        return self.change(cid, update)

    def stop(self, cid=None, reason='Остановлено владельцем.'):
        with self.store.tx() as c:
            entries=self.entries(c)
            for item in entries:
                if cid is None or item['id']==cid:
                    item.update(state='stopped',reason=reason)
            self.store.put('manager_campaigns',entries,c)
            if cid is None:
                job=self.store.get('manager_search',{},c)
                if job.get('state') in ('queued','running'):
                    job['state']='cancelled'
                    self.store.put('manager_search',job,c)

    def delete(self, cid):
        with self.store.tx() as c:
            if c.execute("SELECT 1 FROM manager_deliveries WHERE campaign=? AND state IN ('sending','unknown')",(cid,)).fetchone():
                raise ValueError('Сначала проверь незавершённую отправку в журнале.')
            self.store.put('manager_campaigns',[x for x in self.entries(c) if x['id']!=cid],c)

    def recover(self):
        with self.store.tx() as c:
            uncertain=c.execute("SELECT id FROM manager_deliveries WHERE state='sending'").fetchall()
            c.execute("UPDATE manager_deliveries SET state='unknown',error='Процесс прервался: проверь группу вручную' WHERE state='sending'")
            entries=self.entries(c)
            for item in entries:
                if uncertain or item['state']=='checking':
                    item.update(state='stopped',reason='После перезапуска проверь журнал и нажми Старт.')
            self.store.put('manager_campaigns',entries,c)
            if uncertain:
                self.store.notice('manager-recover:'+str(uncertain[-1]['id']),'⚠️ Результат отправки менеджера неизвестен. Проверь группу и отметь результат в журнале. Автоповтора нет.',c)
            job=self.store.get('manager_search',{},c)
            if job.get('state')=='running':
                job.update(state='failed',error='Поиск прервался при перезапуске; сохранённые находки доступны.')
                self.store.put('manager_search',job,c)

    def advance(self, item, now):
        item['cursor']+=1
        gap=item['gap_minutes']*60
        item['next_at']=now+gap
        if item['cursor']>=len(item['resolved']):
            item['cursor']=0
            item['round']+=1
            item['next_at']=max(item['round_start']+item['cycle_minutes']*60, now+gap)
            item['round_start']=item['next_at']

    def resolve(self, delivery_id, sent):
        now=int(self.clock())
        with self.store.tx() as c:
            row=c.execute("SELECT * FROM manager_deliveries WHERE id=? AND state='unknown'",(delivery_id,)).fetchone()
            if not row: raise ValueError('Нет неизвестной отправки.')
            entries=self.entries(c)
            item=next((x for x in entries if x['id']==row['campaign']),None)
            if item:
                self.advance(item,now)
                self.store.put('manager_campaigns',entries,c)
            c.execute("UPDATE manager_deliveries SET state=?,completed=?,error='Результат отмечен владельцем' WHERE id=?",('done' if sent else 'skipped',now,delivery_id))
            self.store.put('manager_next_send',now+120,c)

    def tick(self):
        if self.store.get('emergency_stop',{}).get('active'): return
        now=int(self.clock())
        if not self.store.get('manager_account',{}).get('connected'): return
        if self.store.get('manager_cooldown',0)>now: return
        item=next((x for x in self.entries() if x['state'] in ('checking','active')),None)
        if item and item['state']=='checking':
            index=item.get('check_cursor',0)
            try:
                peer=self.gateway.resolve_group(item['targets'][index])
                def checked(current,c):
                    if current['state']!='checking' or current.get('version')!=item['version']: return
                    if not any(x['key']==peer['key'] for x in current['resolved']): current['resolved'].append(peer)
                    current['check_cursor']=index+1
                    if index+1==len(current['targets']):
                        current.update(state='active',round_start=now,next_at=max(now,self.store.get('manager_next_send',0,c)))
                        self.store.notice('manager-ready:'+current['id']+':'+str(now),f'▶️ Проверены группы: {len(current["resolved"])}. Рассылка запущена. Интервал {current["gap_minutes"]} мин.',c)
                self.change(item['id'],checked)
            except Exception as e:
                self.failure(item,e,now)
            return
        if item and item['next_at']<=now and self.store.get('manager_next_send',0)<=now:
            self.deliver(item,now)
            return
        self.search_tick()

    def failure(self, item, error, now, delivery=None):
        known=isinstance(error,Rejected)
        message=str(error) if isinstance(error,AccountError) else 'Связь прервалась; результат нужно проверить вручную.'
        with self.store.tx() as c:
            entries=self.entries(c)
            for entry in entries:
                if entry['id']==item['id'] or isinstance(error,WaitRequired):
                    entry.update(state='stopped',reason=message)
            self.store.put('manager_campaigns',entries,c)
            if delivery:
                c.execute('UPDATE manager_deliveries SET state=?,error=? WHERE id=?',('blocked' if known else 'unknown',message[:500],delivery))
                if known:
                    peer=item['resolved'][item['cursor']]
                    self.store.put('manager_group_next:'+peer['key'],0,c)
            if isinstance(error,WaitRequired): self.store.put('manager_cooldown',now+error.seconds,c)
            self.store.notice('manager-error:'+str(delivery or item['id'])+':'+str(now),'⏸ Менеджер: '+message,c)

    def deliver(self, snapshot, now):
        with self.store.tx() as c:
            if self.store.get('emergency_stop',{},c).get('active'): return
            entries=self.entries(c)
            item=next((x for x in entries if x['id']==snapshot['id']),None)
            if not item or item['state']!='active' or item['next_at']>now: return
            if self.store.get('manager_next_send',0,c)>now or self.store.get('manager_cooldown',0,c)>now: return
            if c.execute("SELECT 1 FROM manager_deliveries WHERE state IN ('sending','unknown')").fetchone(): return
            used=c.execute("SELECT COUNT(*) FROM manager_deliveries WHERE day=? AND state IN ('sending','done','unknown')",(local_day(now),)).fetchone()[0]
            if used>=item['daily_limit']:
                item.update(state='stopped',reason='Достигнут дневной предел отправок. Запуск — вручную.')
                self.store.put('manager_campaigns',entries,c)
                self.store.notice('manager-daily:'+local_day(now),item['reason'],c)
                return
            if not item['resolved']: raise ValueError('Группы не проверены.')
            peer=item['resolved'][item['cursor']]
            group_next=self.store.get('manager_group_next:'+peer['key'],0,c)
            if group_next>now:
                item['next_at']=group_next
                self.store.put('manager_campaigns',entries,c)
                return
            if item['cursor']==0: item['round_start']=now
            rid=secrets.randbits(63)
            delivery=c.execute('''INSERT INTO manager_deliveries(campaign,idx,target,started,day,state,random_id,version,body)
                VALUES(?,?,?,?,?,'sending',?,?,?)''',(item['id'],item['cursor'],peer['ref'],now,local_day(now),rid,item['version'],item['text'])).lastrowid
            self.store.put('manager_campaigns',entries,c)
            self.store.put('manager_next_send',now+item['gap_minutes']*60,c)
            self.store.put('manager_group_next:'+peer['key'],now+item['cycle_minutes']*60,c)
        # No database lock is held during Telegram calls: Stop remains responsive.
        current=self.campaign(item['id'])
        if self.store.get('emergency_stop',{}).get('active') or current['state']!='active' or current['version']!=item['version']:
            with self.store.tx() as c:
                c.execute("UPDATE manager_deliveries SET state='cancelled' WHERE id=?",(delivery,))
                self.store.put('manager_group_next:'+peer['key'],group_next,c)
            return
        try:
            message_id=self.gateway.send_group(peer,item['text'],rid)
        except Exception as error:
            self.failure(item,error,int(self.clock()),delivery)
            return
        completed=int(self.clock())
        with self.store.tx() as c:
            entries=self.entries(c)
            current=next((x for x in entries if x['id']==item['id']),None)
            if current: self.advance(current,completed)
            c.execute("UPDATE manager_deliveries SET state='done',message_id=?,completed=? WHERE id=?",(message_id,completed,delivery))
            self.store.put('manager_campaigns',entries,c)
            self.store.put('manager_next_send',completed+item['gap_minutes']*60,c)

    def queue_search(self):
        if self.store.get('emergency_stop',{}).get('active'): raise ValueError('Аварийная остановка. Сначала проверь систему на сайте.')
        now=int(self.clock())
        with self.store.tx() as c:
            if self.store.get('emergency_stop',{},c).get('active'): raise ValueError('Аварийная остановка. Поиск закрыт до ручной проверки.')
            if not self.store.get('manager_account',{},c).get('connected'): raise ValueError('Сначала подключи аккаунт Telegram.')
            job=self.store.get('manager_search',{},c)
            if job.get('state') in ('queued','running'): raise ValueError('Поиск уже идёт. Дождись результатов.')
            if self.store.get('manager_cooldown',0,c)>now: raise ValueError('Ещё действует пауза Telegram.')
            if job.get('started',0)+120>now: raise ValueError('Следующий поиск доступен через 2 минуты после предыдущего.')
            criteria=dict(self.store.get('manager_partner_filter',{},c))
            if not criteria.get('queries'): raise ValueError('Сначала укажи тему поиска партнёров.')
            criteria['exclude']=[r['target'].lower() for r in c.execute('SELECT target FROM channels WHERE target IS NOT NULL').fetchall()]
            job=dict(id=secrets.token_hex(5),state='queued',started=now,criteria=criteria,found=0)
            self.store.put('manager_search',job,c)
        return job

    def search_tick(self):
        if self.store.get('emergency_stop',{}).get('active'): return
        job=self.store.get('manager_search',{})
        if job.get('state')!='queued': return
        job['state']='running'
        self.store.put('manager_search',job)
        seen={x['peer'] for x in self.store.rows('SELECT peer FROM manager_channels')}
        criteria=job['criteria']
        count=0
        try:
            for result in self.gateway.find_partners(criteria,seen):
                current=self.store.get('manager_search',{})
                if current.get('id')!=job['id'] or current.get('state')!='running': break
                peer=str(result['peer'])
                if peer in seen: continue
                if not re.fullmatch(r'https://t\.me/[A-Za-z][A-Za-z0-9_]{3,31}',result['url']): continue
                if '-100'+peer in criteria.get('exclude',[]) or '@'+result['url'].rsplit('/',1)[-1].lower() in criteria.get('exclude',[]): continue
                subscribers=result.get('subscribers')
                low,high=criteria.get('min',0),criteria.get('max',0)
                if (low or high) and (subscribers is None or subscribers<low or (high and subscribers>high)): continue
                self.store.execute('''INSERT OR IGNORE INTO manager_channels(peer,title,url,about,subscribers,last_post,origin,found)
                    VALUES(?,?,?,?,?,?,?,?)''',(peer,result['title'][:150],result['url'],result.get('about','')[:800],subscribers,
                    result.get('last_post'),result.get('origin','Поиск Telegram')[:120],int(self.clock())))
                seen.add(peer)
                count+=1
                job['found']=count
                self.store.put('manager_search',dict(current,found=count))
                if count>=10: break
            current=self.store.get('manager_search',{})
            if current.get('state')=='running': self.store.put('manager_search',dict(current,state='done'))
            self.store.notice('manager-partners:'+job['id'],f'🤝 Поиск партнёров завершён. Новых каналов: {count}. Менеджер → Партнёры. Сообщения им не отправлялись.')
        except Exception as error:
            current=self.store.get('manager_search',{})
            message=str(error) if isinstance(error,AccountError) else 'Поиск прервался; сохранённые находки доступны.'
            self.store.put('manager_search',dict(current,state='failed',error=message))
            if isinstance(error,WaitRequired):
                self.stop(reason=message)
                self.store.put('manager_cooldown',int(self.clock())+error.seconds)
            self.store.notice('manager-partners:'+job['id'],f'🤝 {message} Найдено: {count}.')

    def loop(self, shutdown):
        self.recover()
        self.store.put('manager_account',dict(connected=False,note='Проверка подключения'))
        self.store.put('manager_check_account',True)
        while not shutdown.is_set():
            try:
                if self.store.get('manager_check_account',False):
                    self.store.put('manager_check_account',False)
                    status=self.gateway.status()
                    self.store.put('manager_account',status)
                    if status.get('cooldown'): self.store.put('manager_cooldown',status['cooldown'])
                    if not status.get('connected'): self.stop(reason=status.get('note','Аккаунт недоступен.'))
                self.tick()
            except Exception:
                self.stop(reason='Ошибка менеджера. Проверь журнал и подключение аккаунта.')
            shutdown.wait(1)
        self.gateway.close()
