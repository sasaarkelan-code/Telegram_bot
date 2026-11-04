from test_core import Base
from smm.navigation import PanelUI


class NavigationTests(Base):
    def setUp(self):
        super().setUp()
        self.store.put('owner',42)
        self.ui=PanelUI(self.store,self.tg,None,None)
        self.calls=[]
        def call(method, **kw):
            self.calls.append((method,kw))
            return {}
        self.tg.call=call

    def message(self,text,user=42):
        self.ui.handle({'message':{'from':{'id':user},'chat':{'id':user,'type':'private'},'text':text}})

    def click(self,data,user=42):
        self.ui.handle({'callback_query':{'id':'q','from':{'id':user},'data':data,
                       'message':{'message_id':1,'text':'old screen','chat':{'id':user,'type':'private'}}}})

    def labels(self, markup):
        return [b['text'] for row in markup.get('keyboard',[]) for b in row]

    def test_manager_replaces_smm_reply_keyboard(self):
        self.message('/start')
        self.message('📰 СММ')
        self.assertIn('📥 Предложения',self.labels(self.tg.messages[-1][2]))
        self.message('📣 Менеджер')
        labels=self.labels(self.tg.messages[-1][2])
        self.assertIn('📝 Кампании',labels)
        self.assertNotIn('📥 Предложения',labels)
        self.assertIn('🏠 Главное меню',labels)

    def test_root_contains_only_root_actions(self):
        self.message('📣 Менеджер')
        self.message('/menu')
        labels=self.labels(self.tg.messages[-1][2])
        self.assertEqual(set(labels),{'📰 СММ','📣 Менеджер','👥 Совладельцы','🌐 Веб-панель'})

    def test_form_cancel_returns_to_campaigns_and_clears_input(self):
        self.message('📣 Менеджер')
        self.click('workspace:campaigns')
        self.click('workspace:new_campaign')
        self.assertIsNotNone(self.store.session(42))
        self.click('nav:cancel')
        self.assertIsNone(self.store.session(42))
        self.assertEqual(self.ui.nav()['stack'][-1],'workspace:campaigns')
        self.assertEqual(self.store.get('manager_campaigns',[]),[])

    def test_bad_input_keeps_form_and_has_navigation(self):
        self.message('📣 Менеджер')
        self.click('workspace:new_campaign')
        self.message('x'*101)
        self.assertIsNotNone(self.store.session(42))
        data=[b['callback_data'] for row in self.tg.messages[-1][2]['inline_keyboard'] for b in row]
        self.assertIn('nav:cancel',data)
        self.assertIn('nav:home',data)

    def test_home_cancels_pending_input(self):
        self.click('workspace:invite')
        self.message('🏠 Главное меню')
        self.message('123456')
        self.assertEqual(self.store.get('team_invite',{}),{})

    def test_back_from_channel_filter_restores_filter_list(self):
        self.message('📰 СММ')
        self.message('🎛 Фильтры')
        self.click('filters:1')
        self.click('field:1:topic')
        self.click('nav:back')
        self.assertEqual(self.ui.nav()['stack'][-1],'filters:1')
        self.click('nav:back')
        self.assertEqual(self.ui.nav()['stack'][-1],'filter_list')
        self.click('nav:back')
        self.assertEqual(self.ui.nav()['stack'][-1],'smm')
        self.click('nav:back')
        self.assertEqual(self.ui.nav()['stack'][-1],'root')

    def test_empty_states_have_back_and_home(self):
        self.message('📰 СММ')
        self.message('✅ Очередь')
        data=[b['callback_data'] for row in self.tg.messages[-1][2]['inline_keyboard'] for b in row]
        self.assertIn('nav:back',data)
        self.assertIn('nav:home',data)

    def test_old_smm_callback_switches_keyboard_and_section(self):
        self.message('📣 Менеджер')
        self.click('filters:1')
        self.assertEqual(self.ui.nav()['section'],'smm')
        keyboards=[m[2] for m in self.tg.messages if m[2] and 'keyboard' in m[2]]
        self.assertIn('📥 Предложения',self.labels(keyboards[-1]))
        self.assertNotIn('📝 Кампании',self.labels(keyboards[-1]))
        self.assertNotIn('manager',self.ui.nav()['stack'])

    def test_notification_preserves_manager_navigation_and_keyboard(self):
        self.message('📣 Менеджер')
        before=self.ui.nav()
        self.slot()
        self.ui.deliver()
        self.assertEqual(before,self.ui.nav())
        for _,_,markup in self.tg.messages[1:]:
            if markup:
                self.assertNotIn('keyboard',markup)

    def test_coowner_navigation_is_independent(self):
        self.store.put('coowners',[43])
        self.message('📣 Менеджер')
        self.message('📰 СММ',43)
        self.assertEqual(self.store.get('navigation:42')['section'],'manager')
        self.assertEqual(self.store.get('navigation:43')['section'],'smm')

    def test_delete_needs_confirmation_and_back_does_not_delete(self):
        self.store.put('manager_campaigns',[{'id':'abc','name':'Test','text':'text','targets':[]}])
        self.message('📣 Менеджер')
        self.click('workspace:campaigns')
        self.click('workspace:campaign_abc')
        self.click('workspace:delete_abc')
        self.assertEqual(len(self.store.get('manager_campaigns')),1)
        self.click('nav:back')
        self.assertEqual(len(self.store.get('manager_campaigns')),1)
        self.assertEqual(self.ui.nav()['stack'][-1],'workspace:campaign_abc')
        self.click('workspace:delete_abc')
        self.click('workspace:confirm_delete_abc')
        self.assertEqual(self.store.get('manager_campaigns'),[])
        self.click('nav:back')
        self.assertEqual(self.ui.nav()['stack'][-1],'manager')

    def test_saved_form_does_not_return_to_form(self):
        self.message('📣 Менеджер')
        self.click('workspace:new_campaign')
        self.message('Test')
        self.message('text')
        self.message('@example')
        self.assertIsNone(self.store.session(42))
        self.assertEqual(self.ui.nav()['stack'][-1],'workspace:campaigns')
        self.click('nav:back')
        self.assertEqual(self.ui.nav()['stack'][-1],'manager')

    def test_click_uses_edit_without_new_screen_message(self):
        self.message('📣 Менеджер')
        n=len(self.tg.messages)
        self.click('workspace:account')
        self.assertEqual(len(self.tg.messages),n)
        self.assertTrue(any(m=='editMessageText' for m,_ in self.calls))

    def test_navigation_survives_new_ui_instance(self):
        self.message('📣 Менеджер')
        self.click('workspace:partners')
        self.ui=PanelUI(self.store,self.tg,None,None)
        self.message('/back')
        self.assertEqual(self.ui.nav()['stack'][-1],'manager')

    def test_post_returns_to_original_queue_page(self):
        _,ids=self.slot()
        self.message('📰 СММ')
        self.ui.pending(2,True)
        self.click('post:'+str(ids[0]))
        self.click('nav:back')
        self.assertEqual(self.ui.nav()['stack'][-1],'pending:2:1')

    def test_filter_save_then_back_never_replays_form(self):
        self.message('📰 СММ')
        self.message('🎛 Фильтры')
        self.click('filters:1')
        self.click('field:1:topic')
        self.message('Новая тематика')
        self.assertIsNone(self.store.session(42))
        self.assertNotIn('form',self.ui.nav()['stack'])
        self.click('nav:back')
        self.assertEqual(self.ui.nav()['stack'][-1],'filter_list')
        self.assertEqual(self.store.channel(1)['config']['topic'],'Новая тематика')

    def test_section_switch_cancels_form(self):
        self.click('workspace:new_campaign')
        self.message('📰 СММ')
        self.assertIsNone(self.store.session(42))
        self.message('text | text | @example')
        self.assertEqual(self.store.get('manager_campaigns',[]),[])

    def test_repeated_navigation_has_no_cycles(self):
        self.message('📣 Менеджер')
        for _ in range(10):
            self.click('workspace:campaigns')
            self.click('nav:back')
        self.assertEqual(self.ui.nav()['stack'],['root','manager'])
        self.assertEqual(self.tg.published,[])

    def test_unknown_text_in_manager_preserves_section(self):
        self.message('📣 Менеджер')
        self.message('random text')
        self.assertEqual(self.ui.nav()['section'],'manager')
        data=[b['callback_data'] for row in self.tg.messages[-1][2]['inline_keyboard'] for b in row]
        self.assertIn('nav:home',data)

    def test_old_approval_button_switches_to_smm(self):
        _,ids=self.slot()
        self.message('📣 Менеджер')
        self.click('approve:'+str(ids[0]))
        self.assertEqual(self.ui.nav()['section'],'smm')
        self.assertEqual(self.store.candidate(ids[0])['state'],'approved')
        self.assertEqual(self.tg.published,[])


    def test_wizard_back_and_cancel_do_not_create_campaign(self):
        self.message('📣 Менеджер')
        self.click('workspace:new_campaign')
        self.message('Campaign')
        self.message('Advertising text')
        self.click('nav:back')
        import json
        self.assertEqual(json.loads(self.store.session(42)['payload'])['step'],1)
        self.click('nav:cancel')
        self.assertIsNone(self.store.session(42))
        self.assertEqual(self.store.get('manager_campaigns',[]),[])
        self.assertEqual(self.ui.nav()['stack'][-1],'manager')

    def test_wizard_validates_and_deduplicates_groups(self):
        self.message('📣 Менеджер')
        self.click('workspace:new_campaign')
        self.message('Campaign')
        self.message('Advertising text')
        self.message('invalid')
        self.assertIsNotNone(self.store.session(42))
        self.assertEqual(self.store.get('manager_campaigns',[]),[])
        self.message('@example, @example')
        self.assertEqual(self.store.get('manager_campaigns')[0]['targets'],['@example'])
