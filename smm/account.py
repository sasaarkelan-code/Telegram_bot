"""Optional Telegram user account. Secrets and session stay outside the repo."""
from __future__ import annotations

import asyncio
import getpass
import os
import re
import sys
import time
from pathlib import Path

from .manager import AccountError, Rejected, WaitRequired


def settings(root):
    enabled=os.environ.get('MANAGER_ENABLED','').lower() in ('1','true','yes')
    api_id=os.environ.get('MANAGER_API_ID','').strip()
    api_hash=os.environ.get('MANAGER_API_HASH','').strip()
    if not enabled:
        raise AccountError('Подключение аккаунта выключено. Сначала настрой MANAGER_ENABLED и Telegram API.')
    if not api_id.isascii() or not api_id.isdigit() or not re.fullmatch(r'[a-fA-F0-9]{32}',api_hash):
        raise AccountError('Задай MANAGER_API_ID и MANAGER_API_HASH в ENV хостинга.')
    return int(api_id),api_hash,Path(root)/'manager.session'


class AccountGateway:
    def __init__(self, root):
        self.root=Path(root)
        self.runner=None
        self.client=None
        self.last_read=0

    async def connect(self):
        api_id,api_hash,session=settings(self.root)
        if not session.exists():
            raise AccountError('Сессия аккаунта не создана. Выполни python main.py manager-login в защищённом терминале.')
        if self.client is None:
            try: from telethon import TelegramClient
            except ImportError: raise AccountError('Не установлен Telethon. Пересобери зависимости из requirements.txt.') from None
            self.client=TelegramClient(str(session),api_id,api_hash,request_retries=0,
                connection_retries=0,auto_reconnect=False,flood_sleep_threshold=0,
                receive_updates=False,timeout=10,raise_last_call_error=True)
        if not self.client.is_connected(): await self.client.connect()
        if not await self.client.is_user_authorized(): raise AccountError('Вход аккаунта истёк. Выполни manager-login повторно.')
        return self.client

    def run(self, coro):
        if self.runner is None: self.runner=asyncio.Runner()
        try:
            return self.runner.run(asyncio.wait_for(coro,timeout=45))
        except Exception as error:
            name=type(error).__name__
            if name in ('FloodWaitError','SlowModeWaitError','FloodPremiumWaitError'):
                raise WaitRequired(getattr(error,'seconds',60)) from None
            if name in ('ChatWriteForbiddenError','ChatSendPlainForbiddenError','UserBannedInChannelError',
                        'ChatAdminRequiredError','ChannelPrivateError','UsernameInvalidError','UsernameNotOccupiedError'):
                raise Rejected('Telegram запретил отправку или группа недоступна ('+name+'). Проверь группы.') from None
            if name=='PeerFloodError':
                raise Rejected('Telegram ограничил аккаунт (PeerFlood). Проверь его вручную через @SpamBot. Автоповтора нет.') from None
            if isinstance(error,(AccountError,ValueError)): raise
            raise AccountError('Нет подтверждённого ответа аккаунта Telegram. Проверь подключение и журнал; автоповтора нет.') from None

    async def read(self, operation):
        # All account API reads share one throttle in the single manager thread.
        delay=max(0,self.last_read+2-time.monotonic())
        if delay: await asyncio.sleep(delay)
        try: return await operation()
        finally: self.last_read=time.monotonic()

    async def entity(self,client,ref):
        value=int(ref) if str(ref).startswith('-') else ref
        try: return await self.read(lambda:client.get_entity(value))
        except ValueError:
            if not isinstance(value,int): raise
            # Numeric private group/channel IDs need the account's own access hash.
            await self.read(lambda:client.get_dialogs(limit=200))
            return await self.read(lambda:client.get_entity(value))

    def status(self):
        async def check():
            client=await self.connect()
            me=await client.get_me()
            if me.bot: raise AccountError('Нужен пользовательский аккаунт, не Bot Token.')
            return dict(connected=True,user_id=me.id,name=me.first_name or 'Аккаунт',checked=int(time.time()),note='Вход подтверждён')
        try: return self.run(check())
        except WaitRequired as error:
            return dict(connected=False,note=str(error),checked=int(time.time()),cooldown=int(time.time())+error.seconds)
        except (AccountError,ValueError,ImportError) as error:
            # Do not expose API hashes, phone numbers or raw exceptions in the bot.
            try: settings(self.root)
            except AccountError as error: note=str(error)
            else: note=str(error) if isinstance(error,AccountError) else 'Для входа выполни python main.py manager-login; затем проверь подключение.'
            return dict(connected=False,note=note,checked=int(time.time()))

    def resolve_group(self, ref):
        async def resolve():
            from telethon import types, utils
            client=await self.connect()
            entity=await self.entity(client,ref)
            if not (isinstance(entity,types.Chat) or (isinstance(entity,types.Channel) and entity.megagroup)):
                raise Rejected('В списке есть личный аккаунт или канал. Для рассылки нужны группы.')
            if getattr(entity,'left',False) or getattr(entity,'deactivated',False):
                raise Rejected('Аккаунт не состоит в группе. Вступи вручную и повтори проверку.')
            permissions=await self.read(lambda:client.get_permissions(entity,'me'))
            rights=getattr(entity,'default_banned_rights',None)
            admin=permissions.is_admin or permissions.is_creator
            if permissions.is_banned or (not admin and rights and (rights.send_messages or getattr(rights,'send_plain',False))):
                raise Rejected('В группе аккаунту запрещено отправлять текст.')
            peer=await client.get_input_entity(entity)
            return dict(key=str(utils.get_peer_id(entity)),ref=ref,title=entity.title,
                        kind='channel' if isinstance(peer,types.InputPeerChannel) else 'chat',
                        id=entity.id,access_hash=getattr(peer,'access_hash',0))
        return self.run(resolve())

    def send_group(self, peer, text, random_id):
        async def send():
            from telethon import functions, types
            client=await self.connect()
            target=types.InputPeerChannel(peer['id'],peer['access_hash']) if peer['kind']=='channel' else types.InputPeerChat(peer['id'])
            result=await client(functions.messages.SendMessageRequest(peer=target,message=text,
                          random_id=random_id,no_webpage=True))
            if isinstance(result,types.UpdateShortSentMessage): return result.id
            for update in getattr(result,'updates',[]):
                if isinstance(update,(types.UpdateNewMessage,types.UpdateNewChannelMessage)):
                    return update.message.id
            raise AccountError('Telegram ответил без ID отправленного сообщения. Проверь группу вручную.')
        return self.run(send())

    def find_partners(self, criteria, seen):
        """Public directory and recommendations only; never join or send messages."""
        from telethon import functions, types, utils
        async def candidates():
            client=await self.connect()
            found={}
            if criteria.get('seed'):
                entity=await self.entity(client,criteria['seed'])
                if isinstance(entity,types.Channel) and entity.broadcast:
                    response=await self.read(lambda:client(functions.channels.GetChannelRecommendationsRequest(channel=entity)))
                    for chat in response.chats[:30]: found[chat.id]=(chat,'Рекомендации к твоему каналу')
            for query in criteria['queries'][:3]:
                response=await self.read(lambda:client(functions.contacts.SearchRequest(q=query,limit=30)))
                for chat in response.chats[:30]: found.setdefault(chat.id,(chat,'Поиск: '+query))
            return list(found.values())[:60]
        entries=self.run(candidates())
        # Recommendations come first. Search results are already ranked by Telegram.
        checked=0
        for entity,origin in entries:
            if checked>=20: break
            if str(entity.id) in seen or not isinstance(entity,types.Channel) or not entity.broadcast or not entity.username:
                continue
            if '@'+entity.username.lower()==criteria.get('seed','').lower(): continue
            if str(utils.get_peer_id(entity)) in criteria.get('exclude',[]) or '@'+entity.username.lower() in criteria.get('exclude',[]): continue
            checked+=1
            async def details():
                client=await self.connect()
                full=await self.read(lambda:client(functions.channels.GetFullChannelRequest(entity)))
                posts=await self.read(lambda:client.get_messages(entity,limit=1))
                about=full.full_chat.about or ''
                if criteria.get('kind')=='vp' and not re.search(r'\bвп\b|взаимопиар|взаимн|сотруднич|партн[её]р',about.lower()):
                    return None
                return dict(peer=str(entity.id),title=entity.title,url='https://t.me/'+entity.username,
                            about=about,subscribers=getattr(full.full_chat,'participants_count',None),
                            last_post=int(posts[0].date.timestamp()) if posts else None,origin=origin)
            result=self.run(details())
            if result: yield result

    def close(self):
        if self.runner:
            try:
                async def disconnect():
                    if self.client: await self.client.disconnect()
                self.runner.run(disconnect())
            finally: self.runner.close()


def login_cli(root):
    """Run only in the owner's protected interactive terminal, with the bot stopped."""
    if not sys.stdin.isatty():
        print('Нужен интерактивный терминал с защищённым вводом. Обычная строка команд веб-панели для входа не подходит.')
        return 1
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    from .__main__ import ProcessLock
    api_id,api_hash,path=settings(root)
    async def login():
        client=TelegramClient(str(path),api_id,api_hash,request_retries=0,connection_retries=0,flood_sleep_threshold=0)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                phone=getpass.getpass('Телефон с кодом страны (ввод скрыт): ')
                request=await client.send_code_request(phone)
                code=getpass.getpass('Код Telegram (ввод скрыт): ')
                try: await client.sign_in(phone=phone,code=code,phone_code_hash=request.phone_code_hash)
                except SessionPasswordNeededError: await client.sign_in(password=getpass.getpass('Пароль 2FA (ввод скрыт): '))
            me=await client.get_me()
            if me.bot: raise ValueError('Нужен пользовательский аккаунт.')
            os.chmod(path,0o600)
            print('Аккаунт подключён. Сессия сохранена; теперь запусти бота и проверь подключение в меню.')
        finally: await client.disconnect()
    Path(root).mkdir(parents=True,exist_ok=True,mode=0o700)
    previous_umask=os.umask(0o077)
    try:
        with ProcessLock(Path(root)/'bot.lock'):
            asyncio.run(login())
    except Exception as error:
        print('Вход не завершён ('+type(error).__name__+'). Проверь настройки в защищённом терминале. Секреты не публикуй.')
        return 1
    finally:
        os.umask(previous_umask)
    return 0
