"""Persistent publication circuit breaker. No network or AI calls in this guard."""
import hashlib
import html
import re

from .store import local_day


def destination(value):
    value=str(value or '').strip().lower().rstrip('/')
    return re.sub(r'^https?://(?:t\.me|telegram\.me)/','@',value)


def normalized(value):
    value=html.unescape(re.sub(r'<[^>]+>',' ',str(value)))
    value=re.sub(r'https?://\S+',' ',value)
    return ' '.join(re.findall(r'[\w]+',value.lower(),re.UNICODE))


def quality_problem(post):
    text=normalized(str(post.get('title',''))+' '+str(post.get('body','')))
    words=re.findall(r'[a-zа-яё]{2,}',text)
    if len(''.join(words))<15 or len(words)<3:
        return 'В посте слишком мало осмысленного текста.'
    if re.search(r'([a-zа-яё])\1{8,}',text):
        return 'В тексте обнаружено многократное повторение одного символа.'
    if len(words)>=12 and (len(set(words))<4 or max(words.count(w) for w in set(words))/len(words)>.65):
        return 'Текст состоит преимущественно из повторяющихся слов.'
    body=normalized(post.get('body',''))
    if any(body.startswith(mark) for mark in ('lorem ipsum','вставьте текст сюда','ваш текст здесь','as an ai language model')):
        return 'Вместо готового поста обнаружен шаблон или служебный ответ.'
    meta=post.get('meta',{})
    if meta.get('demo') or meta.get('unverified'):
        return 'Материал помечен как демонстрационный или непроверенный.'
    if post.get('auto_queued'):
        from .editor import review_digest
        if meta.get('editor_approved') is not True or not meta.get('editor_reason'):
            return 'Автоматический пост не имеет положительной редакторской проверки ИИ.'
        if meta.get('editor_digest')!=review_digest(post):
            return 'Автоматический пост изменился после проверки ИИ.'
    return ''


def overlap(a,b):
    x,y=set(a.split()),set(b.split())
    return len(x&y)/len(x|y) if x and y else 0


class PublicationSafety:
    def __init__(self,store):self.store=store

    def claim(self,post,config,now,c):
        """Called inside the same transaction as the publisher's claim."""
        if self.store.get('emergency_stop',{},c).get('active'): return None
        target=destination(post['target'])
        if post.get('auto_queued'):
            previous=c.execute("SELECT MAX(started) FROM publication_dispatches WHERE target=? AND outcome IN ('sending','sent','unknown')",(target,)).fetchone()[0]
            if previous and previous+3600>now:
                c.execute('UPDATE candidates SET scheduled_at=? WHERE id=?',(previous+3600,post['id']))
                return 0
        problem=quality_problem(post)
        body=normalized(post['body']);title=normalized(post['title'])
        fingerprint=hashlib.sha256((title+'\n'+body).encode()).hexdigest()
        rows=[dict(r) for r in c.execute("SELECT * FROM publication_dispatches WHERE target=? AND started>? AND outcome!='cancelled' ORDER BY id DESC",(target,int(now)-7*86400)).fetchall()]
        # Existing 2.4 installations have no immutable dispatch history yet.
        # Use their saved publication time for older confirmed sends only.
        previous=c.execute("""SELECT p.id,p.title,p.body,p.event_key,ch.target,COALESCE(p.scheduled_at,s.due) AS at
            FROM candidates p JOIN slots s ON s.id=p.slot_id JOIN channels ch ON ch.id=s.channel_id
            WHERE p.state='sent' AND COALESCE(p.scheduled_at,s.due)>? AND COALESCE(p.scheduled_at,s.due)<=?
            AND NOT EXISTS(SELECT 1 FROM publication_dispatches d WHERE d.candidate_id=p.id)""",(int(now)-7*86400,int(now))).fetchall()
        for old in previous:
            if destination(old['target'])!=target:continue
            old_body=normalized(old['body']);old_title=normalized(old['title'])
            rows.append(dict(candidate_id=old['id'],started=old['at'],day=local_day(old['at']),
                fingerprint=hashlib.sha256((old_title+'\n'+old_body).encode()).hexdigest(),body=old_body,
                event_key=old['event_key'],outcome='sent'))
        recent=[r for r in rows if r['started']>now-600]
        # Keep the usual 3 x 2 schedule, with a hard ceiling of 12 per day.
        slots=config.get('publish_times') or config.get('search_times') or config.get('times') or ['09:00','15:00','21:00']
        daily=max(2,min(12,len(set(slots))*max(1,min(2,int(config.get('posts_per_run',2))))))
        plan=self.store.get('ai_plan:'+str(post['channel_id']),{},c)
        if type(plan.get('daily_posts')) is int and 1<=plan['daily_posts']<=6:daily=max(2,plan['daily_posts'])
        if not problem and len(recent)>=2:
            problem='Попытка отправить третий пост в один канал за 10 минут.'
        if not problem and sum(r['day']==local_day(now) for r in rows)>=daily:
            problem=f'Превышен дневной предел канала: {daily} публикаций по его расписанию.'
        if not problem:
            for r in rows:
                if r['outcome'] not in ('sending','sent','unknown'):continue
                exact=bool(post.get('event_key') and post['event_key']==r['event_key']) or fingerprint==r['fingerprint'] or (len(body)>=30 and body==r['body'])
                near=len(set(body.split()))>=8 and overlap(body,r['body'])>=.94
                if exact or near:
                    problem=f'Повтор или почти одинаковый пост: ранее отправлялся материал #{r["candidate_id"]}.'
                    break
        if problem:
            self.store.emergency_halt(problem,post=post,now=now,c=c)
            return None
        return c.execute('''INSERT INTO publication_dispatches(candidate_id,target,started,day,fingerprint,title,body,event_key,outcome)
            VALUES(?,?,?,?,?,?,?,?,'sending')''',(post['id'],target,int(now),local_day(now),fingerprint,title,body,str(post.get('event_key','')))).lastrowid

    def quality_result(self,cid,valid,now):
        """Three bad editorial batches are a fault, not an endless paid retry."""
        with self.store.tx() as c:
            key='quality_streak:'+str(cid)
            if valid:self.store.put(key,0,c);return
            count=self.store.get(key,0,c)+1;self.store.put(key,count,c)
            if count>=3:
                ch=c.execute('SELECT name,target FROM channels WHERE id=?',(cid,)).fetchone()
                self.store.emergency_halt('Три подбора подряд не прошли проверку качества ИИ.',
                    post=dict(channel_id=cid,channel_name=ch['name'],target=ch['target'],id=0),now=now,c=c)
