"""Owner-only event summaries. Notification failures never pause publications."""
import html
import re
import threading
import time

from .net import NetError


class EventNotifications:
    def __init__(self,store,telegram,clock=None):
        self.store,self.tg=store,telegram
        self.clock=clock or time.time
        self.lock=threading.Lock()

    @staticmethod
    def category(row):
        key=row['notice_key']
        if key.startswith('emergency:'):return 'emergency'
        counts=re.search(r'найдено (\d+) из (\d+)',row.get('text',''),re.I)
        if counts and int(counts[1])<int(counts[2]): return 'partial'
        if key.startswith(('slot-failed:','publish-error:','halt:','ai-problem:','manager-error:','missed-publication:','budget-warning:')): return 'errors'
        if key.startswith('auto-held:'): return 'partial'
        return 'success'

    def tick(self):
        if not self.lock.acquire(blocking=False): return
        try:
            now=int(self.clock())
            cfg=self.store.get('telegram_notifications',{})
            owner=self.store.get('owner',0)
            if not owner: return
            rows=self.store.rows('''SELECT * FROM notices WHERE notify=1 AND delivered=0 AND attempts<3
                AND (notice_key LIKE 'emergency:%' OR (? AND created>=? AND id>?))
                ORDER BY CASE WHEN notice_key LIKE 'emergency:%' THEN 0 ELSE 1 END,id LIMIT 30''',
                (int(bool(cfg.get('enabled'))),cfg.get('since',now),cfg.get('min_id',0)))
            ready=[]
            for row in rows:
                category=self.category(row)
                if category!='emergency' and not cfg.get(category,True):
                    self.store.execute("UPDATE notices SET delivered=3,delivery_error='Категория отключена' WHERE id=? AND delivered=0",(row['id'],))
                    continue
                if row['last_attempt'] and row['last_attempt']+min(1800,30*2**row['attempts'])>now: continue
                ready.append(row)
                if len(ready)==4: break
            if not ready:return
            with self.store.tx() as c:
                current=self.store.get('telegram_notifications',{},c)
                if owner!=self.store.get('owner',0,c): return
                claimed=[]
                for row in ready:
                    critical=self.category(row)=='emergency'
                    if not critical and (not current.get('enabled') or not current.get(self.category(row),True)):continue
                    if c.execute('UPDATE notices SET delivered=-1,last_attempt=?,attempts=attempts+1 WHERE id=? AND delivered=0',(now,row['id'])).rowcount:claimed.append(row)
            ready=claimed
            if not ready:return
            ids=[r['id'] for r in ready]
            placeholders=','.join('?' for _ in ids)
            message='<b>Студия · события</b>\n\n'+'\n\n'.join(html.escape(r['text'][:650]) for r in ready)+'\n\nПодробности и управление — на сайте. /web — вход.'
            try:
                self.tg.send(owner,message)
            except Exception as error:
                # Only a definite refusal may be retried; an ambiguous send is not repeated.
                retry=isinstance(error,NetError) and error.status==429
                self.store.execute(f'UPDATE notices SET delivered=?,delivery_error=? WHERE id IN ({placeholders})',
                    (0 if retry else 2,'Telegram не подтвердил уведомление: '+type(error).__name__,*ids))
            else:
                self.store.execute(f"UPDATE notices SET delivered=1,delivery_error='' WHERE id IN ({placeholders})",ids)
        finally:self.lock.release()

    def loop(self,shutdown):
        while not shutdown.wait(5):
            try:self.tick()
            except Exception:
                # The event journal remains available on the web if Telegram is unavailable.
                continue
