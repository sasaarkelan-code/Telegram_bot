from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from smm.ai import AI, AIError, parse_sse
from smm.auth import AuthError, b64, validate_id_token
from smm.budget import Budget, BudgetStop
from smm.config import DEFAULT_LIMITS
from smm.editor import fashion_options, image_prompt, news_options, render, utf16, validate_product
from smm.engine import Engine
from smm.net import canonical, public_url
from smm.sources import Sources, parse_feed
from smm.store import Store, TZ
from smm.ui import UI, parse_times


class FakeTelegram:
    def __init__(self):
        self.messages, self.published = [], []
        self.fail_publish = False

    def send(self, owner, text, markup=None):
        self.messages.append((owner, text, markup))
        return {"message_id": len(self.messages)}

    def photo(self, owner, media, caption):
        return self.send(owner, caption)

    def call(self, method, **kw):
        return {}

    def publish(self, target, text, media):
        self.published.append((target, text, media))
        if self.fail_publish:
            raise TimeoutError()
        return {"message_id": 123}


class FakeAuth:
    def access(self):
        return "local-test-token-not-real"

    def saved(self):
        return {"test": True}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/"test.sqlite")
        self.store.put("paused", False)
        self.store.put("model", "test-model")
        self.now = int(time.time())
        self.tg = FakeTelegram()

    def tearDown(self):
        self.tmp.cleanup()

    def channel(self, cid=1):
        self.store.update_channel(cid, enabled=True, target="-100123456789")
        return self.store.channel(cid)

    def slot(self, due=None, cid=1, count=2):
        self.channel(cid)
        sid = self.store.execute("INSERT INTO slots(channel_id,due,label,state) VALUES(?,?,?,'preparing')",
                                 (cid, due or self.now+2400, "09:00"))
        ids = self.store.add_candidates(sid, [
            dict(title=f"Вариант {i}", body="Текст с проверенными подробностями. "+str(i)*50,
                 media="photo-file-id", event_key=f"event-{i}", meta={"mode": "news", "sources": []})
            for i in range(count)], self.now)
        return sid, ids


class StoreTests(Base):
    def test_six_profiles_and_seventeen_slots(self):
        chs = self.store.channels()
        self.assertEqual(len(chs), 6)
        self.assertEqual(sum(len(c["config"]["times"]) for c in chs), 17)
        self.assertTrue(all(not c["enabled"] and not c["target"] for c in chs))
        fashion = next(c for c in chs if c["config"]["mode"] == "fashion")
        self.assertEqual(fashion["config"]["budgets"], {"15:00": 10000, "21:00": 20000})

    def test_pair_not_first_stranger_wins(self):
        code = self.store.pair_code()
        self.assertFalse(self.store.pair(666, "wrong"))
        self.assertTrue(self.store.pair(42, code))
        self.assertFalse(self.store.pair(666, code))
        self.assertEqual(self.store.get("owner"), 42)

    def test_pair_expiry(self):
        code = self.store.pair_code()
        p = self.store.get("pair")
        p["expires"] = 1
        self.store.put("pair", p)
        self.assertFalse(self.store.pair(42, code))

    def test_only_one_candidate_can_be_approved(self):
        sid, ids = self.slot()
        self.store.approve(ids[0], self.now)
        with self.assertRaises(ValueError):
            self.store.approve(ids[1], self.now)
        self.assertEqual(self.store.slot(sid)["state"], "queued")

    def test_double_click_is_idempotent(self):
        _, ids = self.slot()
        self.store.approve(ids[0], self.now)
        with self.assertRaises(ValueError):
            self.store.approve(ids[0], self.now)
        self.assertEqual(len(self.store.rows("SELECT id FROM candidates WHERE state='approved'")), 1)

    def test_late_approval_rejected(self):
        _, ids = self.slot(due=self.now-1)
        with self.assertRaises(ValueError):
            self.store.approve(ids[0], self.now)

    def test_edit_revokes_approval(self):
        sid, ids = self.slot()
        self.store.approve(ids[0], self.now)
        self.store.edit_candidate(ids[0], body="Новая версия")
        self.assertEqual(self.store.candidate(ids[0])["state"], "pending")
        self.assertEqual(self.store.slot(sid)["state"], "pending")

    def test_filter_change_cancels_old_queue(self):
        sid, ids = self.slot()
        self.store.approve(ids[0], self.now)
        ch = self.store.channel(1)
        ch["config"]["exclude"] = ["запрет"]
        self.store.update_channel(1, config=ch["config"])
        self.assertEqual(self.store.slot(sid)["state"], "cancelled")
        self.assertEqual(self.store.candidate(ids[0])["state"], "draft")

    def test_required_image_and_demo_block_approval(self):
        _, ids = self.slot()
        self.store.edit_candidate(ids[0], media="")
        with self.assertRaises(ValueError):
            self.store.approve(ids[0], self.now)
        self.store.edit_candidate(ids[0], media="photo")
        self.store.execute("UPDATE candidates SET meta=? WHERE id=?", (json.dumps({"demo": True}), ids[0]))
        with self.assertRaises(ValueError):
            self.store.approve(ids[0], self.now)


class BudgetTests(Base):
    def test_preflight_blocks_before_request(self):
        limits = self.store.get("limits")
        limits["daily_requests"] = 1
        self.store.put("limits", limits)
        b = Budget(self.store)
        rid = b.reserve(1, 1, 100, self.now)
        b.finish(rid, {"input_tokens": 10, "output_tokens": 20}, now=self.now)
        with self.assertRaises(BudgetStop):
            b.reserve(1, 2, 100, self.now)
        self.assertTrue(self.store.get("paused"))
        self.assertEqual(b.usage(self.now)["requests"], 1)

    def test_usage_uses_actual_input_plus_output(self):
        b = Budget(self.store)
        rid = b.reserve(1, 1, 100, self.now)
        b.finish(rid, {"input_tokens": 250, "output_tokens": 150}, now=self.now)
        self.assertEqual(b.usage(self.now)["tokens"], 400)

    def test_missing_usage_charges_reserve_and_stops(self):
        b = Budget(self.store)
        rid = b.reserve(1, 1, 100, self.now)
        b.finish(rid, None, now=self.now)
        self.assertEqual(b.usage(self.now)["tokens"], DEFAULT_LIMITS["request_reserve"])
        self.assertTrue(self.store.get("paused"))
        self.assertEqual(b.usage(self.now)["uncertain"], 1)

    def test_overrun_stops(self):
        b = Budget(self.store)
        rid = b.reserve(1, 1, 100, self.now)
        b.finish(rid, {"input_tokens": 10000, "output_tokens": 10000}, now=self.now)
        self.assertTrue(self.store.get("paused"))
        self.assertEqual(b.usage(self.now)["tokens"], 20000)

    def test_restart_preserves_uncertain_charge(self):
        b = Budget(self.store)
        b.reserve(1, 1, 100, self.now)
        self.store.recover()
        new = Store(self.store.path)
        self.assertTrue(new.get("paused"))
        self.assertEqual(Budget(new).usage(self.now)["uncertain"], 1)
        self.assertEqual(Budget(new).usage(self.now)["tokens"], 12000)

    def test_resume_does_not_reset_counters(self):
        b = Budget(self.store)
        rid = b.reserve(1, 1, 100, self.now)
        b.finish(rid, {"input_tokens": 400, "output_tokens": 300}, now=self.now)
        self.store.halt("test")
        self.store.put("paused", False)
        self.assertEqual(b.usage(self.now)["tokens"], 700)

    def test_rolling_window_crosses_midnight(self):
        midnight = int(datetime(2026, 11, 3, 0, 20, tzinfo=TZ).timestamp())
        b = Budget(self.store)
        rid = b.reserve(1, 1, 100, midnight-3600)
        b.finish(rid, {"input_tokens": 800, "output_tokens": 200}, now=midnight-3600)
        self.assertEqual(b.usage(midnight)["tokens"], 0)
        self.assertEqual(b.usage(midnight)["rolling"], 1000)

    def test_parallel_reservations_cannot_overspend(self):
        limits = self.store.get("limits")
        limits["daily_tokens"] = limits["rolling_tokens"] = 24000
        limits["channel_daily_tokens"] = 24000
        self.store.put("limits", limits)
        accepted = []
        def reserve():
            try:
                accepted.append(Budget(self.store).reserve(1, 1, 100, self.now))
            except BudgetStop:
                pass
        workers = [threading.Thread(target=reserve) for _ in range(5)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        self.assertEqual(len(accepted), 2)
        self.assertEqual(Budget(self.store).usage(self.now)["tokens"], 24000)


class EngineTests(Base):
    def engine(self):
        return Engine(self.store, self.tg, None)

    def test_publish_only_approved_at_due_time(self):
        sid, ids = self.slot(due=self.now+60)
        e = self.engine()
        e.publish(self.now)
        self.assertEqual(self.tg.published, [])
        self.store.approve(ids[0], self.now)
        e.publish(self.now)
        self.assertEqual(self.tg.published, [])
        e.publish(self.now+60)
        e.publish(self.now+61)
        self.assertEqual(len(self.tg.published), 1)
        self.assertEqual(self.store.slot(sid)["state"], "sent")

    def test_paused_never_publishes(self):
        _, ids = self.slot(due=self.now+1)
        self.store.approve(ids[0], self.now)
        self.store.halt("stop")
        self.engine().publish(self.now+1)
        self.assertEqual(self.tg.published, [])

    def test_lost_ack_no_retry_even_after_restart(self):
        sid, ids = self.slot(due=self.now+1)
        self.store.approve(ids[0], self.now)
        self.tg.fail_publish = True
        e = self.engine()
        e.publish(self.now+1)
        self.assertTrue(self.store.get("paused"))
        self.assertEqual(self.store.slot(sid)["state"], "unknown")
        self.store.recover()
        self.store.put("paused", False)
        e.publish(self.now+2)
        self.assertEqual(len(self.tg.published), 1)

    def test_restart_during_send_marks_unknown(self):
        sid, ids = self.slot()
        self.store.execute("UPDATE slots SET state='publishing' WHERE id=?", (sid,))
        self.store.execute("UPDATE candidates SET state='publishing' WHERE id=?", (ids[0],))
        self.store.recover()
        self.assertEqual(self.store.slot(sid)["state"], "unknown")
        self.assertTrue(self.store.get("paused"))

    def test_expired_never_backfills(self):
        sid, ids = self.slot(due=self.now-600)
        e = self.engine()
        e.expire(self.now)
        e.publish(self.now)
        self.assertEqual(self.store.slot(sid)["state"], "expired")
        self.assertFalse(self.tg.published)

    def test_schedule_uses_moscow_and_only_preparation_window(self):
        self.channel()
        e = self.engine()
        early = datetime(2026, 11, 3, 8, 19, tzinfo=TZ).timestamp()
        e.create_slots(early)
        self.assertFalse(self.store.rows("SELECT * FROM slots"))
        e.create_slots(early+60)
        e.create_slots(early+61)
        rows = self.store.rows("SELECT * FROM slots")
        self.assertEqual(len(rows), 1)
        self.assertEqual(datetime.fromtimestamp(rows[0]["due"], TZ).strftime("%H:%M"), "09:00")

    def test_empty_sources_never_call_ai(self):
        self.channel()
        sid = self.store.execute("INSERT INTO slots(channel_id,due,label) VALUES(?,?,?)", (1, self.now+1200, "09:00"))
        class Empty:
            def collect(self, *a): return []
            def shortlist(self, *a): return []
        class Explode:
            def call(self, *a, **kw): raise AssertionError("AI must not be called")
        e = Engine(self.store, self.tg, Explode(), Empty())
        e.prepare(sid, self.now)
        self.assertEqual(self.store.slot(sid)["state"], "failed")


def events(*items):
    return io.BytesIO(b"".join(b"data: "+json.dumps(x).encode()+b"\n\n" for x in items))


class AITests(Base):
    def provider(self, response):
        self.calls = 0
        def opener(request, timeout):
            self.calls += 1
            self.request = json.loads(request.data)
            return response
        return AI(self.store, FakeAuth(), opener)

    def test_stream_completes_and_metered_no_tools_for_news(self):
        response = events(
            {"type": "response.output_text.delta", "delta": '{"options":[]}'},
            {"type": "response.completed", "response": {"usage": {"input_tokens": 30, "output_tokens": 10}, "output": []}})
        ai = self.provider(response)
        data, urls = ai.call(1, 1, "rules", {"x": 1})
        self.assertEqual(data, {"options": []})
        self.assertEqual(self.calls, 1)
        self.assertFalse(self.request["store"])
        self.assertTrue(self.request["stream"])
        self.assertNotIn("tools", self.request)
        self.assertNotIn("max_output_tokens", self.request)
        self.assertEqual(Budget(self.store).usage()["tokens"], 40)

    def test_interrupted_stream_no_automatic_retry(self):
        ai = self.provider(events({"type": "response.output_text.delta", "delta": '{"options":'}))
        with self.assertRaises(AIError):
            ai.call(1, 1, "rules", {})
        self.assertEqual(self.calls, 1)
        self.assertTrue(self.store.get("paused"))

    def test_tool_loop_stops_at_configured_fashion_limit(self):
        response = events(*[{"type": "response.output_item.added", "item": {"type": "web_search_call", "id": str(i)}} for i in range(DEFAULT_LIMITS['fashion_web_calls']+1)])
        ai = self.provider(response)
        with self.assertRaisesRegex(AIError, 'лишний поиск'):
            ai.call(5, 1, "rules", {}, web=True)
        self.assertTrue(self.store.get("paused"))
        self.assertEqual(self.calls, 1)

    def test_no_response_usage_stops(self):
        ai = self.provider(events({"type": "response.completed", "response": {"output": [{"type": "message", "content": [{"type": "output_text", "text": '{"options":[]}'}]}]}}))
        with self.assertRaises(BudgetStop):
            ai.call(1, 1, "rules", {})
        self.assertTrue(self.store.get("paused"))

    def test_large_output_interrupts(self):
        ai = self.provider(events({"type": "response.output_text.delta", "delta": "x"*7000}))
        with self.assertRaises(AIError):
            ai.call(1, 1, "rules", {})
        self.assertTrue(self.store.get("paused"))

    def test_two_malformed_answers_trip_breaker_without_repair_requests(self):
        for _ in range(2):
            ai = self.provider(events(
                {"type": "response.output_text.delta", "delta": "not json"},
                {"type": "response.completed", "response": {"usage": {"input_tokens": 20, "output_tokens": 10}, "output": []}}))
            with self.assertRaises(AIError):
                ai.call(1, 1, "rules", {})
            self.assertEqual(self.calls, 1)
        self.assertTrue(self.store.get("paused"))
        self.assertEqual(Budget(self.store).usage()["tokens"], 60)


class ParserTests(Base):
    def test_rss_dates_and_images(self):
        raw = b'<rss xmlns:media="http://search.yahoo.com/mrss/"><channel><item><title>A</title><link>https://example.org/a?utm_source=x</link><pubDate>Tue, 03 Nov 2026 06:00:00 GMT</pubDate><description>Hello</description><media:thumbnail url="https://example.org/a.jpg"/></item></channel></rss>'
        rows = parse_feed(raw, "https://example.org/rss", self.now)
        self.assertEqual(rows[0]["url"], "https://example.org/a")
        self.assertEqual(rows[0]["image"], "https://example.org/a.jpg")
        self.assertGreater(rows[0]["published"], 0)

    def test_atom(self):
        raw = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>B</title><link href="https://example.org/b"/><published>2026-11-03T06:00:00Z</published><summary>Summary</summary></entry></feed>'
        self.assertEqual(parse_feed(raw, "s", self.now)[0]["summary"], "Summary")

    def test_reject_xml_entities(self):
        with self.assertRaises(ValueError):
            parse_feed(b'<!DOCTYPE rss [<!ENTITY x "test">]><rss/>', "s", self.now)

    def test_canonical_preserves_product_query(self):
        self.assertEqual(canonical("https://www.ozon.ru/product/test-123/?utm_source=x&color=5#reviews"),
                         "https://www.ozon.ru/product/test-123/?color=5")

    def test_reject_local_and_credential_urls(self):
        for url in ("http://127.0.0.1/x", "http://169.254.169.254/", "file:///etc/passwd", "https://user:pass@example.org/"):
            with self.assertRaises(ValueError):
                public_url(url, resolve=False)

    def test_visible_caption_length(self):
        self.assertEqual(utf16('<a href="https://example.org/">Привет</a> 😀'), 9)

    def test_times_validate(self):
        self.assertEqual(parse_times("21:00 09:00"), ["09:00", "21:00"])
        for value in ("24:00", "9:00", "09:00 09:00"):
            with self.assertRaises(ValueError):
                parse_times(value)


class EditorTests(Base):
    def test_unprovided_article_id_is_rejected(self):
        cfg = self.store.channel(1)["config"]
        obj = {"options": [{"title": "Title", "text": "А"*80, "event_key": "event", "article_ids": [99]}]}
        self.assertEqual(news_options(obj, [], [], cfg, self.now), [])

    def test_same_event_from_two_sources_is_not_two_options(self):
        cfg = self.store.channel(1)["config"]
        articles = [dict(id=i, title=f"title{i}", url=f"https://example.org/{i}", published=self.now, image="") for i in (1, 2)]
        obj = {"options": [dict(title=f"Новость {i}", text="А"*80, event_key="same-event", article_ids=[i]) for i in (1,2)]}
        self.assertEqual(len(news_options(obj, articles, [], cfg, self.now)), 1)

    def catalogue(self):
        categories = ["outerwear", "top", "bottom", "shoes", "hat", "bag"]
        return [dict(name=f"Вещь {i}", category=c, price=1000, url=f"https://www.wildberries.ru/catalog/{100000+i}/detail.aspx",
                     checked_at=self.now, winter=True) for i, c in enumerate(categories)]

    def test_full_winter_outfit_and_total(self):
        result = {"options": [{"title": "Зимний образ", "description": "Спокойные цвета", "item_ids": [1,2,3,4,5,6]}]}
        rows = fashion_options(result, {}, 10000, self.now, catalog=self.catalogue())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["meta"]["total"], 6000)
        self.assertFalse(rows[0]["meta"]["unverified"])
        self.assertIn("две", image_prompt(rows[0]).lower())
        self.assertLessEqual(utf16(render(rows[0])), 1000)

    def test_outfit_requires_all_categories(self):
        obj = {"options": [{"item_ids": [1,2,3,4,5]}]}
        self.assertEqual(fashion_options(obj, {}, 10000, self.now, catalog=self.catalogue()), [])

    def test_outfit_budget_applies_to_entire_set(self):
        obj = {"options": [{"item_ids": [1,2,3,4,5,6]}]}
        self.assertEqual(fashion_options(obj, {}, 5000, self.now, catalog=self.catalogue()), [])

    def test_price_must_be_real_positive_number(self):
        p = self.catalogue()[0]
        for price in ("NaN", -10, 0, "бесплатно"):
            with self.assertRaises(ValueError):
                validate_product(dict(p, price=price))

    def test_marketplace_search_page_not_product(self):
        p = self.catalogue()[0]
        with self.assertRaises(ValueError):
            validate_product(dict(p, url="https://www.ozon.ru/search/?text=coat"))

    def test_unverified_goods_cannot_approve(self):
        _, ids = self.slot()
        self.store.execute("UPDATE candidates SET meta=? WHERE id=?", (json.dumps({"unverified": True}), ids[0]))
        with self.assertRaises(ValueError):
            self.store.approve(ids[0], self.now)


class UITests(Base):
    def test_stranger_cannot_stop_or_change_filters(self):
        self.store.put("owner", 42)
        ui = UI(self.store, self.tg, FakeAuth(), Engine(self.store, self.tg, None))
        ui.handle({"message": {"from": {"id": 666}, "chat": {"id": 666, "type": "private"}, "text": "🛑 Стоп"}})
        self.assertFalse(self.store.get("paused"))
        self.assertFalse(self.tg.messages)

    def test_menu_filters_show_each_channel(self):
        self.store.put("owner", 42)
        ui = UI(self.store, self.tg, FakeAuth(), Engine(self.store, self.tg, None))
        ui.channel_list(True)
        rows = self.tg.messages[-1][2]["inline_keyboard"]
        self.assertEqual(len(rows), 8)
        self.assertIn("filters:1", str(rows))

    def test_owner_can_set_filter_without_calling_ai(self):
        self.store.put("owner", 42)
        ui = UI(self.store, self.tg, FakeAuth(), Engine(self.store, self.tg, None))
        self.store.session(42, "field", {"cid": 1, "field": "exclude"})
        ui.message({"text": "слухи, реклама"})
        self.assertEqual(self.store.channel(1)["config"]["exclude"], ["слухи", "реклама"])
        self.assertEqual(self.store.rows("SELECT * FROM ledger"), [])


class JWTTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cryptography.hazmat.primitives.asymmetric import rsa
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        n = cls.key.public_key().public_numbers()
        cls.jwks = {"keys": [{"kid": "test", "kty": "RSA", "alg": "RS256", "n": b64(n.n.to_bytes((n.n.bit_length()+7)//8, "big")), "e": b64(n.e.to_bytes(3, "big"))}]}

    def token(self, **changes):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        claims = dict(iss="https://auth.openai.com", aud="app-test", exp=time.time()+60, sub="user-test", nonce="nonce")
        claims.update(changes)
        h = b64(json.dumps({"alg": "RS256", "kid": "test"}).encode())
        p = b64(json.dumps(claims).encode())
        sig = self.key.sign((h+"."+p).encode(), padding.PKCS1v15(), hashes.SHA256())
        return h+"."+p+"."+b64(sig)

    def verify(self, token):
        return validate_id_token(token, self.jwks, "https://auth.openai.com", "app-test", "nonce")

    def test_valid_signature(self):
        self.assertEqual(self.verify(self.token())["sub"], "user-test")

    def test_wrong_audience(self):
        with self.assertRaises(AuthError):
            self.verify(self.token(aud="other-client"))

    def test_wrong_nonce(self):
        with self.assertRaises(AuthError):
            self.verify(self.token(nonce="wrong"))

    def test_expired_identity(self):
        with self.assertRaises(AuthError):
            self.verify(self.token(exp=time.time()-300))

    def test_tampered_identity(self):
        tok = self.token()
        h, p, s = tok.split(".")
        p = b64(json.dumps({"sub": "attacker"}).encode())
        with self.assertRaises(AuthError):
            self.verify(h+"."+p+"."+s)


if __name__ == "__main__":
    unittest.main()
