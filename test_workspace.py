import unittest
from test_core import Base
from smm.workspace_ui import WorkspaceUI

class WorkspaceTests(Base):
    def setUp(self):
        super().setUp()
        self.store.put('owner', 42)
        self.ui=WorkspaceUI(self.store,self.tg,None,None)

    def message(self,user,text):
        self.ui.handle({'message':{'from':{'id':user},'chat':{'id':user,'type':'private'},'text':text}})

    def test_invitation_bound_to_user_and_full_access(self):
        self.store.put('team_invite',{'user':43,'code':'test','expires':self.now+100})
        self.message(44,'/join test')
        self.assertEqual(self.store.get('coowners',[]),[])
        self.message(43,'/join test')
        self.assertEqual(self.store.get('coowners'),[43])
        self.message(43,'/menu')
        self.assertEqual(self.tg.messages[-1][0],43)
        self.assertEqual(self.ui.owner,42)

    def test_expired_invitation_rejected(self):
        self.store.put('team_invite',{'user':43,'code':'test','expires':1})
        self.message(43,'/join test')
        self.assertEqual(self.store.get('coowners',[]),[])

    def test_campaign_is_draft(self):
        self.ui.callback('workspace:new_campaign')
        self.message(42,'Тест | Текст рекламы | @example')
        item=self.store.get('manager_campaigns')[0]
        self.assertEqual(item['state'],'draft')
        self.assertEqual(self.tg.published,[])

    def test_stranger_cannot_create_campaign(self):
        self.message(55,'Тест | Реклама | @example')
        self.assertEqual(self.store.get('manager_campaigns',[]),[])

    def test_coowner_sessions_separate(self):
        self.store.put('coowners',[43])
        self.store.session(42,'workspace',{'action':'add_partner'})
        self.message(43,'/menu')
        self.assertIsNotNone(self.store.session(42))
