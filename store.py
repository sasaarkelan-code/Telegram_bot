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
            CREATE UNIQUE INDEX IF NOT EXISTS one_approved ON candidates(slot_id) WHERE state='approved';
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
            if not c.execute("SELECT 1 FROM settings WHERE key='initialized'").fetchone():
                for k, v in {"initialized": True, "limits": DEFAULT_LIMITS, "paused": True, "pause_reason": "Первоначальная настройка", "owner": 0, "offset": 0, "model": ""}.items():
                    self.put(k, v, c)
                for item in DEFAULT_CHANNELS:
                    cfg = channel_defaults()
                    cfg.update({k: v for k, v in item.items() if k != "name"})
                    c.execute("INSERT INTO channels(name,config) VALUES(?,?)", (item["name"], json.dumps(cfg, ensure_ascii=False)))

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
            if config is not None:
                c.execute("UPDATE channels SET config=? WHERE id=?", (json.dumps(config, ensure_ascii=False), cid))
            for k, v in fields.items():
                c.execute(f"UPDATE channels SET {k}=? WHERE id=?", (v, cid))
            # A changed profile never keeps an old publication approval.
            c.execute("UPDATE candidates SET state='draft' WHERE slot_id IN (SELECT id FROM slots WHERE channel_id=? AND state NOT IN ('sent','publishing','unknown')) AND state IN ('pending','approved')", (cid,))
            c.execute("UPDATE slots SET state='cancelled',error='Настройки канала изменены' WHERE channel_id=? AND state NOT IN ('sent','publishing','unknown')", (cid,))

    def notice(self, key, text, c=None):
        if c is None:
            with self.tx() as conn:
                return self.notice(key, text, conn)
        c.execute("INSERT OR IGNORE INTO notices(notice_key,text) VALUES(?,?)", (key, text))

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
        rows = self.rows("""SELECT p.*,s.due,s.channel_id,s.label,s.state AS slot_state,ch.name AS channel_name,ch.target,ch.enabled,ch.config
                           FROM candidates p JOIN slots s ON s.id=p.slot_id JOIN channels ch ON ch.id=s.channel_id WHERE p.id=?""", (pid,))
        if not rows:
            return None
        p = rows[0]
        p["meta"], p["config"] = json.loads(p["meta"]), json.loads(p["config"])
        return p

    def add_candidates(self, sid, options, now):
        with self.tx() as c:
            slot = c.execute("SELECT * FROM slots WHERE id=?", (sid,)).fetchone()
            if not slot or slot["state"] != "preparing":
                raise ValueError("Подготовка отменена; результат не будет опубликован.")
            ids = []
            for p in options:
                cur = c.execute("INSERT INTO candidates(slot_id,title,body,media,meta,event_key,created) VALUES(?,?,?,?,?,?,?)",
                                (sid, p["title"], p["body"], p.get("media", ""), json.dumps(p.get("meta", {}), ensure_ascii=False), p.get("event_key", ""), int(now)))
                ids.append(cur.lastrowid)
                for aid in p.get("article_ids", []):
                    c.execute("INSERT INTO candidate_articles VALUES(?,?)", (cur.lastrowid, aid))
            c.execute("UPDATE slots SET state='pending',error='' WHERE id=?", (sid,))
            return ids

    def approve(self, pid, now):
        with self.tx() as c:
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

    def edit_candidate(self, pid, *, body=None, media=None):
        with self.tx() as c:
            p = c.execute("SELECT * FROM candidates WHERE id=?", (pid,)).fetchone()
            if not p or p["state"] in ("sent", "publishing", "unknown"):
                raise ValueError("Этот пост уже отправлен или его отправка требует проверки.")
            slot = c.execute("SELECT state FROM slots WHERE id=?", (p["slot_id"],)).fetchone()
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
            c.execute("UPDATE slots SET state='unknown',error='Перезапуск во время отправки; проверь канал' WHERE state='publishing'")
            c.execute("UPDATE candidates SET state='unknown' WHERE state='publishing'")
            c.execute("UPDATE slots SET state='failed',error='Подготовка прервана; без автоматического повтора' WHERE state='preparing'")
            if unknown or sending:
                self.halt(f"После перезапуска: незавершённых запросов ИИ — {unknown}, отправок с неизвестным результатом — {sending}. Они не будут повторены.", c)
