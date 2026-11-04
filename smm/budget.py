from __future__ import annotations

import time
import json
from .store import Store, local_day


class BudgetStop(RuntimeError):
    pass


class Budget:
    def __init__(self, store: Store):
        self.store = store

    def usage(self, now=None, c=None):
        now = time.time() if now is None else now
        if c is None:
            with self.store.tx() as conn:
                return self.usage(now, conn)
        rows = c.execute("SELECT * FROM ledger WHERE day=? OR started>?", (local_day(now), int(now)-18000)).fetchall()
        charge = lambda r: r["reserved"] if r["state"] == "reserved" else r["charged"]
        day = [r for r in rows if r["day"] == local_day(now)]
        rolling = [r for r in rows if r["started"] > now-18000]
        return dict(tokens=sum(charge(r) for r in day), rolling=sum(charge(r) for r in rolling),
                    requests=len(day), hourly=sum(r["started"] > now-3600 for r in rows),
                    uncertain=sum(r["state"] in ("uncertain", "reserved") for r in day))

    def reserve(self, channel_id, slot_id, input_bytes, now=None):
        now = time.time() if now is None else now
        error, rid = None, None
        with self.store.tx() as c:
            limits = self.store.get("limits", {}, c)
            if self.store.get("paused", True, c):
                raise BudgetStop("Бот на паузе.")
            if input_bytes + 2048 > limits["request_reserve"]:
                raise BudgetStop("Контекст слишком большой; запрос к ИИ не отправлен.")
            channel_cfg=c.execute('SELECT config FROM channels WHERE id=?',(channel_id,)).fetchone()
            fashion=bool(channel_cfg and json.loads(channel_cfg['config']).get('mode')=='fashion')
            if fashion:
                if self.store.get('fashion_spend_closed:'+str(slot_id),False,c):
                    raise BudgetStop('Расход этой подборки уже выше ожидаемого. Новые запросы по ней закрыты; готовый результат сохраняется.')
                spent=c.execute("SELECT COUNT(*),COALESCE(SUM(CASE WHEN state='reserved' THEN reserved ELSE charged END),0) FROM ledger WHERE channel_id=? AND slot_id=?",(channel_id,slot_id)).fetchone()
                if spent[0]>=min(2,limits.get('fashion_steps',2)):
                    raise BudgetStop('Достигнут предел запросов этой подборки одежды. Нового запроса не будет; другие каналы продолжают работать.')
                if spent[1]+limits['request_reserve']>limits.get('fashion_slot_tokens',24000):
                    raise BudgetStop('Не хватает бюджета одной подборки на новый запрос с резервом. Сохранённые образы доступны; другие каналы продолжают работать.')
            u = self.usage(now, c)
            channel = c.execute("SELECT COALESCE(SUM(CASE WHEN state='reserved' THEN reserved ELSE charged END),0) FROM ledger WHERE day=? AND channel_id=?", (local_day(now), channel_id)).fetchone()[0]
            if fashion and channel+limits['request_reserve']>limits['channel_daily_tokens']:
                raise BudgetStop('Достигнут дневной предел ИИ канала одежды с резервом. Другие каналы не остановлены.')
            checks = [
                (u["tokens"] + limits["request_reserve"] > limits["daily_tokens"], "Достигнут дневной предел токенов с учётом резерва."),
                (u["rolling"] + limits["request_reserve"] > limits["rolling_tokens"], "Достигнут предел токенов за последние 5 часов с учётом резерва."),
                (channel + limits["request_reserve"] > limits["channel_daily_tokens"], "Достигнут дневной предел токенов этого канала с учётом резерва."),
                (u["requests"] >= limits["daily_requests"], "Достигнут дневной предел запросов."),
                (u["hourly"] >= limits["hourly_requests"], "Слишком много запросов за час."),
            ]
            error = next((msg for condition, msg in checks if condition), None)
            if error:
                self.store.halt(error, c)
            else:
                cur = c.execute("INSERT INTO ledger(channel_id,slot_id,started,day,reserved) VALUES(?,?,?,?,?)",
                                (channel_id, slot_id, int(now), local_day(now), limits["request_reserve"]))
                rid = cur.lastrowid
        if error:
            raise BudgetStop(error)
        return rid

    def finish(self, rid, usage=None, error="", now=None):
        now = time.time() if now is None else now
        with self.store.tx() as c:
            row = c.execute("SELECT * FROM ledger WHERE id=?", (rid,)).fetchone()
            if not row or row["state"] != "reserved":
                return
            valid = isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0 for k in ("input_tokens", "output_tokens"))
            if not valid:
                c.execute("UPDATE ledger SET charged=reserved,state='uncertain',error=? WHERE id=?", ((error or "Нет достоверного usage")[:200], rid))
                self.store.halt("Расход последнего запроса неизвестен. Резерв сохранён, новые операции остановлены.", c)
                return
            total = usage["input_tokens"] + usage["output_tokens"]
            c.execute("UPDATE ledger SET input_tokens=?,output_tokens=?,charged=?,state='done',error=? WHERE id=?",
                      (usage["input_tokens"], usage["output_tokens"], total, error[:200], rid))
            self.store.put("ai_errors", self.store.get("ai_errors", 0, c)+1 if error else 0, c)
            limits = self.store.get("limits", {}, c)
            u = self.usage(now, c)
            channel_cfg=c.execute('SELECT config FROM channels WHERE id=?',(row['channel_id'],)).fetchone()
            fashion=bool(channel_cfg and json.loads(channel_cfg['config']).get('mode')=='fashion')
            if fashion:
                slot_total=c.execute('SELECT COALESCE(SUM(charged),0) FROM ledger WHERE channel_id=? AND slot_id=?',(row['channel_id'],row['slot_id'])).fetchone()[0]
                if total>row['reserved'] or slot_total>=limits.get('fashion_slot_tokens',24000):
                    self.store.put('fashion_spend_closed:'+str(row['slot_id']),True,c)
                    self.store.notice('fashion-spend:'+str(row['slot_id']),'🧥 Подборка одежды достигла ожидаемого расхода. Новые запросы по ней закрыты; полученный полный образ сохранится. Фактический расход учтён, другие каналы работают в своих общих лимитах.',c)
            if u["tokens"] >= limits["daily_tokens"] or u["rolling"] >= limits["rolling_tokens"]:
                self.store.halt("Достигнут предел токенов.", c)
            elif total > row["reserved"] and not fashion:
                self.store.halt("Один запрос превысил свой резерв токенов. Новые операции остановлены.", c)
            elif self.store.get("ai_errors", 0, c) >= 2:
                self.store.halt("Два подряд ошибочных ответа ИИ. Автоматических исправлений и повторов нет.", c)
            elif u["tokens"] >= limits["daily_tokens"] * .8:
                self.store.notice("budget-warning:"+local_day(now), f"⚠️ Использовано {u['tokens']} из {limits['daily_tokens']} токенов дневного бюджета бота. Это журнал бота, а не остаток всей подписки.", c)
