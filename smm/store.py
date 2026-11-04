from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import DEFAULT_CHANNELS, DEFAULT_LIMITS, channel_defaults

TZ = ZoneInfo("Europe/Moscow")


def local_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).date().isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS channels(
              id INTEGER PRIMARY KEY, name TEXT NOT NULL, target TEXT, enabled INTEGER NOT NULL DEFAULT 0,
              config TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS slots(
              id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES channels(id),
              due INTEGER NOT NULL, label TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'new',
              attempts INTEGER NOT NULL DEFAULT 0, selected INTEGER, error TEXT NOT NULL DEFAULT '',
              UNIQUE(channel_id,due));
            CREATE TABLE IF NOT EXISTS candidates(
              id INTEGER PRIMARY KEY, slot_id INTEGER NOT NULL REFERENCES slots(id),
              title TEXT NOT NULL, body TEXT NOT NULL, media TEXT NOT NULL DEFAULT '',
              meta TEXT NOT NULL, event_key TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'pending',
              created INTEGER NOT NULL, sent_message_id INTEGER, notified INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS articles(
              id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL, summary TEXT NOT NULL,
              source TEXT NOT NULL, published INTEGER NOT NULL, image TEXT NOT NULL DEFAULT '',
              fingerprint TEXT NOT NULL, fetched INTEGER NOT NULL, text TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS candidate_articles(
              candidate_id INTEGER NOT NULL REFERENCES candidates(id), article_id INTEGER NOT NULL REFERENCES articles(id),
              PRIMARY KEY(candidate_id,article_id));
            CREATE TABLE IF NOT EXISTS feed_cache(
              url TEXT PRIMARY KEY, etag TEXT, modified TEXT, checked INTEGER NOT NULL, error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS ledger(
              id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, slot_id INTEGER NOT NULL,
              started INTEGER NOT NULL, day TEXT NOT NULL, reserved INTEGER NOT NULL,
              input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
              charged INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'reserved',
              error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS notices(
              id INTEGER PRIMARY KEY, notice_key TEXT UNIQUE NOT NULL, text TEXT NOT NULL,
              delivered INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, last_attempt INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sessions(owner INTEGER PRIMARY KEY, action TEXT NOT NULL, payload TEXT NOT NULL, expires INTEGER NOT NULL);
            """)
            if not c.in_transaction:
                c.execute("BEGIN IMMEDIATE")
            for table,fields in {'notices':{'created':'INTEGER NOT NULL DEFAULT 0'},'slots':{'kind':"TEXT NOT NULL DEFAULT 'legacy'",'requested_at':'INTEGER NOT NULL DEFAULT 0',
                                         'search_cancelled':'INTEGER NOT NULL DEFAULT 0','automatic':'INTEGER NOT NULL DEFAULT 0'},
                                 'candidates':{'scheduled_at':'INTEGER','publication_error':"TEXT NOT NULL DEFAULT ''"}}.items():
                columns={r['name'] for r in c.execute('PRAGMA table_info('+table+')').fetchall()}
                for name,definition in fields.items():
                    if name not in columns: c.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+definition)
            c.execute('DROP INDEX IF EXISTS one_approved')
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_approved_legacy ON candidates(slot_id) WHERE state='approved' AND scheduled_at IS NULL")
            c.execute('CREATE INDEX IF NOT EXISTS publication_times ON candidates(state,scheduled_at)')
            if not c.execute("SELECT 1 FROM settings WHERE key='initialized'").fetchone():
                for k, v in {"initialized": True, "limits": DEFAULT_LIMITS, "paused": True, "pause_reason": "Первоначальная настройка", "owner": 0, "offset": 0, "model": ""}.items():
                    self.put(k, v, c)
                for item in DEFAULT_CHANNELS:
                    cfg = channel_defaults()
                    cfg.update({k: v for k, v in item.items() if k != "name"})
                    c.execute("INSERT INTO channels(name,config) VALUES(?,?)", (item["name"], json.dumps(cfg, ensure_ascii=False)))
            # Apply the requested limits to an existing installation once. Later
            # user edits (including disabling discovery) must survive restarts.
            if self.get("fashion_upgrade", 0, c) < 1:
                limits = self.get("limits", {}, c)
                for key, default in DEFAULT_LIMITS.items():
                    if key in ("daily_tokens", "rolling_tokens", "channel_daily_tokens", "daily_requests", "hourly_requests"):
                        limits[key] = max(limits.get(key, 0), default)
                    else:
                        limits.setdefault(key, default)
                self.put("limits", limits, c)
                for row in c.execute("SELECT id,config FROM channels").fetchall():
                    cfg = json.loads(row["config"])
                    if cfg.get("mode") == "fashion":
                        cfg["web_search"] = True
                        c.execute("UPDATE channels SET config=? WHERE id=?", (json.dumps(cfg, ensure_ascii=False), row["id"]))
                self.put("fashion_upgrade", 1, c)
                self.notice("fashion-upgrade:1", "🔎 Автопоиск WB/Ozon включён для каналов одежды; ручной каталог необязателен. Общие ограничения — в «Лимитах». Модель и счётчики сохранены. Поиск зависит от доступа модели; генерация моделей в одежде через подписку недоступна.", c)
            if not self.get('fashion_optimized_v2',False,c):
                limits=self.get('limits',{},c)
                for key in ('fashion_steps','fashion_web_calls','fashion_output_bytes','fashion_seconds'):
                    limits[key]=min(limits.get(key,DEFAULT_LIMITS[key]),DEFAULT_LIMITS[key])
                limits.setdefault('fashion_slot_tokens',DEFAULT_LIMITS['fashion_slot_tokens'])
                self.put('limits',limits,c)
                self.put('fashion_optimized_v2',True,c)
                self.notice('fashion-optimized:2','🧥 Подбор одежды оптимизирован: готовый образ за один запрос, максимум одно исправление. Запасные образы сохраняются на сутки. Старый расход, выбранная модель и общие лимиты сохранены; новых запросов по одной заявке — не больше двух.',c)
            if not self.get('manual_publication_v1',False,c):
                for row in c.execute('SELECT id,config FROM channels').fetchall():
                    cfg=json.loads(row['config'])
                    cfg.update(publication_mode='manual_time',auto_search=True)
                    cfg.setdefault('search_times',['09:00','15:00','21:00'])
                    if cfg.get('mode')=='fashion':
                        for label in cfg['search_times']: cfg.setdefault('budgets',{}).setdefault(label,10000)
                    c.execute('UPDATE channels SET config=? WHERE id=?',(json.dumps(cfg,ensure_ascii=False),row['id']))
                # Move old unscheduled alternatives into durable batches while
                # preserving every already-approved legacy publication.
                old_slots=c.execute("SELECT DISTINCT s.* FROM slots s JOIN candidates p ON p.slot_id=s.id WHERE s.kind='legacy' AND p.state IN ('pending','draft')").fetchall()
                for old in old_slots:
                    deadline=int(time.time())+2400
                    while c.execute('SELECT 1 FROM slots WHERE channel_id=? AND due=?',(old['channel_id'],deadline)).fetchone(): deadline+=1
                    new=c.execute("INSERT INTO slots(channel_id,due,label,state,kind,requested_at) VALUES(?,?,?,'pending','batch',?)",(old['channel_id'],deadline,old['label'],int(time.time()))).lastrowid
                    c.execute("UPDATE candidates SET slot_id=? WHERE slot_id=? AND state IN ('pending','draft')",(new,old['id']))
                    if not c.execute('SELECT 1 FROM candidates WHERE slot_id=?',(old['id'],)).fetchone():
                        c.execute("UPDATE slots SET state='cancelled' WHERE id=? AND state NOT IN ('sent','publishing','unknown')",(old['id'],))
                self.put('manual_publication_v1',True,c)
                self.notice('manual-publication:1','📅 Время публикации выбираешь ты для каждого поста. Автоподбор: до двух постов в 09:00, 15:00 и 21:00 МСК. Для каждого канала есть «Найти 2 сейчас» и переключатель автоподбора. Старые одобренные публикации сохранены.',c)

            if not self.get('sources_v22',False,c):
                from .source_presets import PRESETS, preset_for
                for row in c.execute('SELECT id,name,config FROM channels').fetchall():
                    cfg=json.loads(row['config']); kind=preset_for(row['name'],cfg)
                    if kind:
                        cfg['sources']=list(dict.fromkeys(cfg.get('sources',[])+PRESETS[kind]))[:30]
                        c.execute('UPDATE channels SET config=? WHERE id=?',(json.dumps(cfg,ensure_ascii=False),row['id']))
                self.put('sources_v22',True,c)
                self.notice('sources:22','Добавлены источники для финансов, футбола, ИИ и кино. Свои ссылки сохранены. В каналах видны результаты проверки источников.',c)
            if self.get('fashion_unmetered',None,c) is None:
                self.put('fashion_unmetered',True,c)
                limits=self.get('limits',{},c)
                limits['fashion_web_calls']=max(limits.get('fashion_web_calls',3),6)
                limits['fashion_seconds']=max(limits.get('fashion_seconds',120),180)
                self.put('limits',limits,c)
            if self.get('budget_enforced',None,c) is None:
                self.put('budget_enforced',True,c)
                self.notice('budget-mode:22','Для «Собрано» внутренний учёт токенов и бюджетные остановки отключены. Остальные каналы используют свои лимиты; режим предупреждений доступен в разделе «Расход».',c)

    @contextlib.contextmanager
    def tx(self):
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            if c.in_transaction:
                c.commit()
        except BaseException:
            if c.in_transaction:
                c.rollback()
            raise
        finally:
            c.close()

    def get(self, key, default=None, c=None):
        if c is None:
            with self.tx() as conn:
                return self.get(key, default, conn)
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value, c=None):
        if c is None:
            with self.tx() as conn:
                return self.put(key, value, conn)
        c.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value, ensure_ascii=False)))

    def rows(self, sql, params=()):
        with self.tx() as c:
            return [dict(x) for x in c.execute(sql, params).fetchall()]

    def execute(self, sql, params=()):
        with self.tx() as c:
            return c.execute(sql, params).lastrowid

    def channels(self):
        rows = self.rows("SELECT * FROM channels ORDER BY id")
        for r in rows:
            r["config"] = json.loads(r["config"])
        return rows

    def channel(self, cid):
        return next((r for r in self.channels() if r["id"] == cid), None)

    def add_channel(self, name, config=None):
        return self.execute("INSERT INTO channels(name,config) VALUES(?,?)",
                            (name, json.dumps(config or channel_defaults(), ensure_ascii=False)))

    def update_channel(self, cid, config=None, **fields):
        if set(fields) - {"name", "target", "enabled"}:
            raise ValueError("Неизвестное поле канала.")
        with self.tx() as c:
            previous=c.execute('SELECT target,config FROM channels WHERE id=?',(cid,)).fetchone()
            old_cfg=json.loads(previous['config']) if previous else {}
            changed_keys={k for k in set(old_cfg)|set(config or {}) if old_cfg.get(k)!=(config or {}).get(k)} if config is not None else set()
            if config is not None:
                c.execute("UPDATE channels SET config=? WHERE id=?", (json.dumps(config, ensure_ascii=False), cid))
            for k, v in fields.items():
                c.execute(f"UPDATE channels SET {k}=? WHERE id=?", (v, cid))
            # Search timing alone never revokes an existing publication.
            if changed_keys-{'auto_search','search_times'} or fields:
                c.execute("UPDATE candidates SET state='draft' WHERE slot_id IN (SELECT id FROM slots WHERE channel_id=? AND kind='legacy' AND state NOT IN ('sent','publishing','unknown')) AND state IN ('pending','approved')", (cid,))
                c.execute("UPDATE slots SET state='cancelled',error='Настройки канала изменены' WHERE channel_id=? AND kind='legacy' AND state NOT IN ('sent','publishing','unknown')", (cid,))
            if changed_keys-{'auto_search','search_times'}:
                c.execute("UPDATE slots SET state='cancelled',search_cancelled=1,error='Фильтры изменены' WHERE channel_id=? AND kind='batch' AND state IN ('new','preparing')",(cid,))
            if 'target' in fields and previous and previous['target']!=fields['target']:
                c.execute("UPDATE candidates SET state='pending',scheduled_at=NULL WHERE state='approved' AND slot_id IN (SELECT id FROM slots WHERE channel_id=? AND kind='batch')",(cid,))

    def notice(self, key, text, c=None):
        if c is None:
            with self.tx() as conn:
                return self.notice(key, text, conn)
        c.execute("INSERT OR IGNORE INTO notices(notice_key,text,created) VALUES(?,?,?)", (key, text, int(time.time())))

    def halt(self, reason, c=None):
        if c is None:
            with self.tx() as conn:
                return self.halt(reason, conn)
        was_paused = self.get("paused", True, c)
        old_reason = self.get("pause_reason", "", c)
        self.put("paused", True, c)
        self.put("pause_reason", reason[:500], c)
        if not was_paused or old_reason != reason[:500]:
            self.notice(f"halt:{int(time.time()*1000)}", "🛑 Аварийная остановка\n" + reason[:500] + "\nНовых запросов к ИИ и публикаций не будет. Проверь «Лимиты» и «Очередь». Возобновление — только твоей кнопкой.", c)

    def pair_code(self):
        code = secrets.token_hex(4)
        self.put("pair", {"hash": hashlib.sha256(code.encode()).hexdigest(), "expires": int(time.time()) + 1800})
        return code

    def pair(self, owner, code):
        with self.tx() as c:
            if self.get("owner", 0, c):
                return False
            p = self.get("pair", {}, c)
            got = hashlib.sha256(code.strip().encode()).hexdigest()
            if p.get("expires", 0) < time.time() or not secrets.compare_digest(p.get("hash", ""), got):
                return False
            self.put("owner", owner, c)
            self.put("pair", {}, c)
            return True

    def session(self, owner, action=None, payload=None):
        with self.tx() as c:
            if action is None:
                r = c.execute("SELECT * FROM sessions WHERE owner=? AND expires>?", (owner, int(time.time()))).fetchone()
                return dict(r) if r else None
            c.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)", (owner, action, json.dumps(payload or {}), int(time.time()) + 1800))

    def clear_session(self, owner):
        self.execute("DELETE FROM sessions WHERE owner=?", (owner,))

    def slot(self, sid):
        rows = self.rows("SELECT * FROM slots WHERE id=?", (sid,))
        return rows[0] if rows else None

    def candidate(self, pid):
        rows = self.rows("""SELECT p.*,COALESCE(p.scheduled_at,s.due) AS due,s.kind AS batch_kind,s.channel_id,s.label,s.state AS slot_state,ch.name AS channel_name,ch.target,ch.enabled,ch.config
                           FROM candidates p JOIN slots s ON s.id=p.slot_id JOIN channels ch ON ch.id=s.channel_id WHERE p.id=?""", (pid,))
        if not rows:
            return None
        p = rows[0]
        p["meta"], p["config"] = json.loads(p["meta"]), json.loads(p["config"])
        return p

    def add_candidates(self, sid, options, now):
        with self.tx() as c:
            slot = c.execute("SELECT * FROM slots WHERE id=?", (sid,)).fetchone()
            if not slot or (slot['state']!='preparing' and not (slot['kind']=='batch' and slot['state']=='cancelled')):
                raise ValueError("Подготовка отменена; результат не будет опубликован.")
            ids = []
            for p in options[:2]:
                cur = c.execute("INSERT INTO candidates(slot_id,title,body,media,meta,event_key,created,state) VALUES(?,?,?,?,?,?,?,?)",
                                (sid, p["title"], p["body"], p.get("media", ""), json.dumps(p.get("meta", {}), ensure_ascii=False), p.get("event_key", ""), int(now),'draft' if slot['state']=='cancelled' else 'pending'))
                ids.append(cur.lastrowid)
                for aid in p.get("article_ids", []):
                    c.execute("INSERT INTO candidate_articles VALUES(?,?)", (cur.lastrowid, aid))
            c.execute("UPDATE slots SET state='pending',error='' WHERE id=? AND state='preparing'", (sid,))
            return ids

    def approve(self, pid, now):
        with self.tx() as c:
            kind=c.execute('SELECT s.kind FROM candidates p JOIN slots s ON s.id=p.slot_id WHERE p.id=?',(pid,)).fetchone()
            if kind and kind['kind']=='batch': raise ValueError('Выбери время публикации этого поста.')
            p = c.execute("""SELECT p.*,s.due,s.state AS ss,s.channel_id,ch.enabled,ch.target,ch.config
                             FROM candidates p JOIN slots s ON s.id=p.slot_id JOIN channels ch ON ch.id=s.channel_id WHERE p.id=?""", (pid,)).fetchone()
            if not p or p["state"] not in ("pending", "draft"):
                raise ValueError("Этот вариант уже обработан.")
            if p["due"] <= now:
                raise ValueError("Время выхода прошло. Автоматического переноса нет.")
            if p["ss"] not in ("pending", "queued") or not p["enabled"] or not p["target"]:
                raise ValueError("Канал выключен, не подключён или заявка отменена.")
            if c.execute("SELECT 1 FROM candidates WHERE slot_id=? AND state='approved'", (p["slot_id"],)).fetchone():
                raise ValueError("На это время уже выбран другой пост.")
            cfg, meta = json.loads(p["config"]), json.loads(p["meta"])
            if cfg["require_photo"] and not p["media"]:
                raise ValueError("Сначала прикрепи и проверь картинку.")
            if meta.get("demo"):
                raise ValueError("Демонстрационные материалы не публикуются.")
            if meta.get("unverified"):
                raise ValueError("Не удалось подтвердить товары/цены. Исправь подборку вручную.")
            c.execute("UPDATE candidates SET state='draft' WHERE slot_id=? AND id<>? AND state='pending'", (p["slot_id"], pid))
            c.execute("UPDATE candidates SET state='approved' WHERE id=?", (pid,))
            c.execute("UPDATE slots SET state='queued',selected=? WHERE id=?", (pid, p["slot_id"]))

    def enqueue_search(self,cid,now=None,automatic=False,label=None):
        now=int(time.time() if now is None else now)
        with self.tx() as c:
            row=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
            if not row: raise ValueError('Канал не найден.')
            cfg=json.loads(row['config'])
            if automatic:
                marker=f'auto-batch:{cid}:{now}'
                if self.get(marker,False,c): return None
                self.put(marker,True,c)
            existing=c.execute("SELECT id FROM slots WHERE channel_id=? AND kind='batch' AND state IN ('new','preparing') ORDER BY id LIMIT 1",(cid,)).fetchone()
            if existing: return existing['id']
            labels=sorted(cfg.get('search_times',['09:00','15:00','21:00']))
            if not label:
                clock=datetime.fromtimestamp(now,TZ).strftime('%H:%M')
                label=next((s for s in reversed(labels) if s<=clock),labels[0])
            deadline=now+2400
            while c.execute('SELECT 1 FROM slots WHERE channel_id=? AND due=?',(cid,deadline)).fetchone(): deadline+=1
            return c.execute("INSERT INTO slots(channel_id,due,label,kind,requested_at,automatic) VALUES(?,?,?,'batch',?,?)",(cid,deadline,label,now,int(automatic))).lastrowid

    def stop_search(self,cid):
        with self.tx() as c:
            c.execute("UPDATE slots SET state='cancelled',search_cancelled=1,error='Поиск остановлен владельцем' WHERE channel_id=? AND kind='batch' AND state IN ('new','preparing')",(cid,))

    def schedule(self,pid,due,now=None):
        now=time.time() if now is None else now
        if type(due) is not int or not now+10<=due<=now+30*86400:
            raise ValueError('Выбери будущее время в пределах 30 дней.')
        with self.tx() as c:
            p=c.execute('''SELECT p.*,s.kind,s.channel_id,ch.config,ch.enabled,ch.target
                FROM candidates p JOIN slots s ON s.id=p.slot_id JOIN channels ch ON ch.id=s.channel_id WHERE p.id=?''',(pid,)).fetchone()
            if not p or p['kind']!='batch' or p['state'] not in ('pending','draft','approved'):
                raise ValueError('Этот пост уже отправлен, отправляется или требует проверки результата.')
            if not p['enabled'] or not p['target']: raise ValueError('Сначала подключи и включи канал.')
            cfg,meta=json.loads(p['config']),json.loads(p['meta'])
            if cfg['require_photo'] and not p['media']: raise ValueError('Сначала прикрепи и проверь картинку.')
            if meta.get('demo') or meta.get('unverified'): raise ValueError('Сначала проверь материалы, товары и цены.')
            conflict=c.execute('''SELECT p.id FROM candidates p JOIN slots s ON s.id=p.slot_id
                WHERE s.channel_id=? AND p.id<>? AND p.state IN ('approved','publishing')
                AND COALESCE(p.scheduled_at,s.due)=?''',(p['channel_id'],pid,due)).fetchone()
            if conflict: raise ValueError('У канала уже есть пост на это время. Выбери другое время.')
            c.execute("UPDATE candidates SET scheduled_at=?,state='approved',publication_error='' WHERE id=?",(due,pid))
        return due

    def unschedule(self,pid):
        with self.tx() as c:
            row=c.execute('SELECT p.state,s.kind FROM candidates p JOIN slots s ON s.id=p.slot_id WHERE p.id=?',(pid,)).fetchone()
            if not row or row['kind']!='batch' or row['state'] not in ('pending','draft','approved'):
                raise ValueError('Эту публикацию уже нельзя снять с очереди.')
            c.execute("UPDATE candidates SET state='pending',scheduled_at=NULL,publication_error='' WHERE id=?",(pid,))

    def edit_candidate(self, pid, *, body=None, media=None):
        with self.tx() as c:
            p = c.execute("SELECT * FROM candidates WHERE id=?", (pid,)).fetchone()
            if not p or p["state"] in ("sent", "publishing", "unknown"):
                raise ValueError("Этот пост уже отправлен или его отправка требует проверки.")
            slot = c.execute("SELECT state,kind FROM slots WHERE id=?", (p["slot_id"],)).fetchone()
            if slot['kind']=='batch':
                if body is not None: c.execute("UPDATE candidates SET body=? WHERE id=?",(body,pid))
                if media is not None: c.execute("UPDATE candidates SET media=? WHERE id=?",(media,pid))
                c.execute("UPDATE candidates SET state='pending',scheduled_at=NULL,publication_error='' WHERE id=?",(pid,))
                return
            if slot["state"] not in ("pending", "queued"):
                raise ValueError("Эта заявка закрыта.")
            if body is not None:
                c.execute("UPDATE candidates SET body=?,state='pending' WHERE id=?", (body, pid))
            if media is not None:
                c.execute("UPDATE candidates SET media=?,state='pending' WHERE id=?", (media, pid))
            c.execute("UPDATE slots SET state='pending',selected=NULL WHERE id=?", (p["slot_id"],))
            c.execute("UPDATE candidates SET state='pending' WHERE slot_id=? AND state='approved'", (p["slot_id"],))

    def recover(self):
        with self.tx() as c:
            unknown = c.execute("SELECT count(*) FROM ledger WHERE state='reserved'").fetchone()[0]
            c.execute("UPDATE ledger SET state='uncertain',charged=reserved,error='Перезапуск во время запроса' WHERE state='reserved'")
            sending = c.execute("SELECT count(*) FROM slots WHERE state='publishing'").fetchone()[0]
            sending_posts=c.execute("SELECT COUNT(*) FROM candidates p JOIN slots s ON s.id=p.slot_id WHERE p.state='publishing' AND s.kind='batch'").fetchone()[0]
            c.execute("UPDATE slots SET state='unknown',error='Перезапуск во время отправки; проверь канал' WHERE state='publishing'")
            c.execute("UPDATE candidates SET state='unknown' WHERE state='publishing'")
            c.execute("UPDATE slots SET state='failed',error='Подготовка прервана; без автоматического повтора' WHERE state='preparing'")
            if unknown or sending or sending_posts:
                self.halt(f"После перезапуска: незавершённых запросов ИИ — {unknown}, отправок с неизвестным результатом — {sending+sending_posts}. Они не будут повторены.", c)
