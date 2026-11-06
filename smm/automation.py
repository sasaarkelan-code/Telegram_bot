"""Scheduling decisions stay deterministic; editorial approval comes from the AI."""
from datetime import datetime, timedelta
import json
import math

from .store import TZ


def next_release(config, now, occupied=(), planned=0, automatic=False):
    gap=max(60 if automatic else 1,int(config.get('post_gap_minutes',5)))*60
    count=max(1,min(2,int(config.get('posts_per_run',2))))
    if planned:
        if now>planned+1800:
            raise ValueError('Окно автоматического выпуска пропущено. Пост оставлен для проверки.')
        bases=[max(int(planned),math.ceil(now)+(600 if automatic else 30))]
    else:
        current=datetime.fromtimestamp(now,TZ)
        bases=[]
        for offset in range(31):
            day=current.date()+timedelta(days=offset)
            for label in sorted(config.get('publish_times') or config.get('search_times') or ['09:00','15:00','21:00']):
                hour,minute=map(int,label.split(':'))
                bases.append(int(datetime(day.year,day.month,day.day,hour,minute,tzinfo=TZ).timestamp()))
    for base in bases:
        for index in range(count):
            due=base+index*gap
            if due>=now+(600 if automatic else 15) and due<=now+30*86400 and not any(abs(due-t)<gap for t in occupied):
                return due
    raise ValueError('Свободного времени в расписании нет. Проверь календарь.')


class AutoPublisher:
    def __init__(self,store): self.store=store

    def queue_batch(self,sid,now):
        slot=self.store.slot(sid)
        if not slot: return 0
        ch=self.store.channel(slot['channel_id'])
        if not ch or not ch['config'].get('auto_publish',False): return 0
        queued=0
        for row in self.store.rows('SELECT id FROM candidates WHERE slot_id=? AND state=\'pending\' ORDER BY id',(sid,)):
            p=self.store.candidate(row['id'])
            try:
                due=self.choose_time(p,now,slot.get('publish_at',0),automatic=True)
                self.store.schedule(p['id'],due,now,automatic=True)
                queued+=1
            except ValueError as error:
                self.store.execute("UPDATE candidates SET publication_error=? WHERE id=? AND state='pending'",(str(error)[:500],p['id']))
                self.store.notice('auto-held:'+str(p['id']),f'⚠️ {ch["name"]}: пост #{p["id"]} оставлен на проверку. {error}')
        return queued

    def choose_time(self,p,now,planned=0,automatic=False):
        occupied=self.store.rows('''SELECT COALESCE(p.scheduled_at,s.due) AS at FROM candidates p
            JOIN slots s ON s.id=p.slot_id WHERE s.channel_id=? AND p.id<>?
            AND p.state IN ('approved','publishing','sent') AND COALESCE(p.scheduled_at,s.due)>?''',
            (p['channel_id'],p['id'],int(now)-(86400 if automatic else 1800)))
        cfg=dict(p['config'])
        if automatic:
            from .store import local_day
            plan=self.store.get('ai_plan:'+str(p['channel_id']),{})
            daily=plan.get('daily_posts',1 if cfg.get('mode') in ('cinema','fashion') else 6)
            cfg['publish_times']=plan.get('times') or cfg.get('publish_times')
            if type(p.get('meta',{}).get('preferred_hour')) is int:
                cfg['publish_times']=[f"{p['meta']['preferred_hour']:02d}:00"]
                planned=0
            due=next_release(cfg,now,[r['at'] for r in occupied],planned,True)
            if sum(local_day(r['at'])==local_day(due) for r in occupied)>=daily:
                raise ValueError('План канала на этот день уже заполнен. Материал остаётся на проверке.')
            return due
        return next_release(cfg,now,[r['at'] for r in occupied],planned,False)


def accept_plan(store,channel,value,now):
    if not isinstance(value,dict):return
    cap=1 if channel['config'].get('mode') in ('cinema','fashion') else 6
    count=value.get('daily_posts');hours=value.get('hours')
    if type(count) is not int or not 1<=count<=cap or not isinstance(hours,list) or not 1<=len(hours)<=3 or any(type(h) is not int or not 8<=h<=22 for h in hours):return
    store.put('ai_plan:'+str(channel['id']),dict(daily_posts=count,times=[f'{h:02d}:00' for h in sorted(set(hours))],reason=str(value.get('reason',''))[:250],updated=int(now)))
