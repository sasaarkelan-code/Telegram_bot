import json
from datetime import datetime
from unittest.mock import patch

from test_core import Base, FakeAuth
from smm.ai import AIError
from smm.engine import Engine
from smm.navigation import PanelUI
from smm.planning import publication_time
from smm.store import Store, TZ


class PlanningTests(Base):
    def batch(self,count=2,cid=1):
        self.channel(cid)
        sid=self.store.enqueue_search(cid,self.now)
        self.store.execute("UPDATE slots SET state='preparing' WHERE id=?",(sid,))
        ids=self.store.add_candidates(sid,[dict(title='Пост '+str(i),body='Проверенный текст '+str(i)*60,media='file-id',
            event_key='batch-event-'+str(sid)+'-'+str(i),meta={'mode':'news','sources':[]}) for i in range(count)],self.now)
        return sid,ids

    def test_both_posts_can_be_scheduled_at_independent_times_and_survive_restart(self):
        sid,ids=self.batch()
        self.store.schedule(ids[0],self.now+3600,self.now)
        self.store.schedule(ids[1],self.now+7200,self.now)
        restarted=Store(self.store.path)
        restarted.recover()
        self.assertEqual([restarted.candidate(i)['scheduled_at'] for i in ids],[self.now+3600,self.now+7200])
        engine=Engine(restarted,self.tg,None)
        engine.publish(self.now+3599);self.assertEqual(self.tg.published,[])
        engine.publish(self.now+3600);engine.publish(self.now+3601)
        self.assertEqual(len(self.tg.published),1)
        self.assertEqual(restarted.candidate(ids[1])['state'],'approved')
        engine.publish(self.now+7200)
        self.assertEqual(len(self.tg.published),2)
        self.assertEqual(restarted.slot(sid)['state'],'pending')

    def test_no_time_means_no_publication_and_no_twelve_hour_expiry(self):
        sid,ids=self.batch();engine=Engine(self.store,self.tg,None)
        engine.expire(self.now+3*86400);engine.publish(self.now+3*86400)
        self.assertEqual(self.tg.published,[])
        self.assertTrue(all(self.store.candidate(i)['state']=='pending' for i in ids))
        self.store.schedule(ids[1],self.now+3*86400+3600,self.now+3*86400)

    def test_reschedule_and_cancel_one_post_do_not_change_its_sibling(self):
        sid,ids=self.batch()
        self.store.schedule(ids[0],self.now+1800,self.now)
        self.store.schedule(ids[1],self.now+3600,self.now)
        self.store.schedule(ids[0],self.now+5400,self.now)
        self.store.unschedule(ids[0])
        self.assertIsNone(self.store.candidate(ids[0])['scheduled_at'])
        self.assertEqual(self.store.candidate(ids[1])['scheduled_at'],self.now+3600)
        self.assertEqual(self.store.candidate(ids[1])['state'],'approved')

    def test_same_channel_same_time_is_rejected_but_other_channel_is_allowed(self):
        sid,ids=self.batch()
        self.store.schedule(ids[0],self.now+3600,self.now)
        with self.assertRaises(ValueError):self.store.schedule(ids[1],self.now+3600,self.now)
        sid2,ids2=self.batch(cid=2)
        self.store.schedule(ids2[0],self.now+3600,self.now)

    def test_edit_revokes_only_that_posts_time(self):
        sid,ids=self.batch()
        for i,delta in zip(ids,(1800,3600)):self.store.schedule(i,self.now+delta,self.now)
        self.store.edit_candidate(ids[0],body='Изменённый текст')
        self.assertIsNone(self.store.candidate(ids[0])['scheduled_at'])
        self.assertEqual(self.store.candidate(ids[1])['state'],'approved')

    def test_schedule_still_checks_image_verification_and_future_time(self):
        sid,ids=self.batch()
        self.store.edit_candidate(ids[0],media='')
        with self.assertRaises(ValueError):self.store.schedule(ids[0],self.now+3600,self.now)
        self.store.execute('UPDATE candidates SET meta=? WHERE id=?',(json.dumps({'unverified':True}),ids[1]))
        with self.assertRaises(ValueError):self.store.schedule(ids[1],self.now+3600,self.now)
        with self.assertRaises(ValueError):self.store.schedule(ids[1],self.now-1,self.now)

    def test_filter_changes_keep_chosen_publications_but_target_changes_cancel_them(self):
        sid,ids=self.batch();self.store.schedule(ids[0],self.now+3600,self.now)
        ch=self.store.channel(1);ch['config']['topic']='Новая тема для следующих поисков'
        self.store.update_channel(1,config=ch['config'])
        self.assertEqual(self.store.candidate(ids[0])['state'],'approved')
        self.store.update_channel(1,target='-100999999999')
        self.assertIsNone(self.store.candidate(ids[0])['scheduled_at'])

    def test_uncertain_delivery_stops_without_repeating_or_claiming_sibling(self):
        sid,ids=self.batch()
        self.store.schedule(ids[0],self.now+60,self.now)
        self.store.schedule(ids[1],self.now+120,self.now)
        self.tg.fail_publish=True;engine=Engine(self.store,self.tg,None)
        engine.publish(self.now+60)
        self.assertEqual(self.store.candidate(ids[0])['state'],'unknown')
        self.assertEqual(self.store.candidate(ids[1])['state'],'approved')
        engine.publish(self.now+120)
        self.assertEqual(len(self.tg.published),1)
        self.store.recover();self.store.put('paused',False)
        engine.publish(self.now+61)
        self.assertEqual(len(self.tg.published),1)

    def test_recovery_finds_batch_inflight_without_mutating_parent(self):
        sid,ids=self.batch()
        self.store.schedule(ids[0],self.now+60,self.now)
        self.store.execute("UPDATE candidates SET state='publishing' WHERE id=?",(ids[0],))
        self.store.recover()
        self.assertTrue(self.store.get('paused'))
        self.assertEqual(self.store.candidate(ids[0])['state'],'unknown')
        self.assertEqual(self.store.slot(sid)['state'],'pending')

    def test_missed_time_becomes_draft_without_expiring_other_proposal(self):
        sid,ids=self.batch();self.store.schedule(ids[0],self.now+60,self.now)
        engine=Engine(self.store,self.tg,None)
        engine.expire(self.now+600);engine.publish(self.now+600)
        self.assertEqual(self.tg.published,[])
        self.assertEqual(self.store.candidate(ids[0])['state'],'draft')
        self.assertIn('пропущено',self.store.candidate(ids[0])['publication_error'])
        self.assertEqual(self.store.candidate(ids[1])['state'],'pending')

    def test_upgrade_moves_unapproved_old_posts_but_preserves_old_approved_queue(self):
        sid,ids=self.slot()
        self.store.approve(ids[0],self.now)
        self.store.put('manual_publication_v1',False)
        self.store.put('model','gpt-5.6-sol');self.store.put('coowners',[43])
        upgraded=Store(self.store.path)
        self.assertEqual(upgraded.candidate(ids[0])['batch_kind'],'legacy')
        self.assertEqual(upgraded.candidate(ids[0])['state'],'approved')
        self.assertEqual(upgraded.candidate(ids[1])['batch_kind'],'batch')
        self.assertEqual(upgraded.candidate(ids[1])['state'],'draft')
        self.assertEqual(upgraded.get('model'),'gpt-5.6-sol')
        self.assertEqual(upgraded.get('coowners'),[43])

    def test_turning_off_search_preserves_approved_legacy_publication(self):
        sid,ids=self.slot()
        self.store.approve(ids[0],self.now)
        ch=self.store.channel(1);ch['config']['auto_search']=False
        self.store.update_channel(1,config=ch['config'])
        self.assertEqual(self.store.candidate(ids[0])['state'],'approved')
        self.assertEqual(self.store.slot(sid)['state'],'queued')

    def test_date_parser_uses_moscow_and_never_silently_moves_past_time(self):
        at=int(datetime(2026,11,4,13,0,tzinfo=TZ).timestamp())
        self.assertEqual(datetime.fromtimestamp(publication_time('завтра 12:30',at),TZ).day,5)
        self.assertEqual(publication_time('04.11.2026 18:30',at),publication_time('18:30',at))
        self.assertEqual(publication_time('через 30 мин',at),at+1800)
        for value in ('12:00','31.02.2026 12:00','99:99','через 999999 часов'):
            with self.assertRaises(ValueError):publication_time(value,at)

    def test_rescheduling_between_selection_and_claim_does_not_publish_early(self):
        sid,ids=self.batch();self.store.schedule(ids[0],self.now+60,self.now)
        original=self.store.rows
        def rows(sql,params=()):
            result=original(sql,params)
            if 'SELECT p.id FROM candidates' in sql:
                self.store.schedule(ids[0],self.now+3600,self.now+60)
            return result
        with patch.object(self.store,'rows',side_effect=rows):Engine(self.store,self.tg,None).publish(self.now+60)
        self.assertEqual(self.tg.published,[])
        Engine(self.store,self.tg,None).publish(self.now+3600)
        self.assertEqual(len(self.tg.published),1)


class BatchSearchTests(Base):
    def sources(self):
        ids=[]
        for i,title in enumerate(('Научная станция открылась','Новый отчёт о торговле','Начались спортивные соревнования','Разработчики представили технологию')):
            ids.append(self.store.execute('''INSERT INTO articles(url,title,summary,source,published,image,fingerprint,fetched,text)
                VALUES(?,?,?,?,?,?,?,?,?)''',('https://example.org/'+str(i),title,'Подробный проверенный материал с фактами и описанием события. '+str(i)*50,
                'fixture',self.now-60,'file-id','fp-'+str(i),self.now,'Подробный проверенный материал с фактами и описанием события. '+str(i)*50)))
        parent=self
        class Sources:
            def collect(self,*a):return []
            def enrich(self,*a):pass
            def shortlist(self,*a):return parent.store.rows('SELECT * FROM articles')
        return Sources()

    def provider(self,one=False,fail_second=False,hook=None):
        parent=self
        class Provider:
            calls=0
            def call(self,cid,sid,instructions,context,web=False):
                self.calls+=1
                if fail_second and self.calls==2:raise AIError('Вторая попытка не удалась')
                if hook:hook(cid)
                articles=context['articles'][:1 if one else 2]
                return dict(options=[dict(title=a['title'],text='Проверенный материал о событии. '+str(a['id'])*65,
                     article_ids=[a['id']],event_key='event-'+str(a['id'])) for a in articles]),set()
        return Provider()

    def test_manual_search_outside_any_window_stops_after_two_and_does_not_publish(self):
        self.channel();sid=self.store.enqueue_search(1,self.now)
        ai=self.provider();engine=Engine(self.store,self.tg,ai,self.sources())
        engine.tick(self.now);engine.tick(self.now+1)
        posts=self.store.rows('SELECT * FROM candidates WHERE slot_id=?',(sid,))
        self.assertEqual(len(posts),2);self.assertEqual(ai.calls,1)
        self.assertTrue(all(p['scheduled_at'] is None for p in posts))
        self.assertEqual(self.tg.published,[])

    def test_one_at_a_time_stops_exactly_after_second_valid_post(self):
        self.channel();sid=self.store.enqueue_search(1,self.now)
        ai=self.provider(one=True);engine=Engine(self.store,self.tg,ai,self.sources())
        engine.prepare(sid,self.now)
        self.assertEqual(ai.calls,2)
        self.assertEqual(len(self.store.rows('SELECT * FROM candidates WHERE slot_id=?',(sid,))),2)
        engine.tick(self.now+2);self.assertEqual(ai.calls,2)

    def test_second_attempt_failure_keeps_first_post(self):
        self.channel();sid=self.store.enqueue_search(1,self.now)
        ai=self.provider(one=True,fail_second=True)
        Engine(self.store,self.tg,ai,self.sources()).prepare(sid,self.now)
        self.assertEqual(len(self.store.rows('SELECT * FROM candidates WHERE slot_id=?',(sid,))),1)
        self.assertEqual(self.store.slot(sid)['state'],'pending')

    def test_duplicate_click_reuses_active_job_and_stop_prevents_new_calls(self):
        self.channel();sid=self.store.enqueue_search(1,self.now)
        self.assertEqual(self.store.enqueue_search(1,self.now+1),sid)
        self.store.stop_search(1)
        ai=self.provider();engine=Engine(self.store,self.tg,ai,self.sources())
        # No scheduler tick is needed: a cancelled job must never be prepared.
        self.assertEqual(self.store.rows("SELECT * FROM slots WHERE state='new'"),[])
        self.assertEqual(ai.calls,0)

    def test_stop_during_paid_request_saves_found_posts_as_drafts(self):
        self.channel();sid=self.store.enqueue_search(1,self.now)
        ai=self.provider(hook=self.store.stop_search)
        Engine(self.store,self.tg,ai,self.sources()).prepare(sid,self.now)
        posts=self.store.rows('SELECT state FROM candidates WHERE slot_id=?',(sid,))
        self.assertEqual([p['state'] for p in posts],['draft','draft'])
        self.assertEqual(ai.calls,1)

    def test_auto_search_three_times_on_selected_channel_only(self):
        self.channel(1);self.channel(2)
        ch=self.store.channel(2);ch['config']['auto_search']=False;self.store.update_channel(2,config=ch['config'])
        engine=Engine(self.store,self.tg,None)
        for hour in (9,15,21):
            at=int(datetime(2026,11,4,hour,0,tzinfo=TZ).timestamp())
            engine.create_slots(at);engine.create_slots(at+1)
            self.store.execute("UPDATE slots SET state='pending' WHERE kind='batch'")
        rows=self.store.rows("SELECT * FROM slots WHERE kind='batch'")
        self.assertEqual(len(rows),3)
        self.assertEqual({r['channel_id'] for r in rows},{1})

    def test_restart_does_not_repeat_finished_automatic_batch(self):
        self.channel();at=int(datetime(2026,11,4,9,0,tzinfo=TZ).timestamp())
        Engine(self.store,self.tg,None).create_slots(at)
        self.store.execute("UPDATE slots SET state='pending'")
        Engine(Store(self.store.path),self.tg,None).create_slots(at+5)
        self.assertEqual(len(self.store.rows('SELECT * FROM slots')),1)

    def test_turning_off_auto_search_does_not_cancel_current_manual_search(self):
        self.channel();sid=self.store.enqueue_search(1,self.now)
        ch=self.store.channel(1);ch['config']['auto_search']=False
        self.store.update_channel(1,config=ch['config'])
        self.assertEqual(self.store.slot(sid)['state'],'new')
        self.assertFalse(self.store.channel(1)['config']['auto_search'])

    def test_clothing_batch_returns_two_outfits_without_restoring_six_category_searches(self):
        from test_fashion import ProductAI
        self.channel(5);sid=self.store.enqueue_search(5,self.now)
        ai=ProductAI(spare=True)
        with patch('smm.fashion.verify_product',side_effect=lambda p,n:dict(p,verified=True,checked_at=n,available=True)):
            Engine(self.store,self.tg,ai).prepare(sid,self.now)
        self.assertEqual(len(self.store.rows('SELECT * FROM candidates WHERE slot_id=?',(sid,))),2)
        self.assertEqual(len(ai.calls),1)


class PlanningUITests(Base):
    batch=PlanningTests.batch
    def setUp(self):
        super().setUp();self.store.put('owner',42)
        self.ui=PanelUI(self.store,self.tg,FakeAuth(),None)
        self.tg.call=lambda *a,**kw:{}

    def message(self,text,user=42):
        self.ui.handle({'message':{'from':{'id':user},'chat':{'id':user,'type':'private'},'text':text}})

    def click(self,data,user=42):
        self.ui.handle({'callback_query':{'id':'q','from':{'id':user},'data':data,'message':{'chat':{'id':user,'type':'private'}}}})

    def test_user_picks_time_and_back_does_not_schedule_again(self):
        sid,ids=self.batch();self.message('📰 СММ');self.click('post:'+str(ids[0]))
        self.click('schedule:'+str(ids[0]))
        self.assertIsNotNone(self.store.session(42))
        with patch('smm.ui.time.time',return_value=self.now):self.message('через 30 мин')
        self.assertEqual(self.store.candidate(ids[0])['scheduled_at'],self.now+1800)
        self.assertEqual(self.ui.nav()['stack'][-1],'post:'+str(ids[0]))
        self.click('nav:back')
        self.assertFalse(any(r.startswith(('schedule:','at:')) for r in self.ui.nav()['stack']))

    def test_cancel_time_form_preserves_existing_schedule(self):
        sid,ids=self.batch();self.store.schedule(ids[0],self.now+3600,self.now)
        self.click('post:'+str(ids[0]));self.click('schedule:'+str(ids[0]));self.click('nav:cancel')
        self.assertEqual(self.store.candidate(ids[0])['scheduled_at'],self.now+3600)

    def test_stranger_cannot_schedule_post(self):
        sid,ids=self.batch();self.click('at:'+str(ids[0])+':30',user=66)
        self.assertIsNone(self.store.candidate(ids[0])['scheduled_at'])

    def test_manual_search_from_manager_switches_to_correct_section(self):
        self.message('📣 Менеджер');self.message('🔎 Поиск постов');self.click('find:1')
        self.assertEqual(self.ui.nav()['section'],'smm')
        self.assertEqual(len(self.store.rows("SELECT * FROM slots WHERE kind='batch' AND state='new'")),1)
