import unittest,tempfile
from pathlib import Path
from unittest.mock import Mock
from smm.store import Store
from smm.analytics import Analytics
from smm.automation import next_release,accept_plan

class AnalyticsTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(Path(self.tmp.name)/'db');self.s.update_channel(1,target='-100123',enabled=1);self.t=Mock();self.t.call.return_value=100;self.now=100000;self.a=Analytics(self.s,self.t,lambda:self.now)
 def tearDown(self):self.tmp.cleanup()
 def test_subscriber_baseline_and_unknown(self):
  self.a.collect();self.assertIsNone(self.a.report()[0]['change']);self.now+=86400;self.t.call.return_value=105;self.a.collect();self.assertEqual(self.a.report()[0]['change'],5)
 def test_error_keeps_last_value(self):
  self.a.collect();self.t.call.side_effect=RuntimeError();self.now+=3600;self.a.collect();r=self.a.report()[0];self.assertEqual(r['subscribers'],100);self.assertTrue(r['error'])
 def test_views_delta_is_not_cumulative_counter(self):
  g=Mock();g.channel_metrics.return_value=[dict(message_id=1,published=1,views=500)];self.a.collect_views(g);self.now+=86400;g.channel_metrics.return_value=[dict(message_id=1,published=1,views=550)];self.a.collect_views(g);self.assertEqual(self.a.report()[0]['views_gain'],50)
 def test_no_history_not_zero_views(self):self.assertIsNone(self.a.report()[0]['views_gain'])
 def test_target_change_does_not_mix_history(self):
  self.a.collect();self.s.update_channel(1,target='-100456');self.assertIsNone(self.a.report()[0]['subscribers'])
 def test_review_window_and_hour_gap(self):
  cfg=dict(posts_per_run=2,post_gap_minutes=5)
  first=next_release(cfg,1000,planned=1001,automatic=True);second=next_release(cfg,1000,[first],planned=1001,automatic=True)
  self.assertGreaterEqual(first,1600);self.assertGreaterEqual(second-first,3600)
 def test_plan_validation_and_movie_cap(self):
  ch=self.s.channel(1);ch['config']['mode']='cinema';accept_plan(self.s,ch,dict(daily_posts=6,hours=[9]),self.now);self.assertIsNone(self.s.get('ai_plan:1'))
  accept_plan(self.s,ch,dict(daily_posts=1,hours=[20],reason='Вечерний фильм'),self.now);self.assertEqual(self.s.get('ai_plan:1')['times'],['20:00'])
