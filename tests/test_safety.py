import json
from datetime import datetime
from unittest.mock import Mock, patch

from test_core import Base
from smm.budget import Budget, BudgetStop
from smm.engine import Engine
from smm.manager import Manager
from smm.notifications import EventNotifications
from smm.safety import PublicationSafety
from smm.store import Store, TZ


class SafetyTests(Base):
    def setUp(self):
        super().setUp()
        self.now=int(datetime(2026,11,6,9,0,tzinfo=TZ).timestamp())
        self.engine=Engine(self.store,self.tg,Mock())
        self.store.put('owner',42)
        self.manager=Manager(self.store,Mock(),clock=lambda:self.now)

    def post(self,index,cid=1,body=None,title=None,at=None):
        self.channel(cid)
        sid=self.store.enqueue_search(cid,self.now+index*10)
        self.store.execute("UPDATE slots SET state='preparing' WHERE id=?",(sid,))
        pid=self.store.add_candidates(sid,[dict(title=title or f'Новое событие номер {index}',
            body=body or f'Подтверждены результаты научной работы номер {index}. Исследователи представили данные и подробности проведённого эксперимента.',
            event_key=f'event-{index}',media='fixture-photo',meta={})],self.now)[0]
        due=at if at is not None else self.now+30+index
        self.store.schedule(pid,due,self.now)
        return pid

    def incident(self):return self.store.get('emergency_stop',{})

    def test_normal_two_distinct_posts_do_not_stop(self):
        a=self.post(1,body='Учёные открыли новый остров. Экспедиция исследовала берег и опубликовала результаты наблюдений.')
        b=self.post(2,body='Правительство представило транспортный проект. Новая железная дорога соединит несколько крупных городов.')
        self.engine.publish(self.now+60)
        self.assertEqual(len(self.tg.published),2)
        self.assertFalse(self.incident().get('active'))

    def test_third_post_in_ten_minutes_halts_before_send_and_quarantines_all(self):
        ids=[self.post(i) for i in (1,2,3)]
        other=self.post(4,cid=2,at=self.now+1800)
        self.engine.publish(self.now+60)
        self.assertEqual(len(self.tg.published),2)
        self.assertTrue(self.incident()['active']);self.assertTrue(self.store.get('paused'))
        for pid in (ids[2],other):
            p=self.store.candidate(pid);self.assertEqual(p['state'],'draft');self.assertIsNone(p['scheduled_at'])
        self.engine.publish(self.now+1800);self.assertEqual(len(self.tg.published),2)

    def test_daily_cap_is_schedule_count_not_unlimited_manual_queue(self):
        cfg=self.store.channel(1)['config'];cfg['publish_times']=['09:00'];cfg['posts_per_run']=2
        self.store.update_channel(1,config=cfg)
        for i in (1,2):
            self.post(i,at=self.now+i*1200);self.engine.publish(self.now+i*1200)
        self.post(3,at=self.now+3600);self.engine.publish(self.now+3600)
        self.assertEqual(len(self.tg.published),2)
        self.assertIn('дневной предел',self.incident()['reason'])

    def test_new_day_resets_daily_cap(self):
        cfg=self.store.channel(1)['config'];cfg['publish_times']=['09:00'];cfg['posts_per_run']=2
        self.store.update_channel(1,config=cfg)
        for i in (1,2):self.post(i,at=self.now+i*1200);self.engine.publish(self.now+i*1200)
        self.post(3,at=self.now+86400);self.engine.publish(self.now+86400)
        self.assertEqual(len(self.tg.published),3)

    def test_duplicate_body_stops_even_with_changed_title_and_event(self):
        body='В городе открылась новая библиотека. Жителям доступны книги, электронные издания и читальные залы.'
        self.post(1,body=body);self.engine.publish(self.now+60)
        self.post(2,body=body,title='Совсем другой заголовок',at=self.now+1200);self.engine.publish(self.now+1200)
        self.assertEqual(len(self.tg.published),1);self.assertIn('Повтор',self.incident()['reason'])

    def test_near_duplicate_with_tracking_link_is_also_rejected(self):
        body='Международная экспедиция впервые изучила глубокую океанскую впадину и обнаружила неизвестные организмы на морском дне.'
        self.post(1,body=body+' https://example.com?a=1');self.engine.publish(self.now+60)
        self.post(2,body=body+' https://example.com?a=2',at=self.now+1200);self.engine.publish(self.now+1200)
        self.assertEqual(len(self.tg.published),1);self.assertTrue(self.incident()['active'])

    def test_same_target_in_two_profiles_shares_the_limit(self):
        for i in (1,2,3):self.post(i,cid=1 if i<3 else 2)
        self.engine.publish(self.now+60)
        self.assertEqual(len(self.tg.published),2);self.assertTrue(self.incident()['active'])

    def test_distinct_targets_have_independent_counters(self):
        self.post(1);self.post(2);self.post(3,cid=2)
        self.store.update_channel(2,target='-100999999999')
        # Target change revokes the old claim, so reapprove the remaining post.
        p=self.store.rows("SELECT p.id FROM candidates p JOIN slots s ON s.id=p.slot_id WHERE s.channel_id=2")[0]
        self.store.schedule(p['id'],self.now+60,self.now)
        self.engine.publish(self.now+60)
        self.assertEqual(len(self.tg.published),3);self.assertFalse(self.incident().get('active'))

    def test_garbage_is_stopped_before_first_network_request(self):
        self.post(1,body='!!! ??? !!!',title='???')
        self.engine.publish(self.now+60)
        self.assertEqual(self.tg.published,[]);self.assertTrue(self.incident()['active'])

    def test_repeated_words_and_placeholders_trip(self):
        for body in ('новость '*30,'Lorem ipsum dolor sit amet consectetur adipiscing elit'):
            with self.subTest(body=body):
                from smm.safety import quality_problem
                self.assertTrue(quality_problem(dict(title='Новости',body=body)))

    def test_changed_automatic_post_cannot_skip_last_check(self):
        pid=self.post(1)
        self.store.execute('UPDATE candidates SET auto_queued=1,meta=? WHERE id=?',(json.dumps(dict(editor_approved=True,editor_reason='Проверено',editor_digest='wrong')),pid))
        self.engine.publish(self.now+60)
        self.assertEqual(self.tg.published,[]);self.assertIn('изменился',self.incident()['reason'])

    def test_three_editorial_failures_trip_but_good_batch_resets_streak(self):
        guard=PublicationSafety(self.store)
        guard.quality_result(1,False,self.now);guard.quality_result(1,False,self.now)
        guard.quality_result(1,True,self.now)
        self.assertEqual(self.store.get('quality_streak:1'),0)
        for _ in range(3):guard.quality_result(1,False,self.now)
        self.assertTrue(self.incident()['active'])

    def test_incident_survives_restart_and_cannot_be_bypassed_by_pause_flag(self):
        self.store.emergency_halt('Test',now=self.now)
        fresh=Store(self.store.path);fresh.recover();fresh.put('paused',False)
        self.assertTrue(fresh.get('emergency_stop')['active'])
        with self.assertRaises(BudgetStop):Budget(fresh).reserve(1,1,100,now=self.now)
        self.assertEqual(self.tg.published,[])

    def test_manager_active_campaign_and_search_are_stopped(self):
        self.store.put('manager_campaigns',[dict(id='x',state='active')])
        self.store.put('manager_search',dict(id='y',state='running'))
        self.store.put('manager_account',dict(connected=True))
        self.store.emergency_halt('Test',now=self.now)
        self.assertEqual(self.store.get('manager_campaigns')[0]['state'],'stopped')
        self.assertEqual(self.store.get('manager_search')['state'],'cancelled')
        with self.assertRaises(ValueError):self.manager.start('x')
        self.manager.tick();self.manager.gateway.send_group.assert_not_called()
        with self.assertRaises(ValueError):self.manager.queue_search()

    def test_stop_during_ai_generation_cancels_jobs_and_no_new_approvals(self):
        pid=self.post(1);sid=self.store.enqueue_search(1,self.now+5)
        self.store.emergency_halt('Test',now=self.now)
        self.assertEqual(self.store.slot(sid)['state'],'cancelled')
        with self.assertRaises(ValueError):self.store.schedule(pid,self.now+600,self.now)
        with self.assertRaises(ValueError):self.store.enqueue_search(1,self.now+100)

    def test_resume_is_manual_and_does_not_replay_quarantined_posts_or_campaigns(self):
        pid=self.post(1);self.store.put('manager_campaigns',[dict(id='x',state='active')])
        self.store.emergency_halt('Test',now=self.now);self.store.resume_after_review(42)
        self.assertFalse(self.incident()['active']);self.assertEqual(self.incident()['reviewed_by'],42)
        self.assertEqual(self.store.candidate(pid)['state'],'draft')
        self.assertEqual(self.store.get('manager_campaigns')[0]['state'],'stopped')
        self.engine.publish(self.now+60);self.assertEqual(self.tg.published,[])

    def test_unknown_send_requires_resolution_before_emergency_reset(self):
        pid=self.post(1);self.store.execute("UPDATE candidates SET state='unknown' WHERE id=?",(pid,))
        self.store.emergency_halt('Test',now=self.now)
        with self.assertRaises(ValueError):self.store.resume_after_review(42)
        self.assertTrue(self.incident()['active'])

    def test_one_critical_notification_bypasses_disabled_normal_messages(self):
        self.store.put('telegram_notifications',dict(enabled=False,errors=False,min_id=999999,since=self.now+99999))
        self.store.notice('sent:old','Ordinary message')
        self.store.emergency_halt('Too many posts',now=self.now)
        self.store.emergency_halt('Second reason',now=self.now)
        sender=EventNotifications(self.store,self.tg,clock=lambda:self.now)
        sender.tick();sender.tick()
        self.assertEqual(len(self.tg.messages),1)
        self.assertEqual(self.tg.messages[0][0],42)
        self.assertIn('АВАРИЙНАЯ',self.tg.messages[0][1]);self.assertNotIn('Ordinary',self.tg.messages[0][1])

    def test_critical_notification_has_priority_and_ambiguous_send_is_not_retried(self):
        self.store.put('telegram_notifications',dict(enabled=True,since=0,min_id=0))
        for i in range(40):self.store.notice('sent:'+str(i),'Ordinary')
        self.store.emergency_halt('Test',now=self.now)
        tg=Mock();tg.send.side_effect=TimeoutError()
        sender=EventNotifications(self.store,tg,clock=lambda:self.now)
        sender.tick()
        self.assertIn('АВАРИЙНАЯ',tg.send.call_args.args[1])
        row=self.store.rows("SELECT delivered FROM notices WHERE notice_key LIKE 'emergency:%'")[0]
        self.assertEqual(row['delivered'],2);self.assertTrue(self.incident()['active'])

    def test_audit_counters_survive_restart(self):
        self.post(1);self.post(2);self.engine.publish(self.now+60)
        fresh=Store(self.store.path);fresh.put('paused',False)
        self.post(3,at=self.now+120)
        Engine(fresh,self.tg,Mock()).publish(self.now+120)
        self.assertEqual(len(self.tg.published),2);self.assertTrue(fresh.get('emergency_stop')['active'])

    def test_existing_confirmed_posts_count_after_upgrade(self):
        ids=[self.post(i) for i in (1,2,3)]
        for pid in ids[:2]:self.store.execute("UPDATE candidates SET state='sent' WHERE id=?",(pid,))
        self.engine.publish(self.now+60)
        self.assertEqual(self.tg.published,[]);self.assertTrue(self.incident()['active'])

    def test_parallel_publishers_cannot_exceed_burst_limit(self):
        import threading
        for i in (1,2,3,4):self.post(i)
        engines=[Engine(self.store,self.tg,Mock()),Engine(Store(self.store.path),self.tg,Mock())]
        threads=[threading.Thread(target=e.publish,args=(self.now+60,)) for e in engines]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertLessEqual(len(self.tg.published),2);self.assertTrue(self.incident()['active'])

    def test_guard_failure_also_emergency_stops_all_instead_of_sending_unchecked(self):
        self.post(1)
        self.engine.shutdown=Mock();self.engine.shutdown.wait.side_effect=[False,True]
        with patch('smm.engine.time.time',return_value=self.now+60),patch('smm.engine.PublicationSafety.claim',side_effect=ValueError('corrupt audit')):
            self.engine.publisher_loop()
        self.assertEqual(self.tg.published,[]);self.assertTrue(self.incident()['active'])

    def test_notification_claim_is_atomic_across_sender_instances(self):
        import threading
        self.store.emergency_halt('Test',now=self.now)
        tg=Mock()
        senders=[EventNotifications(self.store,tg,clock=lambda:self.now),EventNotifications(Store(self.store.path),tg,clock=lambda:self.now)]
        threads=[threading.Thread(target=s.tick) for s in senders]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads));tg.send.assert_called_once()

    def test_later_error_does_not_overwrite_incident_reason(self):
        self.store.emergency_halt('Original emergency',now=self.now)
        self.store.halt('Unrelated follow-up error')
        self.assertIn('Original emergency',self.store.get('pause_reason'))

    def test_crash_turns_claim_into_unknown_audit_until_manual_resolution(self):
        pid=self.post(1)
        post=self.store.candidate(pid)
        with self.store.tx() as c:
            PublicationSafety(self.store).claim(post,post['config'],self.now+60,c)
            c.execute("UPDATE candidates SET state='publishing' WHERE id=?",(pid,))
        fresh=Store(self.store.path);fresh.recover()
        self.assertEqual(fresh.candidate(pid)['state'],'unknown')
        self.assertEqual(fresh.rows('SELECT outcome FROM publication_dispatches')[0]['outcome'],'unknown')
