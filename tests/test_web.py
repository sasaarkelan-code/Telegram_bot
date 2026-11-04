import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from PIL import Image
from smm.store import Store
from smm.engine import Engine
from smm.webapp import create_app, issue_code


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.s=Store(self.root/'smm.sqlite');self.s.put('owner',12345);self.s.put('model','gpt-5.6-sol')
        self.auth=Mock();self.auth.saved.return_value=True;self.tg=Mock();self.tg.connect_channel.return_value='-100123456'
        self.engine=Engine(self.s,self.tg,Mock());self.app=create_app(self.root,self.s,self.engine,self.auth,self.tg,env={'SMM_WEB_COOKIE_SECURE':'0'})
        self.c=self.app.test_client();self.csrf=''
    def tearDown(self):self.tmp.cleanup()
    def login(self,actor=12345):
        code=issue_code(self.s,actor);r=self.c.post('/api/login',json={'code':code,'username':str(actor)},headers={'X-SMM-Web':'1'})
        self.assertEqual(r.status_code,200,r.json);self.csrf=self.c.get('/api/state').json['csrf'];return code
    def post(self,path,d):return self.c.post(path,json=d,headers={'X-SMM-Web':'1','X-CSRF-Token':self.csrf})
    def candidates(self):
        ch=self.s.channel(1);cfg=ch['config'];cfg['require_photo']=False
        self.s.update_channel(1,config=cfg,enabled=1,target='-100123456');sid=self.s.enqueue_search(1)
        self.s.execute("UPDATE slots SET state='preparing' WHERE id=?",(sid,))
        return self.s.add_candidates(sid,[dict(title='Пост',body='Текст проверенного поста'),dict(title='Второй',body='Другой проверенный пост')],time.time())
    def test_public_shell_private_data(self):
        with self.c.get('/') as r:self.assertEqual(r.status_code,200)
        self.assertEqual(self.c.get('/healthz').json['version'],'2.3.0')
        self.assertEqual(self.c.get('/api/state').status_code,401)
        with self.c.get('/assets/app.js') as r:self.assertEqual(r.status_code,200)
    def test_code_single_use_and_cookie(self):
        code=issue_code(self.s,12345);r=self.c.post('/api/login',json={'code':code,'username':'12345'},headers={'X-SMM-Web':'1'})
        self.assertIn('HttpOnly',r.headers['Set-Cookie']);self.assertIn('SameSite=Strict',r.headers['Set-Cookie'])
        self.assertEqual(self.app.test_client().post('/api/login',json={'code':code,'username':'12345'},headers={'X-SMM-Web':'1'}).status_code,401)
    def test_expired_and_unauthorized_codes(self):
        with self.assertRaises(ValueError):issue_code(self.s,999)
        code=issue_code(self.s,12345);self.s.execute('UPDATE web_codes SET expires=0')
        self.assertEqual(self.c.post('/api/login',json={'code':code,'username':'12345'},headers={'X-SMM-Web':'1'}).status_code,401)
    def test_csrf_and_origin(self):
        self.login();self.assertEqual(self.c.post('/api/control',json={'action':'pause'}).status_code,403)
        self.assertEqual(self.c.post('/api/control',json={'action':'pause'},headers={'X-SMM-Web':'1','X-CSRF-Token':self.csrf,'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.post('/api/control',{'action':'pause'}).status_code,200)
    def test_rate_limit(self):
        for _ in range(20):self.c.post('/api/login',json={'code':'bad','username':'12345'},headers={'X-SMM-Web':'1'})
        self.assertEqual(self.c.post('/api/login',json={'code':'bad','username':'12345'},headers={'X-SMM-Web':'1'}).status_code,429)
    def test_two_queued_edit_clears_only_one(self):
        self.login();a,b=self.candidates()
        for pid,minute in ((a,30),(b,60)):
            r=self.post(f'/api/posts/{pid}',{'action':'schedule','time':f'через {minute} мин'});self.assertEqual(r.status_code,200,r.json)
        self.assertEqual(self.post(f'/api/posts/{a}',{'action':'edit','body':'Новый текст'}).status_code,200)
        self.assertIsNone(self.s.candidate(a)['scheduled_at']);self.assertEqual(self.s.candidate(b)['state'],'approved')
        self.assertEqual(self.s.get('model'),'gpt-5.6-sol')
    def test_photo_required(self):
        self.login();a,_=self.candidates();ch=self.s.channel(1);cfg=ch['config'];cfg['require_photo']=True;self.s.update_channel(1,config=cfg)
        self.assertEqual(self.post(f'/api/posts/{a}',{'action':'schedule','time':'через 30 мин'}).status_code,400)
    def test_upload_validates_and_persists(self):
        self.login();a,_=self.candidates();raw=io.BytesIO();Image.new('RGB',(80,60),'purple').save(raw,'PNG');raw.seek(0)
        r=self.c.post(f'/api/posts/{a}/image',data={'image':(raw,'../bad.png')},headers={'X-SMM-Web':'1','X-CSRF-Token':self.csrf});self.assertEqual(r.status_code,200,r.json)
        media=self.s.candidate(a)['media'];self.assertRegex(media,r'^local:[a-f0-9]{32}\.jpg$');self.assertTrue((self.root/'media'/media[6:]).exists())
        with self.c.get(f'/api/posts/{a}/image') as r:self.assertEqual(r.status_code,200)
        r=self.c.post(f'/api/posts/{a}/image',data={'image':(io.BytesIO(b'<script>bad</script>'),'bad.jpg')},headers={'X-SMM-Web':'1','X-CSRF-Token':self.csrf});self.assertEqual(r.status_code,400)
    def test_credentials_not_exposed_or_network_called(self):
        self.login();self.s.put('telegram_token','NEVER_SHOW');self.s.put('access_token','PRIVATE')
        r=self.c.get('/api/state');self.assertNotIn('NEVER_SHOW',r.text);self.assertNotIn('PRIVATE',r.text);self.auth.models.assert_not_called();self.tg.send.assert_not_called()
    def test_coowner_revocation(self):
        self.s.put('coowners',[777]);self.login(777)
        self.assertEqual(self.post('/api/team',{'action':'remove','id':777}).status_code,200)
        self.assertEqual(self.c.get('/api/state').status_code,401)
    def test_invite_requires_acceptance(self):
        self.login();r=self.post('/api/team',{'action':'invite','id':777});self.assertEqual(r.status_code,200)
        self.assertTrue(r.json['command'].startswith('/join '));self.assertNotIn(777,self.s.get('coowners',[]))
        with self.assertRaises(ValueError):issue_code(self.s,777)
    def test_campaign_draft_and_account_guard(self):
        self.login();r=self.post('/api/campaigns',{'name':'Реклама','text':'Пост','targets':'@group_one\n@group_two'});self.assertEqual(r.status_code,200,r.json)
        cid=r.json['id'];self.assertEqual(self.post('/api/campaigns/'+cid,{'action':'start'}).status_code,400)
        self.assertEqual(self.post('/api/campaigns/'+cid,{'action':'edit','field':'text','value':'Новый пост'}).status_code,200)
        self.assertEqual(self.s.get('manager_campaigns')[0]['text'],'Новый пост');self.tg.send.assert_not_called()
    def test_connect_and_filter_validation(self):
        self.login();self.assertEqual(self.post('/api/channels/1',{'enabled':True}).status_code,400)
        self.assertEqual(self.post('/api/channels/1',{'action':'connect','target':'@my_channel'}).status_code,200)
        self.assertEqual(self.post('/api/channels/1',{'enabled':True,'config':{'search_times':'09:00 15:00 21:00'}}).status_code,200)
        self.assertEqual(self.post('/api/channels/1',{'config':{'sources':['http://127.0.0.1/secret']}}).status_code,400)
        self.assertEqual(self.post('/api/channels/1',{'config':{'search_times':'09:00'}}).status_code,400)
    def test_limits_bounds(self):
        self.login();self.assertEqual(self.post('/api/limits',{'daily_tokens':400000}).status_code,200)
        self.assertEqual(self.post('/api/limits',{'fashion_steps':99}).status_code,400)
        self.assertEqual(self.s.get('limits')['daily_tokens'],400000)

    def test_notices_private_and_read_cursor_is_per_actor(self):
        self.s.notice('test-event','Saved for the web only')
        self.assertEqual(self.c.get('/api/state').status_code,401)
        self.login()
        state=self.c.get('/api/state').json
        latest=state['notices'][0]['id']
        self.assertGreater(state['notice_unread'],0)
        self.assertEqual(self.post('/api/notices/read',{'until':latest}).status_code,200)
        self.assertEqual(self.c.get('/api/state').json['notice_unread'],0)
        self.s.put('coowners',[12346])
        self.login(12346)
        self.assertGreater(self.c.get('/api/state').json['notice_unread'],0)

    def test_read_notice_does_not_hide_future_events(self):
        self.login()
        self.assertEqual(self.post('/api/notices/read',{'until':999999}).status_code,200)
        self.s.notice('after-read','A later event')
        self.assertEqual(self.c.get('/api/state').json['notice_unread'],1)
        self.assertEqual(self.post('/api/notices/read',{'until':-1}).status_code,400)
        self.assertEqual(self.c.post('/api/notices/read',json={'until':1}).status_code,403)

    def test_source_presets_and_health_are_private_state(self):
        self.login()
        state=self.c.get('/api/state').json
        self.assertGreater(len(state['source_presets']['ai']),2)
        self.assertGreater(len(state['source_presets']['cinema']),1)
        self.assertTrue(state['fashion_unmetered'])
        self.assertIsInstance(state['source_health'],list)

    def test_warning_mode_can_be_changed_without_resetting_limits(self):
        self.login()
        original=self.s.get('limits')
        self.assertEqual(self.post('/api/limits',{'budget_enforced':False}).status_code,200)
        self.assertFalse(self.s.get('budget_enforced'))
        self.assertEqual(self.s.get('limits'),original)
        self.assertEqual(self.post('/api/limits',{'budget_enforced':'false'}).status_code,400)
    def test_search_and_stop(self):
        self.login();self.candidates();self.assertEqual(self.post('/api/channels/1',{'action':'search'}).status_code,400)
        self.assertEqual(self.post('/api/control',{'action':'resume'}).status_code,200)
        self.assertEqual(self.post('/api/channels/1',{'action':'search'}).status_code,200)
        self.assertEqual(self.post('/api/channels/1',{'action':'stop_search'}).status_code,200)
        self.assertFalse(self.s.rows("SELECT id FROM slots WHERE state IN ('new','preparing')"))
    def test_headers_and_logout(self):
        with self.c.get('/') as r:
            self.assertEqual(r.headers['X-Frame-Options'],'DENY');self.assertIn("script-src 'self'",r.headers['Content-Security-Policy']);self.assertEqual(r.headers['Cache-Control'],'no-store')
        self.login();self.assertEqual(self.post('/api/logout',{}).status_code,200);self.assertEqual(self.c.get('/api/state').status_code,401)
    def test_telegram_web_command_is_private(self):
        from smm.navigation import PanelUI
        ui=PanelUI(self.s,self.tg,self.auth,self.engine)
        ui.handle({'message':{'chat':{'type':'private','id':99},'from':{'id':99},'text':'/web'}})
        self.assertFalse(self.s.rows('SELECT * FROM web_codes'))
        ui.handle({'message':{'chat':{'type':'private','id':12345},'from':{'id':12345},'text':'/web'}})
        self.assertEqual(len(self.s.rows('SELECT * FROM web_codes')),1);self.assertIn('Вход в веб-панель',self.tg.send.call_args.args[1])

    def test_username_is_bound_to_telegram_identity(self):
        code=issue_code(self.s,12345,'Actual_Name')
        self.assertEqual(self.c.post('/api/login',json={'username':'wrong_name','code':code},headers={'X-SMM-Web':'1'}).status_code,401)
        self.assertEqual(self.c.post('/api/login',json={'username':'@ACTUAL_NAME','code':code},headers={'X-SMM-Web':'1'}).status_code,200)
        self.assertEqual(self.c.get('/api/state').json['actor'],12345)

    def test_pwa_public_files_and_no_private_cache(self):
        with self.c.get('/manifest.webmanifest') as r:
            self.assertEqual(r.status_code,200);self.assertEqual(r.json['display'],'standalone')
        with self.c.get('/sw.js') as r:
            self.assertEqual(r.status_code,200);self.assertIn('/api/',r.text);self.assertIn('network',r.text)

    def test_backup_preserves_wal_database_and_private_files(self):
        from smm.backup import backup_data
        self.s.put('model','gpt-5.6-sol');(self.root/'credentials.json').write_text('{"test":"fake"}')
        dest=Path(self.tmp.name).parent/('backup-test-'+self.root.name)
        try:
            copy=backup_data(self.root,dest)
            other=Store(copy/'smm.sqlite');self.assertEqual(other.get('model'),'gpt-5.6-sol')
            self.assertEqual((copy/'credentials.json').read_text(),'{"test":"fake"}')
            self.assertEqual((copy/'credentials.json').stat().st_mode&0o777,0o600)
            with self.assertRaises(ValueError):backup_data(self.root,self.root/'nested')
        finally:__import__('shutil').rmtree(dest,ignore_errors=True)

    def test_preview_uses_exact_rendering_and_escapes_untrusted_text(self):
        self.login();pid,_=self.candidates()
        self.s.edit_candidate(pid,body='<img src=x onerror=alert(1)>')
        p=next(p for p in self.c.get('/api/state').json['posts'] if p['id']==pid)
        self.assertIn('&lt;img',p['preview_html']);self.assertNotIn('<img',p['preview_html'])
        self.assertIn('<b>Пост</b>',p['preview_html'])

    def test_older_unknown_publications_are_never_hidden_by_recent_limit(self):
        self.login();pid,_=self.candidates()
        old=self.s.candidate(pid);self.s.execute("UPDATE candidates SET state='unknown',created=0 WHERE id=?",(pid,))
        with self.s.tx() as c:
            for i in range(310):
                c.execute("INSERT INTO candidates(slot_id,title,body,meta,state,created) VALUES(?,?,?,?,?,?)",(old['slot_id'],'Recent '+str(i),'Текст','{}','draft',int(time.time())))
        ids={p['id'] for p in self.c.get('/api/state').json['posts']};self.assertIn(pid,ids)
