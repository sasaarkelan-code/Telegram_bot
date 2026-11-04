import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from smm.store import Store
from smm.web_access import ACCESS_BUTTON, ACCESS_KEYBOARD, WebAccess
from smm.webapp import install_tables


class WebAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'smm.sqlite')
        install_tables(self.store)
        self.store.put('owner',42)
        self.tg=Mock()
        self.gate=WebAccess(self.store,self.tg,{'SMM_WEB_URL':'https://studio.example'})

    def tearDown(self): self.tmp.cleanup()

    def message(self, actor, text, chat_type='private', chat_id=None):
        return self.gate.handle({'message':{'from':{'id':actor,'username':'user_name'},
            'chat':{'id':actor if chat_id is None else chat_id,'type':chat_type},'text':text}})

    def test_key_issues_code_only_in_own_private_chat(self):
        self.message(42,ACCESS_BUTTON)
        self.assertEqual(len(self.store.rows('SELECT * FROM web_codes')),1)
        self.assertEqual(self.tg.send.call_args.args[0],42)
        self.assertEqual(self.tg.send.call_args.args[2],ACCESS_KEYBOARD)
        self.assertIn('https://studio.example',self.tg.send.call_args.args[1])
        self.assertIn('user_name',self.tg.send.call_args.args[1])

    def test_stranger_group_and_spoofed_chat_get_no_code(self):
        self.message(43,'/web')
        self.message(42,'/web',chat_type='supergroup')
        self.message(42,'/web',chat_id=43)
        self.assertFalse(self.tg.send.called)
        self.assertEqual(self.store.rows('SELECT * FROM web_codes'),[])

    def test_old_commands_and_forms_cannot_change_state(self):
        self.store.put('paused',False)
        self.store.session(42,'text',{'pid':7})
        for text in ['/start','🛑 Стоп','▶️ Продолжить','📥 Предложения','replace old post']:
            self.message(42,text)
        self.assertFalse(self.store.get('paused'))
        self.assertIsNone(self.store.session(42))
        self.assertEqual(self.store.rows('SELECT * FROM web_codes'),[])
        for call in self.tg.send.call_args_list:
            self.assertEqual(call.args[2],ACCESS_KEYBOARD)

    def test_old_admin_callback_is_inert(self):
        self.store.put('paused',False)
        self.gate.handle({'callback_query':{'id':'query','from':{'id':42},'data':'halt',
            'message':{'chat':{'type':'private','id':42}}}})
        self.assertFalse(self.store.get('paused'))
        self.tg.call.assert_called_once_with('answerCallbackQuery',callback_query_id='query',text='Управление перенесено на сайт.')

    def test_invitation_is_bound_to_recipient_single_use(self):
        self.store.put('team_invite',{'user':43,'code':'invite-code','expires':time.time()+100})
        self.message(44,'/join invite-code')
        self.assertEqual(self.store.get('coowners',[]),[])
        self.message(43,'/join invite-code')
        self.assertEqual(self.store.get('coowners'),[43])
        self.message(43,'/web')
        self.assertEqual(self.store.rows('SELECT actor FROM web_codes')[0]['actor'],43)
        self.assertEqual(self.store.get('team_invite'),{})

    def test_expired_invite_rejected_and_revoked_actor_cannot_issue_code(self):
        self.store.put('team_invite',{'user':43,'code':'invite-code','expires':1})
        self.message(43,'/join invite-code')
        self.message(43,'/web')
        self.assertEqual(self.store.rows('SELECT * FROM web_codes'),[])

    def test_pairing_still_requires_setup_code(self):
        self.store.put('owner',0)
        self.message(42,'/start')
        self.assertEqual(self.store.get('owner'),0)
        code=self.store.pair_code()
        self.message(42,'/start '+code)
        self.assertEqual(self.store.get('owner'),42)

    def test_command_menu_contains_only_web(self):
        self.gate.configure()
        self.assertEqual([x['command'] for x in self.tg.call.call_args.kwargs['commands']],['web'])

    def test_existing_notices_are_never_pushed_by_access_handler(self):
        self.store.notice('event','Private work notification')
        self.message(42,'/start')
        self.assertNotIn('Private work notification',self.tg.send.call_args.args[1])
        self.assertEqual(self.store.rows("SELECT delivered FROM notices WHERE notice_key='event'")[0]['delivered'],0)

    def test_runtime_keeps_publisher_but_never_pushes_pending_notices(self):
        from smm.__main__ import run
        class FastEvent(threading.Event):
            def wait(self, timeout=None):
                return super().wait(min(timeout or .01,.01))
        engine=Mock(); engine.shutdown=FastEvent(); engine.wake=threading.Event()
        publisher_started=threading.Event()
        engine.loop.side_effect=lambda: engine.shutdown.wait(.01)
        engine.publisher_loop.side_effect=lambda: publisher_started.set()
        self.store.notice('waiting-notice','Do not send this to Telegram')
        self.store.put('paused',False)
        def poll(*args):
            time.sleep(.1)
            raise KeyboardInterrupt
        self.tg.updates.side_effect=poll
        manager=Mock()
        with patch('smm.__main__.read_config',return_value={'telegram_token':'fake-test-token'}), \
             patch('smm.telegram.Telegram',return_value=self.tg), \
             patch('smm.engine.Engine',return_value=engine), \
             patch('smm.manager.Manager',return_value=manager), \
             patch('smm.webapp.start_server'), patch('signal.signal'), \
             patch.dict('os.environ',{'SMM_WEB_ENABLED':'1'}), patch('builtins.print'):
            run(Path(self.tmp.name),self.store)
        self.assertTrue(publisher_started.is_set())
        self.tg.send.assert_not_called()
        self.assertEqual(self.store.rows("SELECT delivered FROM notices WHERE notice_key='waiting-notice'")[0]['delivered'],0)
