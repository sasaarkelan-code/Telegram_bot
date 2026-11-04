import json
import threading
from unittest.mock import patch

from test_core import Base
from smm.manager import Manager, Rejected, WaitRequired, parse_targets
from smm.navigation import PanelUI


class Gateway:
    def __init__(self,clock):
        self.clock=clock
        self.sent=[]
        self.resolved=[]
        self.error=None
        self.hook=None
        self.searches=0

    def resolve_group(self,ref):
        self.resolved.append(ref)
        if self.error: raise self.error
        return dict(key=ref,ref=ref,title=ref,kind='channel',id=len(self.resolved),access_hash=0)

    def send_group(self,peer,text,random_id):
        self.sent.append((self.clock(),peer['ref'],text,random_id))
        if self.hook: self.hook()
        if self.error: raise self.error
        return 123

    def find_partners(self,criteria,seen):
        self.searches+=1
        for i in range(15):
            if str(i) not in seen:
                yield dict(peer=str(i),title='Канал '+str(i),url=f'https://t.me/channel_{i:03}',
                           about='Нейросети. Открыты к сотрудничеству',subscribers=1000+i,
                           origin='Поиск: нейросети')


class ManagerTests(Base):
    def setUp(self):
        super().setUp()
        self.at=self.now
        self.clock=lambda:self.at
        self.gateway=Gateway(self.clock)
        self.manager=Manager(self.store,self.gateway,self.clock)
        self.store.put('manager_account',dict(connected=True,user_id=42))

    def campaign(self,n=3):
        item=self.manager.create('Реклама','Один общий пост',[f'@group_{i:03}' for i in range(n)])
        self.manager.start(item['id'])
        for _ in range(n): self.manager.tick()
        return item['id']

    def test_three_groups_spacing_and_thirty_minute_round(self):
        cid=self.campaign()
        for offset in (0,1,119,120,121,239,240,241,1799,1800,1920,2040):
            self.at=self.now+offset
            self.manager.tick()
        self.assertEqual([x[0]-self.now for x in self.gateway.sent],[0,120,240,1800,1920,2040])
        self.assertEqual([x[1] for x in self.gateway.sent],['@group_000','@group_001','@group_002']*2)
        self.assertEqual(len({x[3] for x in self.gateway.sent}),6)

    def test_sixteen_groups_extend_cycle_instead_of_overlap(self):
        cid=self.campaign(16)
        for offset in range(0,1801,120):
            self.at=self.now+offset
            self.manager.tick()
        self.assertEqual(len(self.gateway.sent),16)
        self.assertEqual(self.manager.campaign(cid)['next_at'],self.now+1920)
        self.at=self.now+1801; self.manager.tick()
        self.assertEqual(len(self.gateway.sent),16)
        self.at=self.now+1920; self.manager.tick()
        self.assertEqual(len(self.gateway.sent),17)

    def test_stop_and_resume_preserves_position(self):
        cid=self.campaign(); self.manager.tick()
        self.manager.stop(cid)
        self.at+=30; self.manager.tick()
        self.manager.start(cid); self.manager.tick()
        self.assertEqual(len(self.gateway.sent),1)
        self.at=self.now+120; self.manager.tick()
        self.assertEqual(self.gateway.sent[-1][1],'@group_001')

    def test_stop_during_inflight_send_prevents_next_send(self):
        cid=self.campaign()
        self.gateway.hook=lambda:self.manager.stop(cid)
        self.manager.tick()
        self.at+=120; self.manager.tick()
        self.assertEqual(len(self.gateway.sent),1)
        self.assertEqual(self.manager.campaign(cid)['state'],'stopped')
        self.assertEqual(self.manager.campaign(cid)['cursor'],1)

    def test_new_text_applies_only_to_future_sends_and_is_logged(self):
        cid=self.campaign(); self.manager.tick()
        self.manager.edit(cid,'text','Новый общий пост')
        self.at+=120; self.manager.tick()
        self.assertEqual([x[2] for x in self.gateway.sent],['Один общий пост','Новый общий пост'])
        rows=self.store.rows('SELECT version,body FROM manager_deliveries ORDER BY id')
        self.assertEqual([x['version'] for x in rows],[1,2])
        self.assertEqual(rows[0]['body'],'Один общий пост')

    def test_double_start_does_not_reset_round(self):
        cid=self.campaign(); self.manager.tick()
        self.manager.start(cid); self.manager.start(cid)
        self.manager.tick()
        self.assertEqual(len(self.gateway.sent),1)
        self.assertEqual(self.manager.campaign(cid)['cursor'],1)

    def test_no_sends_without_account(self):
        self.store.put('manager_account',dict(connected=False))
        item=self.manager.create('Реклама','Текст',['@group_000'])
        with self.assertRaises(ValueError): self.manager.start(item['id'])
        self.manager.tick()
        self.assertEqual(self.gateway.sent,[])

    def test_missing_group_permission_stops_preflight_before_messages(self):
        item=self.manager.create('Реклама','Текст',['@group_000'])
        self.gateway.error=Rejected('Писать запрещено')
        self.manager.start(item['id']); self.manager.tick()
        self.assertEqual(self.gateway.sent,[])
        self.assertEqual(self.manager.campaign(item['id'])['state'],'stopped')

    def test_aliases_of_same_group_are_deduplicated_after_resolution(self):
        item=self.manager.create('Реклама','Текст',['@group_000','@group_alias'])
        self.gateway.resolve_group=lambda ref:dict(key='-10012345',ref=ref,title='Одна группа')
        self.manager.start(item['id']); self.manager.tick(); self.manager.tick()
        self.assertEqual(len(self.manager.campaign(item['id'])['resolved']),1)

    def test_flood_wait_stops_and_requires_explicit_restart_after_deadline(self):
        cid=self.campaign()
        self.gateway.error=WaitRequired(3600)
        self.manager.tick()
        self.assertEqual(self.manager.campaign(cid)['state'],'stopped')
        self.assertEqual(self.store.get('manager_cooldown'),self.now+3600)
        self.assertEqual(self.store.rows('SELECT state FROM manager_deliveries')[0]['state'],'blocked')
        with self.assertRaises(ValueError): self.manager.start(cid)
        self.at+=3601; self.gateway.error=None; self.manager.tick()
        self.assertEqual(len(self.gateway.sent),1)

    def test_uncertain_result_is_never_automatically_retried(self):
        cid=self.campaign()
        self.gateway.error=TimeoutError()
        self.manager.tick()
        self.at+=1800; self.manager.tick()
        row=self.store.rows('SELECT * FROM manager_deliveries')[0]
        self.assertEqual(row['state'],'unknown')
        self.assertEqual(len(self.gateway.sent),1)
        with self.assertRaises(ValueError): self.manager.start(cid)
        self.manager.resolve(row['id'],False)
        self.gateway.error=None
        self.at+=120
        self.manager.start(cid); self.manager.tick()
        self.assertEqual(self.gateway.sent[-1][1],'@group_001')

    def test_restart_recovers_inflight_to_unknown(self):
        cid=self.campaign()
        self.store.execute('''INSERT INTO manager_deliveries(campaign,idx,target,started,day,state,random_id,version,body)
                            VALUES(?,?,?,?,'2026-11-03','sending',123,1,'Пост')''',(cid,0,'@group_000',self.now))
        Manager(self.store,self.gateway,self.clock).recover()
        self.assertEqual(self.store.rows('SELECT state FROM manager_deliveries')[0]['state'],'unknown')
        self.assertEqual(self.manager.campaign(cid)['state'],'stopped')

    def test_daily_limit_is_persistent_and_stops_before_extra_send(self):
        cid=self.campaign()
        self.manager.edit(cid,'daily_limit','1')
        self.manager.start(cid)
        for _ in range(3): self.manager.tick()
        self.manager.tick()
        self.at+=120
        Manager(self.store,self.gateway,self.clock).tick()
        self.assertEqual(len(self.gateway.sent),1)
        self.assertEqual(self.manager.campaign(cid)['state'],'stopped')

    def test_only_one_active_campaign_on_account(self):
        self.campaign()
        second=self.manager.create('Вторая','Текст',['@second_group'])
        with self.assertRaises(ValueError): self.manager.start(second['id'])

    def test_no_catchup_burst_after_long_downtime(self):
        cid=self.campaign(); self.manager.tick()
        self.at+=7200
        for _ in range(10): self.manager.tick()
        self.assertEqual(len(self.gateway.sent),2)
        self.assertGreaterEqual(self.manager.campaign(cid)['next_at'],self.at+120)

    def test_target_edit_and_restart_cannot_resend_same_group_early(self):
        cid=self.campaign(1); self.manager.tick()
        self.manager.edit(cid,'targets','@group_000')
        self.at+=120
        self.manager.start(cid); self.manager.tick(); self.manager.tick()
        self.assertEqual(len(self.gateway.sent),1)
        self.assertEqual(self.manager.campaign(cid)['next_at'],self.now+1800)

    def test_concurrent_ticks_cannot_claim_same_send_twice(self):
        cid=self.campaign()
        began=threading.Event(); finish=threading.Event()
        self.gateway.hook=lambda:(began.set(),finish.wait(2))
        first=threading.Thread(target=self.manager.tick)
        first.start(); self.assertTrue(began.wait(2))
        self.manager.tick(); finish.set(); first.join(2)
        self.assertEqual(len(self.gateway.sent),1)

    def test_partner_search_saves_up_to_ten_without_messaging(self):
        self.store.put('manager_partner_filter',dict(queries=['нейросети'],min=1002,max=1011))
        self.manager.queue_search(); self.manager.tick()
        self.assertEqual(len(self.store.rows('SELECT * FROM manager_channels')),10)
        self.assertEqual(self.gateway.sent,[])
        self.assertEqual(self.store.get('manager_search')['state'],'done')

    def test_next_partner_search_does_not_repeat_saved_channels(self):
        self.store.put('manager_partner_filter',dict(queries=['нейросети']))
        self.manager.queue_search(); self.manager.tick()
        self.at+=120
        self.manager.queue_search(); self.manager.tick()
        self.assertEqual(len(self.store.rows('SELECT * FROM manager_channels')),15)
        self.assertEqual(self.gateway.sent,[])

    def test_repeated_search_click_does_not_queue_duplicate_job(self):
        self.store.put('manager_partner_filter',dict(queries=['нейросети']))
        self.manager.queue_search()
        with self.assertRaises(ValueError): self.manager.queue_search()
        self.manager.tick()
        with self.assertRaises(ValueError): self.manager.queue_search()
        self.assertEqual(self.gateway.searches,1)

    def test_partners_do_not_become_mailing_targets(self):
        cid=self.campaign(1)
        self.manager.stop(cid)
        self.store.put('manager_partner_filter',dict(queries=['нейросети']))
        self.manager.queue_search(); self.manager.tick()
        self.assertEqual(self.manager.campaign(cid)['targets'],['@group_000'])

    def test_parse_links_and_deduplicate_case(self):
        self.assertEqual(parse_targets('https://t.me/Group_One\n@group_one, -100123456'),['@group_one','-100123456'])
        for value in ('https://t.me/+secret','https://example.org/group','https://t.me/group_one/123'):
            with self.assertRaises(ValueError): parse_targets(value)

    def test_text_limit_counts_telegram_utf16_characters(self):
        with self.assertRaises(ValueError): self.manager.create('Тест','😀'*2000,['@group_000'])

    def test_short_flood_wait_does_not_impose_false_thirty_minute_group_hold(self):
        cid=self.campaign(1)
        self.gateway.error=WaitRequired(60); self.manager.tick()
        self.at+=120; self.gateway.error=None
        self.manager.start(cid); self.manager.tick()
        self.assertEqual(len(self.gateway.sent),2)
        self.assertEqual(self.store.rows('SELECT state FROM manager_deliveries ORDER BY id')[-1]['state'],'done')

    def test_old_drafts_are_upgraded_without_changing_their_text(self):
        self.store.put('manager_campaigns',[dict(id='old',name='Старая',text='Мой текст',targets=['@group_000'],interval_minutes=30,state='draft')])
        item=self.manager.campaign('old')
        self.assertEqual(item['text'],'Мой текст')
        self.assertEqual(item['gap_minutes'],2)
        self.assertEqual(item['cycle_minutes'],30)
        self.assertEqual(item['state'],'draft')


class ManagerUITests(Base):
    def setUp(self):
        super().setUp()
        self.store.put('owner',42)
        self.store.put('manager_account',dict(connected=True,user_id=42))
        self.ui=PanelUI(self.store,self.tg,None,None)
        self.tg.call=lambda *args,**kw:{}

    def message(self,text,user=42):
        self.ui.handle(dict(message=dict(text=text,**{'from':{'id':user}},chat=dict(id=user,type='private'))))

    def click(self,data,user=42):
        self.ui.handle(dict(callback_query=dict(id='q',data=data,**{'from':{'id':user}},message=dict(chat=dict(id=user,type='private')))))

    def item(self):
        self.message('📣 Менеджер'); self.click('workspace:new_campaign')
        self.message('Реклама'); self.message('Пост'); self.message('https://t.me/group_one\n@group_two')
        return self.store.get('manager_campaigns')[0]['id']

    def test_campaign_edit_preserves_manager_section_and_back(self):
        cid=self.item(); self.click('workspace:campaign_'+cid)
        self.click('mgr:edit:text:'+cid); self.message('Новый пост')
        self.assertEqual(self.ui.manager_service.campaign(cid)['text'],'Новый пост')
        self.assertEqual(self.ui.nav()['section'],'manager')
        self.assertNotIn('mgr:form',self.ui.nav()['stack'])
        self.click('nav:back')
        self.assertEqual(self.ui.nav()['stack'][-1],'workspace:campaigns')

    def test_back_does_not_replay_start_or_search(self):
        cid=self.item(); self.click('workspace:campaign_'+cid)
        self.click('mgr:start:'+cid)
        self.assertEqual(self.ui.manager_service.campaign(cid)['state'],'checking')
        self.click('nav:back'); self.click('nav:back')
        self.assertFalse(any(x.startswith('mgr:start') for x in self.ui.nav()['stack']))
        self.assertEqual(self.store.rows('SELECT * FROM manager_deliveries'),[])

    def test_stop_all_is_separate_from_smm_pause(self):
        cid=self.item(); self.click('mgr:start:'+cid)
        self.message('🛑 Остановить всё')
        self.assertEqual(self.ui.manager_service.campaign(cid)['state'],'stopped')
        self.assertFalse(self.store.get('paused'))

    def test_stranger_cannot_start_or_change_campaign(self):
        cid=self.item()
        self.click('mgr:start:'+cid,user=66)
        self.assertEqual(self.ui.manager_service.campaign(cid)['state'],'draft')

    def test_partner_filter_forms_have_cancel_and_validations(self):
        self.message('📣 Менеджер'); self.click('workspace:partners')
        self.click('mgr:partner_filters'); self.click('mgr:partner_edit:range')
        self.message('50000 1000')
        self.assertIsNotNone(self.store.session(42))
        self.message('1000 50000')
        self.assertEqual(self.store.get('manager_partner_filter')['min'],1000)
        self.assertNotIn('mgr:form',self.ui.nav()['stack'])
        self.click('nav:back')
        self.assertEqual(self.ui.nav()['section'],'manager')

    def test_existing_manual_partner_contacts_remain_accessible(self):
        self.store.put('manager_partners',['@old_contact — уже общались'])
        self.message('📣 Менеджер'); self.click('mgr:contacts:0')
        self.assertIn('@old_contact',self.tg.messages[-1][1])
