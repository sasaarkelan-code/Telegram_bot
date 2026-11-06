"""Real measurements, explicit coverage, no AI tokens and no synthetic zeroes."""
import time

class Analytics:
    def __init__(self,store,telegram,clock=None):
        self.store,self.telegram,self.clock=store,telegram,clock or time.time
        with store.tx() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS analytics_subscribers(channel_id INTEGER NOT NULL,target TEXT NOT NULL,recorded INTEGER NOT NULL,subscribers INTEGER NOT NULL,PRIMARY KEY(channel_id,recorded))''')
            c.execute('''CREATE TABLE IF NOT EXISTS analytics_views(channel_id INTEGER NOT NULL,target TEXT NOT NULL,message_id INTEGER NOT NULL,recorded INTEGER NOT NULL,published INTEGER NOT NULL,views INTEGER NOT NULL,PRIMARY KEY(channel_id,target,message_id,recorded))''')

    def collect(self):
        now=int(self.clock())
        for channel in self.store.channels():
            if not channel['target']:continue
            try:
                count=self.telegram.call('getChatMemberCount',chat_id=channel['target'])
                if type(count) is not int or count<0:raise ValueError()
                self.store.execute('INSERT OR IGNORE INTO analytics_subscribers VALUES(?,?,?,?)',(channel['id'],channel['target'],now,count))
                self.store.put('analytics_error:'+str(channel['id']),'')
            except Exception:self.store.put('analytics_error:'+str(channel['id']),'Не удалось обновить подписчиков. Проверь права бота.')

    def collect_views(self,gateway):
        now=int(self.clock())
        if self.store.get('analytics_views_next',0)>now:return
        self.store.put('analytics_views_next',now+3600)
        for ch in self.store.channels():
            if not ch['target']:continue
            try:
                rows=gateway.channel_metrics(ch['target'])
                with self.store.tx() as c:
                    for row in rows:
                        if any(type(row.get(k)) is not int or row[k]<0 for k in ('message_id','published','views')):continue
                        c.execute('INSERT OR IGNORE INTO analytics_views VALUES(?,?,?,?,?,?)',(ch['id'],ch['target'],row['message_id'],now,row['published'],row['views']))
                self.store.put('analytics_views_error:'+str(ch['id']),'')
            except Exception:self.store.put('analytics_views_error:'+str(ch['id']),'Просмотры недоступны: проверь доступ подключённого аккаунта к каналу.')

    def report(self,period=86400):
        now=int(self.clock());result=[];seen=set()
        for ch in self.store.channels():
            if not ch['target'] or str(ch['target']).lower() in seen:continue
            seen.add(str(ch['target']).lower())
            rows=self.store.rows('SELECT recorded,subscribers FROM analytics_subscribers WHERE channel_id=? AND target=? ORDER BY recorded',(ch['id'],ch['target']))
            latest=rows[-1] if rows else None;baseline=next((r for r in reversed(rows) if r['recorded']<=now-period),None)
            views=self.store.rows('SELECT * FROM analytics_views WHERE channel_id=? AND target=? ORDER BY recorded',(ch['id'],ch['target']))
            grouped={}
            for row in views:grouped.setdefault(row['message_id'],[]).append(row)
            posts=[];gain=0;complete=True
            for mid,history in grouped.items():
                last=history[-1];old=next((r for r in reversed(history) if r['recorded']<=now-period),None)
                delta=max(0,last['views']-old['views']) if old else last['views'] if last['published']>=now-period else None
                if delta is None:complete=False
                else:gain+=delta
                if last['published']>=now-period:posts.append(dict(id=mid,views=last['views'],published=last['published']))
            result.append(dict(channel_id=ch['id'],name=ch['name'],subscribers=latest['subscribers'] if latest else None,recorded=latest['recorded'] if latest else None,change=latest['subscribers']-baseline['subscribers'] if latest and baseline else None,baseline=baseline['recorded'] if baseline else None,error=self.store.get('analytics_error:'+str(ch['id']),''),views_error=self.store.get('analytics_views_error:'+str(ch['id']),''),views_recorded=max((r['recorded'] for r in views),default=None),views_gain=gain if grouped and complete else None,post_views=sum(p['views'] for p in posts) if grouped else None,posts=sorted(posts,key=lambda p:p['published'],reverse=True),history=[r for r in rows if r['recorded']>=now-period]))
        return result

    def loop(self,shutdown):
        while not shutdown.is_set():
            try:self.collect()
            except Exception:pass
            if shutdown.wait(3600):return
