import json
from datetime import datetime
from unittest.mock import Mock, patch

from test_core import Base
from smm.automation import AutoPublisher
from smm.budget import Budget, BudgetStop
from smm.editor import review_digest
from smm.engine import Engine
from smm.net import NetError
from smm.notifications import EventNotifications
from smm.store import Store, TZ


class AutonomyTests(Base):
    def setUp(self):
        super().setUp()
        self.store.put('independent_publication',True)
        self.at=int(datetime(2026,11,6,8,20,tzinfo=TZ).timestamp())
        self.release=self.at+2400
        self.auto_channel(1)
        self.ai=Mock();self.ai.call.side_effect=self.generate
        self.source=Mock();self.source.collect.return_value=[]
        self.source.shortlist.return_value=[dict(id=i,url=f'https://news.example/{i}',title=t,
            text='Подтверждённые сведения о событии и его последствиях. '*3,summary='Описание события',
            published=self.at-60,image='https://news.example/photo.jpg') for i,t in enumerate(
                ['Открыта новая научная станция','Опубликован международный торговый отчёт'],1)]
        for a in self.source.shortlist.return_value:
            self.store.execute('INSERT INTO articles(id,url,title,summary,source,published,image,fingerprint,fetched,text) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (a['id'],a['url'],a['title'],a['summary'],'fixture',a['published'],a['image'],str(a['id']),self.at,a['text']))
        self.engine=Engine(self.store,self.tg,self.ai,self.source)

    def auto_channel(self,cid):
        self.channel(cid);ch=self.store.channel(cid);cfg=ch['config']
        cfg.update(publication_mode='automatic',auto_publish=True,auto_search=True,
            publish_times=['09:00','15:00','21:00'],posts_per_run=2,post_gap_minutes=5,auto_text_fallback=True)
        self.store.update_channel(cid,config=cfg,target='-100'+str(cid)*9)

    def generate(self,cid,sid,instructions,context,**kwargs):
        return {'options':[dict(title=a['title'],text='Подтверждённая новость. '+str(a['id'])*65,
            article_ids=[a['id']],event_key='verified-'+str(a['id']),approved=True,
            review_reason='Факты подтверждены источником, дата и тема проверены.')
            for a in context['articles'][:2]]},set()

    def ready(self,cid=1):
        sid=self.store.enqueue_search(cid,self.at,publish_at=self.release)
        self.engine.prepare(sid,self.at)
        return sid,self.store.rows('SELECT id FROM candidates WHERE slot_id=? ORDER BY id',(sid,))

    def test_full_cycle_finds_reviews_queues_and_publishes_without_owner(self):
        self.engine.tick(self.at)
        posts=self.store.rows('SELECT * FROM candidates ORDER BY id')
        self.assertEqual([p['state'] for p in posts],['approved','approved'])
        self.assertEqual([p['scheduled_at'] for p in posts],[self.release,self.release+3600])
        self.assertTrue(all(p['auto_queued'] for p in posts))
        self.assertEqual(self.ai.call.call_count,1)
        self.engine.tick(self.at+10)
        self.engine.publish(self.release);self.engine.publish(self.release+3600)
        self.engine.tick(self.release+3601)
        self.assertEqual(len(self.tg.published),2)
        self.assertEqual(self.ai.call.call_count,1)

    def test_late_start_does_not_require_exact_trigger_minute(self):
        self.engine.create_slots(self.release-60)
        jobs=self.store.rows('SELECT * FROM slots')
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]['publish_at'],self.release)
        self.store.execute("UPDATE slots SET state='pending'")
        Engine(Store(self.store.path),self.tg,self.ai).create_slots(self.release+60)
        self.assertEqual(len(self.store.rows('SELECT * FROM slots')),1)

    def test_midnight_search_is_created_on_previous_day(self):
        cfg=self.store.channel(1)['config'];cfg['publish_times']=['00:10']
        self.store.update_channel(1,config=cfg)
        at=int(datetime(2026,11,6,23,30,tzinfo=TZ).timestamp())
        self.engine.create_slots(at)
        planned=self.store.rows('SELECT publish_at FROM slots')[0]['publish_at']
        self.assertEqual(datetime.fromtimestamp(planned,TZ).strftime('%d %H:%M'),'07 00:10')

    def test_active_manual_search_does_not_consume_future_auto_marker(self):
        sid=self.store.enqueue_search(1,self.at)
        self.engine.create_slots(self.at)
        self.store.execute("UPDATE slots SET state='pending' WHERE id=?",(sid,))
        self.engine.create_slots(self.at+1)
        self.assertEqual(len(self.store.rows('SELECT * FROM slots')),2)

    def test_auto_posts_survive_short_server_outage_but_not_old_backlog(self):
        sid,rows=self.ready()
        self.engine.expire(self.release+600);self.engine.publish(self.release+600)
        self.assertEqual(len(self.tg.published),1)
        self.engine.publish(self.release+4200)
        self.assertEqual(len(self.tg.published),2)
        self.assertFalse(self.store.get('paused'))

    def test_ai_rejection_never_becomes_automatic_approval(self):
        original=self.generate
        def reject(*a,**k):
            result,urls=original(*a,**k)
            for p in result['options']:p.update(approved=False,review_reason='Недостаточно фактов')
            return result,urls
        self.ai.call.side_effect=reject
        sid,rows=self.ready()
        self.assertTrue(all(self.store.candidate(p['id'])['state']=='pending' for p in rows))
        self.assertTrue(self.store.rows("SELECT id FROM notices WHERE notice_key LIKE 'auto-held:%'"))
        self.engine.publish(self.release)
        self.assertEqual(self.tg.published,[])

    def test_stop_during_generation_keeps_paid_results_without_posting(self):
        def stopped(*a,**k):
            self.store.stop_search(1)
            return self.generate(*a,**k)
        self.ai.call.side_effect=stopped
        sid,rows=self.ready()
        self.assertTrue(all(self.store.candidate(p['id'])['state']=='draft' for p in rows))

    def test_edit_between_review_and_queue_invalidates_ai_decision(self):
        original=self.store.schedule
        def edit(pid,due,now=None,**kwargs):
            self.store.edit_candidate(pid,body='Изменённый владельцем текст после проверки ИИ.')
            return original(pid,due,now,**kwargs)
        with patch.object(self.store,'schedule',side_effect=edit):sid,rows=self.ready()
        self.assertTrue(all(self.store.candidate(p['id'])['scheduled_at'] is None for p in rows))

    def test_disabling_auto_revokes_auto_queue_but_preserves_manual_post(self):
        sid,rows=self.ready()
        self.store.schedule(rows[1]['id'],self.release+1800,self.at)
        cfg=self.store.channel(1)['config'];cfg['auto_publish']=False
        self.store.update_channel(1,config=cfg)
        self.assertIsNone(self.store.candidate(rows[0]['id'])['scheduled_at'])
        self.assertEqual(self.store.candidate(rows[1]['id'])['scheduled_at'],self.release+1800)

    def test_unknown_send_isolates_only_its_channel_and_is_not_retried(self):
        self.auto_channel(2)
        a,arows=self.ready(1);b,brows=self.ready(2)
        calls=[]
        def publish(target,*args):
            calls.append(target)
            if target=='-100111111111':raise TimeoutError()
            return {'message_id':len(calls)}
        self.tg.publish=publish
        self.engine.publish(self.release)
        self.assertEqual(self.store.candidate(arows[0]['id'])['state'],'unknown')
        self.assertEqual(self.store.candidate(brows[0]['id'])['state'],'sent')
        self.assertFalse(self.store.get('paused'))
        self.engine.publish(self.release+3600)
        self.assertEqual(calls.count('-100111111111'),1)
        self.assertEqual(self.store.candidate(brows[1]['id'])['state'],'sent')

    def test_definite_telegram_refusal_is_not_reported_as_ambiguous_success(self):
        sid,rows=self.ready()
        self.tg.publish=Mock(side_effect=NetError('No rights',403))
        self.engine.publish(self.release)
        p=self.store.candidate(rows[0]['id'])
        self.assertEqual(p['state'],'draft')
        self.assertIn('403',p['publication_error'])
        self.assertFalse(self.store.get('paused'))

    def test_ai_budget_exhaustion_does_not_stop_existing_publication(self):
        sid,rows=self.ready()
        limits=self.store.get('limits');limits['daily_requests']=1
        self.store.put('limits',limits)
        rid=Budget(self.store).reserve(1,999,10,now=self.at)
        Budget(self.store).finish(rid,dict(input_tokens=10,output_tokens=10),now=self.at)
        with self.assertRaises(BudgetStop):Budget(self.store).reserve(1,1000,10,now=self.at)
        self.assertFalse(self.store.get('paused'))
        self.engine.publish(self.release)
        self.assertEqual(self.store.candidate(rows[0]['id'])['state'],'sent')

    def test_text_fallback_applies_only_when_channel_allows_it(self):
        self.source.shortlist.return_value[0]['image']=''
        self.source.shortlist.return_value[1]['image']=''
        sid,rows=self.ready()
        self.assertEqual(self.store.candidate(rows[0]['id'])['state'],'approved')

    def test_migration_keeps_choices_after_subsequent_restart(self):
        self.store.put('autonomy_v24',False)
        migrated=Store(self.store.path)
        self.assertTrue(migrated.channel(1)['config']['auto_publish'])
        cfg=migrated.channel(1)['config'];cfg['auto_publish']=False
        migrated.update_channel(1,config=cfg)
        self.assertFalse(Store(self.store.path).channel(1)['config']['auto_publish'])

    def test_old_auto_queue_is_not_backfilled_after_long_outage(self):
        sid,rows=self.ready()
        self.engine.expire(self.release+6000);self.engine.publish(self.release+6000)
        self.assertTrue(all(self.store.candidate(p['id'])['state']=='draft' for p in rows))
        self.assertEqual(self.tg.published,[])

    def test_definite_photo_rejection_can_fall_back_to_text_once(self):
        sid,rows=self.ready()
        self.tg.publish=Mock(side_effect=[NetError('Photo unavailable',400),{'message_id':88}])
        self.engine.publish(self.release)
        self.assertEqual(self.tg.publish.call_count,2)
        self.assertEqual(self.tg.publish.call_args.args[2],'')
        self.assertEqual(self.store.candidate(rows[0]['id'])['state'],'sent')
        self.assertEqual(self.store.candidate(rows[0]['id'])['media'],'')

    def test_ambiguous_photo_send_never_triggers_a_text_duplicate(self):
        sid,rows=self.ready()
        self.tg.publish=Mock(side_effect=TimeoutError())
        self.engine.publish(self.release);self.engine.publish(self.release+1)
        self.tg.publish.assert_called_once()
        self.assertEqual(self.store.candidate(rows[0]['id'])['state'],'unknown')

    def test_one_post_per_run_does_not_generate_an_extra_pending_sibling(self):
        cfg=self.store.channel(1)['config'];cfg['posts_per_run']=1
        self.store.update_channel(1,config=cfg)
        sid,rows=self.ready()
        self.assertEqual(len(rows),1)
        self.assertEqual(self.ai.call.call_count,1)


class EventNotificationTests(Base):
    def setUp(self):
        super().setUp()
        self.store.put('owner',42)
        self.store.put('telegram_notifications',dict(enabled=True,success=True,errors=True,partial=True,since=0,min_id=0))
        self.tg=Mock();self.sender=EventNotifications(self.store,self.tg,clock=lambda:self.now)

    def test_summaries_only_to_owner_and_no_duplicate_on_next_tick(self):
        self.store.notice('sent:1','Первый пост опубликован')
        self.store.notice('slot-failed:2','Не удалось найти материал')
        self.sender.tick();self.sender.tick()
        self.tg.send.assert_called_once()
        self.assertEqual(self.tg.send.call_args.args[0],42)
        self.assertIn('Первый пост',self.tg.send.call_args.args[1])
        self.assertIn('Не удалось',self.tg.send.call_args.args[1])

    def test_ambiguous_notification_failure_is_not_retried_or_a_global_stop(self):
        self.store.notice('sent:1','Published')
        self.tg.send.side_effect=TimeoutError()
        self.sender.tick();self.now+=3600;self.sender.tick()
        self.tg.send.assert_called_once()
        self.assertFalse(self.store.get('paused'))

    def test_old_events_and_disabled_categories_are_not_sent(self):
        self.store.notice('sent:old','Old event')
        old=self.store.rows('SELECT MAX(id) AS id FROM notices')[0]['id']
        self.store.put('telegram_notifications',dict(enabled=True,success=False,errors=True,partial=True,since=0,min_id=old))
        self.store.notice('sent:current','Success disabled')
        self.store.notice('auto-result:partial','Найдено 1 из 2 постов')
        self.sender.tick()
        text=self.tg.send.call_args.args[1]
        self.assertNotIn('Old event',text);self.assertNotIn('Success disabled',text)
        self.assertIn('1 из 2',text)

    def test_disabled_notifications_never_send(self):
        self.store.put('telegram_notifications',dict(enabled=False))
        self.store.notice('slot-failed:1','Error')
        self.sender.tick();self.tg.send.assert_not_called()
