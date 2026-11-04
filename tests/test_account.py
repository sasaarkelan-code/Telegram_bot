import asyncio
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from smm.account import AccountGateway, login_cli
from smm.manager import Rejected, WaitRequired


class AccountAdapterTests(unittest.TestCase):
    """Contract tests for account boundaries; no real Telegram credentials."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        (self.root/'manager.session').write_bytes(b'fixture-not-a-real-session')
        self.env=patch.dict('os.environ',dict(MANAGER_ENABLED='1',MANAGER_API_ID='12345',MANAGER_API_HASH='a'*32))
        self.env.start(); self.addCleanup(self.env.stop)
        self.types=types.SimpleNamespace()
        for name in ('Chat','Channel','InputPeerChannel','InputPeerChat','UpdateShortSentMessage','UpdateNewMessage','UpdateNewChannelMessage'):
            setattr(self.types,name,type(name,(),{'__init__':lambda obj,*a,**kw:obj.__dict__.update(kw)}))
        self.entity=self.types.Channel(id=5,title='Группа',megagroup=True,left=False,default_banned_rights=None)
        self.requests=[]; self.client_options={}; self.failure=None
        parent=self
        class Client:
            def __init__(self,*args,**kw): parent.client_options.update(kw)
            def is_connected(self): return True
            async def is_user_authorized(self): return True
            async def get_me(self): return types.SimpleNamespace(id=42,first_name='Тест',bot=False)
            async def get_entity(self,value): return parent.entity
            async def get_permissions(self,*args): return types.SimpleNamespace(is_admin=False,is_creator=False,is_banned=False)
            async def get_input_entity(self,entity): return parent.types.InputPeerChannel(id=entity.id,access_hash=99)
            async def disconnect(self): pass
            async def __call__(self,request):
                parent.requests.append(request)
                if parent.failure: raise parent.failure
                return parent.types.UpdateShortSentMessage(id=777)
        sdk=types.SimpleNamespace(TelegramClient=Client,types=self.types,
            functions=types.SimpleNamespace(messages=types.SimpleNamespace(SendMessageRequest=lambda **kw:types.SimpleNamespace(**kw))),
            utils=types.SimpleNamespace(get_peer_id=lambda entity:-100000-entity.id))
        self.modules=patch.dict('sys.modules',{'telethon':sdk})
        self.modules.start(); self.addCleanup(self.modules.stop)
        self.gateway=AccountGateway(self.root); self.addCleanup(self.gateway.close)
        async def immediate(operation): return await operation()
        self.gateway.read=immediate

    def test_membership_preflight_and_plain_text_send_use_persisted_random_id(self):
        peer=self.gateway.resolve_group('@allowed_group')
        self.assertEqual(self.requests,[])
        mid=self.gateway.send_group(peer,'Текст <без форматирования>',123456)
        self.assertEqual(mid,777)
        self.assertEqual(self.requests[0].random_id,123456)
        self.assertEqual(self.requests[0].message,'Текст <без форматирования>')
        self.assertEqual(self.client_options['request_retries'],0)
        self.assertEqual(self.client_options['flood_sleep_threshold'],0)
        self.assertFalse(self.client_options['auto_reconnect'])

    def test_channels_and_personal_accounts_are_not_mailing_groups(self):
        for entity in (self.types.Channel(id=6,megagroup=False),types.SimpleNamespace(id=7)):
            self.entity=entity
            with self.assertRaises(Rejected): self.gateway.resolve_group('@not_group')
        self.assertEqual(self.requests,[])

    def test_left_group_is_rejected_without_joining(self):
        self.entity.left=True
        with self.assertRaises(Rejected): self.gateway.resolve_group('@left_group')
        self.assertEqual(self.requests,[])

    def test_default_group_write_ban_is_rejected(self):
        self.entity.default_banned_rights=types.SimpleNamespace(send_messages=True)
        with self.assertRaises(Rejected): self.gateway.resolve_group('@read_only')

    def test_server_flood_wait_surfaces_without_retry(self):
        peer=self.gateway.resolve_group('@allowed_group')
        error=type('FloodWaitError',(Exception,),{'seconds':300})
        self.failure=error()
        with self.assertRaises(WaitRequired) as result: self.gateway.send_group(peer,'Текст',10)
        self.assertEqual(result.exception.seconds,300)
        self.assertEqual(len(self.requests),1)

    def test_disabled_account_does_not_touch_session_or_connect(self):
        with patch.dict('os.environ',{'MANAGER_ENABLED':'0'}):
            result=self.gateway.status()
        self.assertFalse(result['connected'])
        self.assertIsNone(self.gateway.client)
        self.assertEqual(self.requests,[])

    def test_noninteractive_login_never_requests_secret_input(self):
        with patch('sys.stdin.isatty',return_value=False),patch('getpass.getpass') as prompt,patch('builtins.print'):
            self.assertEqual(login_cli(self.root),1)
            prompt.assert_not_called()
