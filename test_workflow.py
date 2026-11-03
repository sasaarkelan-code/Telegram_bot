import json
import time
import unittest
from datetime import datetime
from unittest.mock import patch

from test_core import Base, FakeAuth, FakeTelegram, events
from smm.ai import AI
from smm.engine import Engine
from smm.sources import Sources
from smm.store import TZ
from smm.ui import UI


class WorkflowTests(Base):
    def test_rss_to_two_proposals_to_approved_scheduled_post(self):
        at = int(datetime(2026, 11, 3, 8, 20, tzinfo=TZ).timestamp())
        self.channel()
        source = self.store.channel(1)["config"]["sources"][0]
        articles = [
            ("Научная станция начала работу", "Научная станция открылась после завершения строительства. Исследователи приступили к наблюдениям.", "station"),
            ("Опубликован отчёт о международной торговле", "Международная организация опубликовала новый отчёт. В нём представлены итоги торговли за прошедший период.", "trade"),
        ]
        ids = []
        for title, text, slug in articles:
            ids.append(self.store.execute(
                "INSERT INTO articles(url,title,summary,source,published,image,fingerprint,fetched,text) VALUES(?,?,?,?,?,?,?,?,?)",
                ("https://example.org/"+slug, title, text, source, at-60, "https://example.org/p.jpg", slug, at, text)))
        output = {"options": [
            dict(title=a[0], text=a[1], article_ids=[aid], event_key=a[2]+"-2026-11-03")
            for aid, a in zip(ids, articles)
        ]}
        payloads = []
        def opener(req, timeout):
            payloads.append(json.loads(req.data))
            return events({"type": "response.output_text.delta", "delta": json.dumps(output)},
                          {"type": "response.completed", "response": {"usage": {"input_tokens": 900, "output_tokens": 300}, "output": []}})
        provider = AI(self.store, FakeAuth(), opener)
        source_reader = Sources(self.store)
        engine = Engine(self.store, self.tg, provider, source_reader)
        with patch.object(source_reader, "collect", return_value=[]):
            engine.tick(at)
            engine.tick(at+10)
        self.assertEqual(len(payloads), 1)
        pending = self.store.rows("SELECT id FROM candidates WHERE state='pending' ORDER BY id")
        self.assertEqual(len(pending), 2)
        self.store.put("owner", 42)
        ui = UI(self.store, self.tg, FakeAuth(), engine)
        ui.show_post(pending[0]["id"])
        self.assertGreater(len(self.tg.messages), 0)
        self.store.approve(pending[0]["id"], at+600)
        engine.publish(at+2399)
        self.assertFalse(self.tg.published)
        engine.publish(at+2400)
        engine.publish(at+2401)
        self.assertEqual(len(self.tg.published), 1)
        self.assertIn("Научная станция", self.tg.published[0][1])
        self.assertNotIn("торговле", self.tg.published[0][1])
        self.assertEqual(len(payloads), 1)

    def test_notification_failure_is_queued_for_later(self):
        self.store.put("owner", 42)
        self.store.notice("notice-test", "Защита остановила ИИ")
        engine = Engine(self.store, self.tg, None)
        ui = UI(self.store, self.tg, FakeAuth(), engine)
        from smm.net import NetError
        with patch.object(self.tg, "send", side_effect=NetError("offline")):
            ui.deliver()
        n = self.store.rows("SELECT * FROM notices")[0]
        self.assertFalse(n["delivered"])
        self.store.execute("UPDATE notices SET last_attempt=0")
        ui.deliver()
        self.assertTrue(self.store.rows("SELECT * FROM notices")[0]["delivered"])


if __name__ == "__main__":
    unittest.main()
