import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from smm.budget import Budget, BudgetStop
from smm.config import channel_defaults
from smm.source_presets import PRESETS
from smm.sources import Sources
from smm.store import Store


class SourceUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'smm.sqlite'
        self.s = Store(self.path)
        self.s.put('paused', False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_migration_merges_once_and_preserves_user_settings(self):
        ch = self.s.channel(3)
        cfg = ch['config']
        cfg.update(sources=['https://custom.example/rss'], include=['важное'])
        self.s.update_channel(3, config=cfg, target='-100123', enabled=True)
        self.s.put('sources_v22', False)
        self.s.put('model', 'gpt-5.6-sol')
        migrated = Store(self.path)
        ch = migrated.channel(3)
        self.assertEqual(ch['config']['sources'][0], 'https://custom.example/rss')
        self.assertTrue(set(PRESETS['ai']).issubset(ch['config']['sources']))
        self.assertEqual(ch['target'], '-100123')
        self.assertEqual(ch['config']['include'], ['важное'])
        self.assertEqual(migrated.get('model'), 'gpt-5.6-sol')
        cfg = ch['config']; cfg['sources'] = []
        migrated.update_channel(3, config=cfg)
        self.assertEqual(Store(self.path).channel(3)['config']['sources'], [])

    def test_football_recognition_does_not_replace_custom_sources(self):
        cfg = channel_defaults(); cfg['sources'] = ['https://example.org/custom']
        cid = self.s.add_channel('Наш футбол', cfg)
        self.s.put('sources_v22', False)
        s = Store(self.path)
        self.assertTrue(set(PRESETS['football']).issubset(s.channel(cid)['config']['sources']))
        self.assertIn('https://example.org/custom', s.channel(cid)['config']['sources'])

    def test_sixth_source_is_fetched_and_selected(self):
        ch = self.s.channel(3)
        cfg = ch['config']; cfg['sources'] = [f'https://feed{i}.example/rss' for i in range(7)]
        self.s.update_channel(3, config=cfg)
        now = int(time.time())
        raw = b'<rss><channel/></rss>'
        article = dict(url='https://news.example/story', title='New AI model', summary='Details',
                       published=now-1, image='', source=cfg['sources'][6])
        with patch('smm.sources.fetch', return_value=(raw, {}, 'https://feed.example/rss')) as fetch, patch('smm.sources.parse_feed', side_effect=lambda raw, src, now: [article] if src==cfg['sources'][6] else []):
            Sources(self.s).collect(self.s.channel(3), now)
        self.assertEqual(fetch.call_count, 7)
        self.assertEqual(len(Sources(self.s).shortlist(self.s.channel(3), now)), 1)

    def test_fashion_skips_token_tracking_even_above_all_budgets(self):
        fashion = next(c for c in self.s.channels() if c['config']['mode']=='fashion')
        self.s.put('fashion_spend_closed:17', True)
        limits = self.s.get('limits')
        for key in ('daily_tokens','rolling_tokens','channel_daily_tokens','fashion_slot_tokens'):
            limits[key] = 1
        self.s.put('limits', limits)
        b = Budget(self.s)
        self.assertIsNone(b.reserve(fashion['id'], 17, 90000))
        b.finish(None, None)
        self.assertEqual(self.s.rows('SELECT id FROM ledger'), [])
        self.assertFalse(self.s.get('paused'))
        with self.assertRaises(BudgetStop): b.reserve(1,18,10)

    def test_pause_still_stops_unmetered_fashion(self):
        fashion = next(c for c in self.s.channels() if c['config']['mode']=='fashion')
        self.s.put('paused',True)
        with self.assertRaises(BudgetStop): Budget(self.s).reserve(fashion['id'],1,10)

    def test_warning_mode_keeps_unknown_usage_without_global_pause(self):
        self.s.put('budget_enforced',False)
        rid=Budget(self.s).reserve(1,12,10)
        Budget(self.s).finish(rid,None)
        self.assertFalse(self.s.get('paused'))
        self.assertEqual(self.s.rows('SELECT state FROM ledger')[0]['state'],'uncertain')
