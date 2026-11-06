"""Private web console. Same Store and Engine as the Telegram worker."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

from . import __version__
from .budget import Budget
from .source_presets import PRESETS
from .editor import render, utf16
from .manager import Manager, parse_targets
from .net import NetError, public_url
from .planning import publication_time
from .store import TZ
from .settings import LIMIT_FIELDS, parse_times


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def install_tables(store):
    with store.tx() as c:
        for sql in (
            'CREATE TABLE IF NOT EXISTS web_codes(hash TEXT PRIMARY KEY,actor INTEGER NOT NULL,expires INTEGER NOT NULL)',
            'CREATE TABLE IF NOT EXISTS web_sessions(hash TEXT PRIMARY KEY,actor INTEGER NOT NULL,csrf TEXT NOT NULL,expires INTEGER NOT NULL)',
            'CREATE TABLE IF NOT EXISTS web_attempts(id INTEGER PRIMARY KEY,ip TEXT NOT NULL,created INTEGER NOT NULL)',
            'CREATE TABLE IF NOT EXISTS web_audit(id INTEGER PRIMARY KEY,actor INTEGER NOT NULL,action TEXT NOT NULL,created INTEGER NOT NULL)',
        ): c.execute(sql)
        columns={row['name'] for row in c.execute('PRAGMA table_info(web_codes)')}
        if 'username' not in columns: c.execute("ALTER TABLE web_codes ADD COLUMN username TEXT NOT NULL DEFAULT ''")
        c.execute('CREATE INDEX IF NOT EXISTS web_attempt_time ON web_attempts(created)')


def issue_code(store, actor, username=None):
    install_tables(store)
    now = int(time.time())
    with store.tx() as c:
        if actor not in [store.get('owner', 0, c)] + store.get('coowners', [], c):
            raise ValueError('Нет доступа к панели.')
        if username is None: username=store.get('web_username:'+str(actor),'',c)
        username=str(username or '').strip().lstrip('@').lower()
        if username and not re.fullmatch(r'[a-z0-9_]{5,32}',username): username=''
        store.put('web_username:'+str(actor),username,c)
        code = secrets.token_urlsafe(24)
        c.execute('DELETE FROM web_codes WHERE actor=? OR expires<?', (actor, now))
        c.execute('INSERT INTO web_codes(hash,actor,expires,username) VALUES(?,?,?,?)', (digest(code), actor, now+300,username))
    return code


def create_app(root, store, engine, auth, telegram, manager=None, env=None):
    env = os.environ if env is None else env
    assets = Path(__file__).resolve().parent / 'web'
    app = Flask(__name__, static_folder=None)
    app.config['MAX_CONTENT_LENGTH'] = 11 * 1024 * 1024
    secure = env.get('SMM_WEB_COOKIE_SECURE', '1') != '0'
    public = env.get('SMM_WEB_URL', '').strip().rstrip('/')
    if public and (urlsplit(public).scheme not in ('https', 'http') or urlsplit(public).path):
        raise ValueError('SMM_WEB_URL: укажи адрес сайта без пути, например https://my.bothost.tech')
    manager = manager or Manager(store)
    install_tables(store)
    media_root = root / 'media'
    media_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def audit(action):
        with store.tx() as c:
            c.execute('INSERT INTO web_audit(actor,action,created) VALUES(?,?,?)', (g.actor, action[:200], int(time.time())))
            c.execute('DELETE FROM web_audit WHERE id NOT IN (SELECT id FROM web_audit ORDER BY id DESC LIMIT 2000)')

    def data():
        value = request.get_json(silent=True)
        if not isinstance(value, dict): raise ValueError('Ожидается JSON-объект.')
        return value

    def number(value, low=1, high=2**63-1):
        if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit() or not low <= int(value) <= high:
            raise ValueError(f'Укажи целое число от {low} до {high}.')
        return int(value)

    def need_post(pid):
        p = store.candidate(pid)
        if not p: raise ValueError('Пост не найден.')
        return p

    @app.before_request
    def protect():
        if request.method not in ('GET', 'HEAD', 'POST'): return jsonify(error='Метод недоступен.'), 405
        if request.method == 'POST':
            origin = request.headers.get('Origin')
            expected = public or request.host_url.rstrip('/')
            if origin and origin != expected: return jsonify(error='Недопустимый источник запроса.'), 403
            if request.headers.get('X-SMM-Web') != '1': return jsonify(error='Нужен запрос из панели.'), 403
        if request.path in ('/', '/healthz', '/api/login','/manifest.webmanifest','/sw.js') or request.path.startswith('/assets/'):
            return
        token = request.cookies.get('smm_session', '')
        rows = store.rows('SELECT * FROM web_sessions WHERE hash=? AND expires>?', (digest(token), int(time.time()))) if token else []
        if not rows: return jsonify(error='Войди в панель по одноразовому коду.', login=True), 401
        sess = rows[0]
        if sess['actor'] not in [store.get('owner', 0)] + store.get('coowners', []):
            store.execute('DELETE FROM web_sessions WHERE hash=?', (digest(token),))
            return jsonify(error='Доступ отозван.', login=True), 401
        g.actor, g.csrf = sess['actor'], sess['csrf']
        if request.method == 'POST' and not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), g.csrf):
            return jsonify(error='Проверка сессии не прошла. Обнови страницу.'), 403

    @app.after_request
    def headers(response):
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
        })
        if public.startswith('https://'): response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    @app.errorhandler(ValueError)
    def bad_input(error): return jsonify(error=str(error)), 400

    @app.errorhandler(NetError)
    def upstream(error): return jsonify(error='Сервис временно недоступен. Действие не подтверждено; проверь журнал.'), 502

    @app.errorhandler(Exception)
    def failure(error):
        if isinstance(error, HTTPException): return jsonify(error=error.description), error.code
        # No request bodies, tokens, credential paths or upstream responses in logs.
        logging.getLogger(__name__).error('Web operation failed: %s', type(error).__name__)
        return jsonify(error='Не удалось выполнить действие. Проверь состояние и журнал.'), 500

    @app.get('/')
    def index(): return send_file(assets / 'index.html')

    @app.get('/assets/<name>')
    def asset(name):
        if name not in ('app.js', 'style.css','aurel.css', 'icon.svg','icon-192.png','icon-512.png','theme-init.js'): return '', 404
        return send_file(assets / name)

    @app.get('/manifest.webmanifest')
    def manifest(): return send_file(assets / 'manifest.webmanifest',mimetype='application/manifest+json')

    @app.get('/sw.js')
    def service_worker(): return send_file(assets / 'sw.js',mimetype='application/javascript')

    @app.get('/healthz')
    def health(): return jsonify(status='ok', version=__version__)

    @app.post('/api/login')
    def login():
        now = int(time.time())
        # Ignore untrusted forwarded IP headers. Shared proxy limit remains bounded.
        ip = digest(request.remote_addr or 'unknown')
        incoming=data()
        value = incoming.get('code', '')
        username=str(incoming.get('username','')).strip().lstrip('@').lower()[:100]
        if not isinstance(value, str) or len(value) > 100: value = ''
        with store.tx() as c:
            c.execute('DELETE FROM web_attempts WHERE created<?', (now-600,))
            c.execute('DELETE FROM web_sessions WHERE expires<?', (now,))
            count = c.execute('SELECT COUNT(*) FROM web_attempts WHERE ip=?', (ip,)).fetchone()[0]
            total = c.execute('SELECT COUNT(*) FROM web_attempts').fetchone()[0]
            if count >= 20 or total >= 200: return jsonify(error='Слишком много попыток. Подожди 10 минут.'), 429
            c.execute('INSERT INTO web_attempts(ip,created) VALUES(?,?)', (ip, now))
            row = c.execute('SELECT * FROM web_codes WHERE hash=? AND expires>?', (digest(value.strip()), now)).fetchone()
            allowed = [store.get('owner', 0, c)] + store.get('coowners', [], c)
            if not row or row['actor'] not in allowed or username not in ({str(row['actor']),row['username']} - {''}): return jsonify(error='Username или код неверный, либо код истёк. Получи новый командой /web в боте.'), 401
            c.execute('DELETE FROM web_codes WHERE hash=?', (row['hash'],))
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            c.execute('INSERT INTO web_sessions VALUES(?,?,?,?)', (digest(token), row['actor'], csrf, now+86400))
            # At most 20 active browsers per member.
            c.execute('DELETE FROM web_sessions WHERE actor=? AND hash NOT IN (SELECT hash FROM web_sessions WHERE actor=? ORDER BY expires DESC LIMIT 20)', (row['actor'],row['actor']))
        response = jsonify(ok=True)
        response.set_cookie('smm_session', token, max_age=86400, secure=secure, httponly=True, samesite='Strict', path='/')
        return response

    @app.post('/api/logout')
    def logout():
        store.execute('DELETE FROM web_sessions WHERE hash=?', (digest(request.cookies.get('smm_session', '')),))
        response = jsonify(ok=True)
        response.delete_cookie('smm_session', path='/', secure=secure, httponly=True, samesite='Strict')
        return response

    @app.get('/api/state')
    def state():
        rows = store.rows("""SELECT p.id,p.title,p.body,p.media,p.meta,p.state,p.created,p.scheduled_at,
            COALESCE(p.scheduled_at,s.due) AS due,s.kind AS batch_kind,s.channel_id,ch.name AS channel_name,p.publication_error
            FROM candidates p JOIN slots s ON s.id=p.slot_id JOIN channels ch ON ch.id=s.channel_id
            WHERE p.state IN ('approved','unknown','publishing') OR p.id IN
                (SELECT id FROM candidates WHERE state IN ('pending','draft') OR created>? ORDER BY created DESC,id DESC LIMIT 300)
            ORDER BY p.created DESC,p.id DESC""",(int(time.time())-7*86400,))
        posts=[]
        for row in rows:
            row['meta']=json.loads(row['meta'])
            try: row['preview_html']=render(row)
            except (KeyError,TypeError,ValueError):
                import html
                row['preview_html']='<b>'+html.escape(row['title'])+'</b>\n\n'+html.escape(row['body'])
            posts.append(row)
        channels = store.channels()
        notice_read = store.get('web_notice_read:'+str(g.actor),0)
        return jsonify(version=__version__, actor=g.actor, username=store.get('web_username:'+str(g.actor),''), csrf=g.csrf, now=int(time.time()), timezone='Europe/Moscow',
            notices=store.rows('SELECT id,text,created,delivery_error FROM notices ORDER BY id DESC LIMIT 100'),
            notice_read=notice_read, notice_unread=store.rows('SELECT COUNT(*) AS n FROM notices WHERE id>?',(notice_read,))[0]['n'],
            paused=store.get('paused', True), pause_reason=store.get('pause_reason', ''), emergency=store.get('emergency_stop',{}),
            model=store.get('model',''), models=store.get('model_catalog',[]), ai_connected=auth.saved(),
            channels=channels, posts=posts, searches=store.rows("SELECT id,channel_id,state,error,requested_at FROM slots WHERE kind='batch' ORDER BY id DESC LIMIT 60"),
            usage=Budget(store).usage(), limits=store.get('limits'), budget_enforced=store.get('budget_enforced',True), fashion_unmetered=store.get('fashion_unmetered',True),
            source_health=store.rows('SELECT url,checked,error FROM feed_cache'), source_presets=PRESETS,
            automation_status=[dict(channel_id=c['id'],blocked=store.get('publication_blocked:'+str(c['id']),'')) for c in channels],
            ai_problem=store.get('ai_problem',''),telegram_notifications=store.get('telegram_notifications',{}),
            campaigns=manager.entries(), account=store.get('manager_account',{}), cooldown=store.get('manager_cooldown',0),
            deliveries=store.rows('SELECT id,campaign,target,started,completed,state,error FROM manager_deliveries ORDER BY id DESC LIMIT 100'),
            partners=store.rows('SELECT * FROM manager_channels ORDER BY favorite DESC,found DESC LIMIT 300'),
            partner_filter=store.get('manager_partner_filter',{}), partner_search=store.get('manager_search',{}),
            owner=store.get('owner'), coowners=store.get('coowners',[]),
            audit=store.rows('SELECT actor,action,created FROM web_audit ORDER BY id DESC LIMIT 40'))

    @app.get('/api/analytics')
    def analytics_report():
        from .analytics import Analytics
        days=request.args.get('days','1')
        if days not in ('1','7','30'):raise ValueError('Выбери 1, 7 или 30 дней.')
        return jsonify(channels=Analytics(store,telegram).report(int(days)*86400),days=int(days))

    @app.post('/api/notices/read')
    def read_notices():
        until=number(data().get('until'),0,2**63-1)
        with store.tx() as c:
            latest=c.execute('SELECT COALESCE(MAX(id),0) FROM notices').fetchone()[0]
            key='web_notice_read:'+str(g.actor)
            store.put(key,max(store.get(key,0,c),min(until,latest)),c)
        return jsonify(ok=True)

    @app.post('/api/notification-settings')
    def notification_settings():
        incoming=data();allowed={'enabled','success','errors','partial'}
        if not incoming or set(incoming)-allowed or any(type(v) is not bool for v in incoming.values()): raise ValueError('Нужны переключатели уведомлений.')
        current=store.get('telegram_notifications',{})
        if incoming.get('enabled') is True and not current.get('enabled'):
            current['since']=int(time.time())
            current['min_id']=store.rows('SELECT COALESCE(MAX(id),0) AS id FROM notices')[0]['id']
        current.update(incoming);store.put('telegram_notifications',current)
        audit('Изменены уведомления Telegram')
        return jsonify(ok=True)

    @app.post('/api/control')
    def control():
        action = data().get('action')
        if action in ('pause','stop_all'):
            store.halt('Остановлено из веб-панели.')
            if action == 'stop_all': manager.stop()
        elif action == 'resume':
            if not store.get('model') or not auth.saved(): raise ValueError('Сначала подключи ИИ на сервере и выбери модель в настройках.')
            if not store.get('independent_publication',False) and (store.rows("SELECT id FROM slots WHERE state='unknown'") or store.rows("SELECT id FROM candidates WHERE state='unknown'")):
                raise ValueError('Проверь публикации с неизвестным результатом в календаре.')
            store.resume_after_review(g.actor); engine.wake.set()
        else: raise ValueError('Неизвестное действие.')
        audit('СММ: '+action)
        return jsonify(ok=True)

    @app.post('/api/channels')
    def add_channel():
        d=data(); name=str(d.get('name','')).strip()
        if not 2<=len(name)<=80 or len(store.channels())>=30: raise ValueError('Название 2–80 знаков, максимум 30 каналов.')
        cid=store.add_channel(name); audit('Создан канал #'+str(cid))
        return jsonify(id=cid)

    @app.post('/api/channels/<int:cid>')
    def channel(cid):
        ch=store.channel(cid)
        if not ch: raise ValueError('Канал не найден.')
        d=data(); action=d.get('action','save')
        if action=='search':
            if store.get('paused',True): raise ValueError('СММ на паузе. Проверь причину и нажми «Продолжить».')
            if not ch['enabled'] or not ch['target']: raise ValueError('Сначала подключи и включи канал.')
            sid=store.enqueue_search(cid); engine.wake.set(); audit('Поиск двух постов: #'+str(cid)); return jsonify(id=sid)
        if action=='stop_search': store.stop_search(cid)
        elif action=='connect':
            target=str(d.get('target','')).strip()
            if not re.fullmatch(r'(?:@[A-Za-z][A-Za-z0-9_]{3,31}|-100\d{5,18})',target): raise ValueError('Укажи @имя_канала или числовой ID -100…')
            checked=telegram.connect_channel(target)
            store.update_channel(cid,target=checked)
            store.put('publication_blocked:'+str(cid),'')
            engine.wake.set()
        elif action=='save':
            cfg=dict(ch['config']); fields={}
            if 'name' in d:
                name=str(d['name']).strip()
                if not 2<=len(name)<=80: raise ValueError('Название 2–80 знаков.')
                fields['name']=name
            if 'enabled' in d:
                if type(d['enabled']) is not bool: raise ValueError('enabled: ожидается переключатель.')
                if d['enabled'] and not ch['target']: raise ValueError('Сначала подключи Telegram-канал.')
                fields['enabled']=int(d['enabled'])
            incoming=d.get('config',{})
            if not isinstance(incoming,dict): raise ValueError('Некорректные настройки.')
            allowed={'topic','style','sources','include','exclude','mode','max_age_hours','auto_search','search_times','require_photo','web_search','budgets','auto_publish','publish_times','posts_per_run','post_gap_minutes','auto_text_fallback'}
            if set(incoming)-allowed: raise ValueError('Неизвестная настройка канала.')
            for key,value in incoming.items():
                if key in ('auto_search','require_photo','web_search','auto_publish','auto_text_fallback'):
                    if type(value) is not bool: raise ValueError('Нужен переключатель.')
                elif key in ('search_times','publish_times'):
                    value=parse_times(' '.join(value) if isinstance(value,list) else str(value))
                    if key=='search_times' and len(value)!=3: raise ValueError('Укажи три времени автоподбора, например 09:00 15:00 21:00.')
                elif key=='mode':
                    if value not in ('news','fashion','cinema'): raise ValueError('Неизвестный режим.')
                elif key=='max_age_hours': value=number(value,1,720)
                elif key=='posts_per_run': value=number(value,1,2)
                elif key=='post_gap_minutes': value=number(value,1,60)
                elif key in ('sources','include','exclude'):
                    if not isinstance(value,list) or len(value)>30 or any(not isinstance(x,str) or len(x)>500 for x in value): raise ValueError('До 30 строк, до 500 знаков каждая.')
                    value=[x.strip() for x in value if x.strip()]
                    if key=='sources': value=[public_url(x,resolve=False) for x in value]
                elif key=='budgets':
                    if not isinstance(value,dict) or len(value)>8: raise ValueError('Некорректные бюджеты.')
                    value={parse_times(k)[0]:number(v,100,1000000) for k,v in value.items()}
                elif not isinstance(value,str) or len(value)>3000: raise ValueError('Текст настройки до 3000 знаков.')
                cfg[key]=value
            store.update_channel(cid,config=cfg,**fields)
        else: raise ValueError('Неизвестное действие.')
        audit('Канал #'+str(cid)+': '+action)
        return jsonify(ok=True)

    @app.post('/api/posts/reject-batch')
    def reject_batch():
        ids=data().get('ids')
        if not isinstance(ids,list) or not 1<=len(ids)<=300 or any(type(x) is not int or x<=0 for x in ids):
            raise ValueError('Выбери от 1 до 300 постов.')
        ids=list(dict.fromkeys(ids)); changed=0
        with store.tx() as c:
            for pid in ids:
                row=c.execute("SELECT p.state,s.kind,s.id AS sid FROM candidates p JOIN slots s ON s.id=p.slot_id WHERE p.id=?",(pid,)).fetchone()
                if not row or row['state'] not in ('pending','draft','approved'):continue
                changed+=c.execute("UPDATE candidates SET state='rejected',scheduled_at=NULL,auto_queued=0 WHERE id=? AND state IN ('pending','draft','approved')",(pid,)).rowcount
                if row['kind']=='legacy':c.execute("UPDATE slots SET state='pending',selected=NULL WHERE id=? AND selected=?",(row['sid'],pid))
        audit('Массовое отклонение: '+str(changed))
        return jsonify(ok=True,changed=changed,skipped=len(ids)-changed)

    @app.post('/api/posts/<int:pid>')
    def post(pid):
        p=need_post(pid); d=data(); action=d.get('action')
        if action=='schedule':
            due=publication_time(str(d.get('time','')).replace('T',' '),time.time())
            store.schedule(pid,due)
        elif action=='approve':
            from .automation import AutoPublisher
            due=AutoPublisher(store).choose_time(p,time.time())
            store.schedule(pid,due,allow_text_fallback=True)
        elif action=='unschedule': store.unschedule(pid)
        elif action=='edit':
            body=d.get('body',p['body']); media=d.get('media',p['media'])
            if not isinstance(body,str) or not 1<=len(body)<=650: raise ValueError('Основной текст 1–650 знаков.')
            if not isinstance(media,str) or len(media)>2000: raise ValueError('Некорректная картинка.')
            if media!=p['media'] and media:
                if media.startswith('local:'): raise ValueError('Картинку нужно загрузить кнопкой «Загрузить фото».')
                public_url(media,resolve=False)
            check=dict(p,body=body,media=media)
            if utf16(render(check))>1000: raise ValueError('Подпись вместе со ссылками слишком длинная; сократи текст.')
            store.edit_candidate(pid,body=body,media=media)
        elif action=='restore':
            with store.tx() as c:
                changed=c.execute("UPDATE candidates SET state='pending',scheduled_at=NULL,auto_queued=0,publication_error='' WHERE id=? AND state='rejected'",(pid,)).rowcount
                if not changed:raise ValueError('Вернуть можно только отклонённый пост.')
        elif action in ('draft','reject'):
            with store.tx() as c:
                changed=c.execute("UPDATE candidates SET state=?,scheduled_at=NULL,auto_queued=0 WHERE id=? AND state IN ('pending','draft','approved')",('draft' if action=='draft' else 'rejected',pid)).rowcount
                if not changed: raise ValueError('Пост уже отправляется или обработан.')
                if p['batch_kind']=='legacy': c.execute("UPDATE slots SET state='pending',selected=NULL WHERE id=? AND selected=?",(p['slot_id'],pid))
        elif action=='verified':
            with store.tx() as c:
                row=c.execute("SELECT meta FROM candidates WHERE id=? AND state IN ('pending','draft')",(pid,)).fetchone()
                if not row: raise ValueError('Сначала сними пост с очереди.')
                meta=json.loads(row['meta'])
                if meta.get('mode')!='fashion': raise ValueError('Это не подборка одежды.')
                meta.update(unverified=False,manually_checked_at=int(time.time()))
                c.execute('UPDATE candidates SET meta=? WHERE id=?',(json.dumps(meta,ensure_ascii=False),pid))
        elif action=='resolve':
            sent=d.get('sent')
            if type(sent) is not bool: raise ValueError('Укажи результат проверки.')
            with store.tx() as c:
                changed=c.execute("UPDATE candidates SET state=?,scheduled_at=CASE WHEN ? THEN scheduled_at ELSE NULL END WHERE id=? AND state='unknown'",('sent' if sent else 'draft',sent,pid)).rowcount
                if not changed: raise ValueError('Нет неопределённой отправки.')
                c.execute("UPDATE publication_dispatches SET outcome=? WHERE candidate_id=? AND outcome='unknown'",('sent' if sent else 'not_sent',pid))
                if p['batch_kind']=='legacy': c.execute('UPDATE slots SET state=? WHERE id=?',('sent' if sent else 'expired',p['slot_id']))
                if not c.execute("SELECT 1 FROM candidates p JOIN slots s ON s.id=p.slot_id WHERE s.channel_id=? AND p.state='unknown'",(p['channel_id'],)).fetchone(): store.put('publication_blocked:'+str(p['channel_id']),'',c)
        else: raise ValueError('Неизвестное действие.')
        audit('Пост #'+str(pid)+': '+action)
        return jsonify(ok=True)

    @app.post('/api/posts/<int:pid>/image')
    def upload(pid):
        need_post(pid)
        from PIL import Image, ImageOps, UnidentifiedImageError
        file=request.files.get('image')
        if not file: raise ValueError('Выбери JPEG, PNG или WebP до 10 МБ.')
        raw=file.read(10*1024*1024+1)
        if len(raw)>10*1024*1024: raise ValueError('Максимальный размер — 10 МБ.')
        try:
            with Image.open(io.BytesIO(raw)) as img:
                if img.format not in ('JPEG','PNG','WEBP') or img.width*img.height>25000000: raise ValueError('Фото до 25 мегапикселей в JPEG, PNG или WebP.')
                img=ImageOps.exif_transpose(img).convert('RGB'); img.thumbnail((2400,2400))
                name=secrets.token_hex(16)+'.jpg'; path=media_root/name
                img.save(path,'JPEG',quality=90)
                path.chmod(0o600)
        except (UnidentifiedImageError,OSError,Image.DecompressionBombError): raise ValueError('Не удалось прочитать изображение.') from None
        try: store.edit_candidate(pid,media='local:'+name)
        except Exception:
            path.unlink(missing_ok=True); raise
        audit('Картинка поста #'+str(pid))
        return jsonify(ok=True)

    @app.get('/api/posts/<int:pid>/image')
    def picture(pid):
        p=need_post(pid); media=p['media']
        if media.startswith('local:') and re.fullmatch(r'local:[a-f0-9]{32}\.jpg',media):
            return send_file(media_root/media[6:],mimetype='image/jpeg')
        # File IDs are opaque; download only from the fixed Telegram file service.
        if media and not media.startswith(('http://','https://')):
            raw=telegram.file_bytes(media)
            return send_file(io.BytesIO(raw),mimetype='image/jpeg')
        return '',404

    @app.post('/api/limits')
    def limits():
        incoming=data(); current=store.get('limits')
        if not incoming or set(incoming)-(set(LIMIT_FIELDS)|{'budget_enforced'}): raise ValueError('Неизвестная настройка лимитов.')
        for key,value in incoming.items():
            if key=='budget_enforced':
                if type(value) is not bool: raise ValueError('Нужен переключатель режима бюджета.')
                continue
            _,low,high=LIMIT_FIELDS[key]; current[key]=number(value,low,high)
        store.put('limits',current)
        if 'budget_enforced' in incoming: store.put('budget_enforced',incoming['budget_enforced'])
        audit('Изменены лимиты ИИ')
        return jsonify(ok=True)

    @app.post('/api/model')
    def model():
        slug=data().get('model')
        known={x.get('slug') for x in store.get('model_catalog',[])}
        known.add(store.get('model'))
        if slug not in known or not slug: raise ValueError('Выбери модель из сохранённого каталога. Обновление: python -m smm model')
        store.put('model',slug); audit('Выбрана модель '+slug)
        return jsonify(ok=True)

    @app.post('/api/campaigns')
    def create_campaign():
        d=data(); item=manager.create(str(d.get('name','')).strip(),str(d.get('text','')),parse_targets(str(d.get('targets',''))))
        audit('Создана кампания '+item['id']); return jsonify(id=item['id'])

    @app.post('/api/campaigns/<cid>')
    def campaign(cid):
        d=data(); action=d.get('action'); manager.campaign(cid)
        if action=='start': manager.start(cid)
        elif action=='stop': manager.stop(cid)
        elif action=='delete': manager.delete(cid)
        elif action=='edit': manager.edit(cid,str(d.get('field','')),d.get('value'))
        else: raise ValueError('Неизвестное действие.')
        audit('Кампания '+cid+': '+action); return jsonify(ok=True)

    @app.post('/api/manager')
    def manager_action():
        d=data(); action=d.get('action')
        if action=='stop': manager.stop()
        elif action=='check_account': store.put('manager_check_account',True)
        elif action=='resolve':
            if type(d.get('sent')) is not bool: raise ValueError('Укажи результат проверки.')
            manager.resolve(number(d.get('id')),d['sent'])
        else: raise ValueError('Неизвестное действие.')
        audit('Менеджер: '+action); return jsonify(ok=True)

    @app.post('/api/partners')
    def partners():
        d=data(); action=d.get('action')
        if action=='filters':
            queries=[x.strip() for x in str(d.get('queries','')).splitlines() if x.strip()]
            if not 1<=len(queries)<=3 or any(len(x)>80 for x in queries): raise ValueError('От 1 до 3 тем поиска, каждая до 80 знаков.')
            low,high=number(d.get('min',0),0,1000000000),number(d.get('max',0),0,1000000000)
            if high and high<low: raise ValueError('Максимум меньше минимума.')
            old=store.get('manager_partner_filter',{}); old.update(queries=queries,min=low,max=high)
            store.put('manager_partner_filter',old)
        elif action=='search': manager.queue_search()
        elif action=='stop':
            with store.tx() as c:
                job=store.get('manager_search',{},c)
                if job.get('state') in ('queued','running'): store.put('manager_search',dict(job,state='cancelled'),c)
        elif action in ('favorite','hide'):
            key='favorite' if action=='favorite' else 'hidden'
            store.execute('UPDATE manager_channels SET '+key+'=1-'+key+' WHERE peer=?',(str(d.get('peer','')),))
        else: raise ValueError('Неизвестное действие.')
        audit('Партнёры: '+action); return jsonify(ok=True)

    @app.post('/api/team')
    def team():
        d=data(); actor=number(d.get('id')); action=d.get('action')
        if actor==store.get('owner'): raise ValueError('Основной владелец уже имеет доступ и не удаляется.')
        if action=='invite':
            code=secrets.token_urlsafe(18)
            store.put('team_invite',dict(user=actor,code=code,expires=time.time()+1800))
            audit('Приглашение совладельца '+str(actor)); return jsonify(command='/join '+code)
        elif action=='remove':
            with store.tx() as c:
                store.put('coowners',[x for x in store.get('coowners',[],c) if x!=actor],c)
                c.execute('DELETE FROM web_sessions WHERE actor=?',(actor,)); c.execute('DELETE FROM web_codes WHERE actor=?',(actor,))
        else: raise ValueError('Неизвестное действие.')
        audit('Отозван доступ '+str(actor)); return jsonify(ok=True)

    return app


def start_server(root, store, engine, auth, telegram, manager):
    from waitress import create_server
    app=create_app(root,store,engine,auth,telegram,manager)
    port=int(os.environ.get('PORT','8000'))
    server=create_server(app,host='0.0.0.0',port=port,threads=6,
                         max_request_body_size=11*1024*1024,channel_timeout=30,ident='SMM')
    thread=threading.Thread(target=server.run,name='web',daemon=True)
    thread.start()
    print(f'Веб-панель {__version__}: 0.0.0.0:{port}. Для входа отправь боту /web.',flush=True)
    return server
