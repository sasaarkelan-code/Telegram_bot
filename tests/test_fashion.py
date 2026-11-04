import json
import itertools
from unittest.mock import patch

from test_core import Base, FakeAuth, events
from smm.ai import AI, AIError
from smm.budget import Budget, BudgetStop
from smm.editor import REQUIRED, fashion_options, product_key, verify_product
from smm.engine import Engine
from smm.fashion import Fashion
from smm.store import Store


class ProductAI:
    # Complete grounded outfits, with optional useful partials and a spare look.
    def __init__(self, missing=(), ungrounded=False, spare=False):
        self.calls=[]; self.missing=set(missing)
        self.ungrounded=ungrounded; self.spare=spare

    def call(self,cid,sid,instructions,context,web=False):
        self.calls.append((web,context))
        sources=set()
        def product(category,offset=0):
            article=100000+sorted(REQUIRED).index(category)+len(self.calls)*100+offset
            url=f'https://www.wildberries.ru/catalog/{article}/detail.aspx'
            sources.add(url)
            return dict(name=category,category=category,price=1000,winter=True,url=url)
        if 'replace_categories' in context:
            items=[product(cat) for cat in context['replace_categories'] if cat not in self.missing]
            return {'items':items},set() if self.ungrounded else sources
        if not web:
            choices=[[x['id'] for x in context['catalog'] if x['category']==cat] for cat in sorted(REQUIRED)]
            ids=next((list(combo) for combo in itertools.product(*choices)
                      if sorted(combo) not in [sorted(x) for x in context.get('recent',[])]),[])
            return {'options':[dict(title='Зимний образ',description='Полный комплект',item_ids=ids)]},set()
        options=[dict(title='Зимний образ',description='Полный комплект',items=[product(cat) for cat in sorted(REQUIRED) if cat not in self.missing])]
        if self.spare:
            options.append(dict(title='Другой зимний образ',items=[product(cat,10000) for cat in sorted(REQUIRED)]))
        return {'options':options},set() if self.ungrounded else sources


class FashionTests(Base):
    def fashion_slot(self,offset=0):
        self.channel(5)
        sid=self.store.execute("INSERT INTO slots(channel_id,due,label,state) VALUES(?,?,?,'preparing')",(5,self.now+2400+offset,'15:00'))
        return self.store.channel(5),self.store.slot(sid)

    @staticmethod
    def verified(p,now): return dict(p,verified=True,checked_at=now,available=True)

    def preparer(self,ai=None,verifier=None):
        return Fashion(self.store,ai or ProductAI(),verifier=verifier or self.verified,clock=lambda:self.now)

    def test_complete_outfit_is_one_request_not_six_searches_plus_composition(self):
        ch,slot=self.fashion_slot(); ai=ProductAI()
        opts=self.preparer(ai).prepare(ch,slot)
        self.assertEqual(len(ai.calls),1)
        self.assertTrue(ai.calls[0][0])
        self.assertNotIn('category',ai.calls[0][1])
        self.assertEqual(opts[0]['meta']['total'],6000)
        self.assertFalse(opts[0]['meta']['unverified'])
        self.assertFalse(opts[0]['media'])

    def test_ready_cache_does_not_call_ai_again(self):
        ch,slot=self.fashion_slot(); self.preparer().prepare(ch,slot)
        ch,slot=self.fashion_slot(60); ai=ProductAI()
        self.preparer(ai).prepare(ch,slot)
        self.assertEqual(ai.calls,[])

    def test_only_the_recommended_look_is_checked_and_spare_survives_for_next_slot(self):
        ch,slot=self.fashion_slot(); checked=[]
        def verifier(p,n): checked.append(p['url']); return self.verified(p,n)
        opts=self.preparer(ProductAI(spare=True),verifier).prepare(ch,slot)
        self.assertEqual(len(opts),1)
        self.assertEqual(len(checked),6)
        self.store.add_candidates(slot['id'],opts,self.now)
        ch,slot=self.fashion_slot(60); ai=ProductAI()
        second=self.preparer(ai,verifier).prepare(ch,slot)
        self.assertEqual(ai.calls,[])
        self.assertNotEqual(opts[0]['event_key'],second[0]['event_key'])
        self.assertEqual(len(checked),12)

    def test_missing_piece_gets_one_targeted_repair(self):
        ch,slot=self.fashion_slot(); ai=ProductAI()
        original=ai.call
        def first_partial(*args,**kw):
            ai.missing={'bag'} if not ai.calls else set()
            return original(*args,**kw)
        ai.call=first_partial
        opts=self.preparer(ai).prepare(ch,slot)
        self.assertEqual(len(ai.calls),2)
        self.assertEqual(ai.calls[1][1]['replace_categories'],['bag'])
        self.assertEqual(len(ai.calls[1][1]['kept']),5)
        self.assertEqual(opts[0]['meta']['total'],6000)

    def test_failed_repair_saves_partial_and_next_slot_only_repairs_it(self):
        ch,slot=self.fashion_slot(); ai=ProductAI(missing={'bag'})
        with self.assertRaises(AIError): self.preparer(ai).prepare(ch,slot)
        self.assertEqual(len(ai.calls),2)
        self.assertEqual(len(self.store.get('fashion_cache:5')['items']),5)
        ch,slot=self.fashion_slot(60); ai=ProductAI()
        opts=self.preparer(ai).prepare(ch,slot)
        self.assertEqual(len(ai.calls),1)
        self.assertEqual(ai.calls[0][1]['replace_categories'],['bag'])
        self.assertEqual(len(opts),1)

    def test_manual_retry_does_not_repeat_failed_repair(self):
        ch,slot=self.fashion_slot(); ai=ProductAI(missing={'bag'})
        with self.assertRaises(AIError): self.preparer(ai).prepare(ch,slot)
        with self.assertRaises(AIError): self.preparer(ai).prepare(ch,slot)
        self.assertEqual(len(ai.calls),2)
        self.assertEqual(self.store.get('fashion_steps:'+str(slot['id'])),2)

    def test_legacy_exhausted_step_counter_is_not_reset(self):
        ch,slot=self.fashion_slot(); ai=ProductAI()
        self.store.put('fashion_steps:'+str(slot['id']),7)
        with self.assertRaises(AIError): self.preparer(ai).prepare(ch,slot)
        self.assertEqual(ai.calls,[])
        self.assertEqual(self.store.get('fashion_steps:'+str(slot['id'])),7)

    def test_one_request_setting_disables_repair(self):
        ch,slot=self.fashion_slot(); ai=ProductAI(missing={'bag'})
        limits=self.store.get('limits');limits['fashion_steps']=1;self.store.put('limits',limits)
        with self.assertRaises(AIError): self.preparer(ai).prepare(ch,slot)
        self.assertEqual(len(ai.calls),1)

    def test_ungrounded_items_are_rejected_without_wasteful_category_retries(self):
        ch,slot=self.fashion_slot(); ai=ProductAI(ungrounded=True)
        with self.assertRaises(AIError): self.preparer(ai).prepare(ch,slot)
        self.assertEqual(len(ai.calls),1)
        self.assertEqual(self.store.get('fashion_cache:5')['items'],[])

    def test_marketplace_identity_accepts_www_source_variant(self):
        ch,slot=self.fashion_slot(); ai=ProductAI(); original=ai.call
        def provider(*a,**kw):
            obj,urls=original(*a,**kw)
            return obj,{u.replace('www.wildberries.ru','wildberries.ru') for u in urls}
        ai.call=provider
        self.assertEqual(len(self.preparer(ai).prepare(ch,slot)),1)
        self.assertEqual(product_key('https://www.ozon.ru/product/coat-1234/?utm_source=x'),'ozon:1234')

    def test_pause_and_deadline_do_not_call_ai(self):
        ch,slot=self.fashion_slot(); ai=ProductAI()
        self.store.put('paused',True)
        with self.assertRaises(AIError): self.preparer(ai).prepare(ch,slot)
        self.store.put('paused',False)
        with self.assertRaises(AIError): Fashion(self.store,ai,clock=lambda:slot['due']-170).prepare(ch,slot)
        self.assertEqual(ai.calls,[])

    def test_blocked_cards_are_unverified_and_not_researched_by_ai(self):
        ch,slot=self.fashion_slot(); ai=ProductAI(); checks=[]
        def blocked(p,n): checks.append(p['url']);return dict(p,verified=False,checked_at=0)
        opts=self.preparer(ai,blocked).prepare(ch,slot)
        self.assertTrue(opts[0]['meta']['unverified'])
        self.assertEqual(len(ai.calls),1)
        ch,slot=self.fashion_slot(60)
        self.preparer(ai,blocked).prepare(ch,slot)
        self.assertEqual(len(checks),6)
        opts[0]['media']='file-id'; ids=self.store.add_candidates(slot['id'],opts,self.now)
        with self.assertRaises(ValueError): self.store.approve(ids[0],self.now)

    def test_out_of_stock_only_repairs_the_bad_category(self):
        ch,slot=self.fashion_slot(); ai=ProductAI()
        # Product generation uses the sorted category index.
        bad=100100+sorted(REQUIRED).index('shoes')
        def checker(p,n):
            return dict(p,verified=False,checked_at=0,available=False) if f'/{bad}/' in p['url'] else self.verified(p,n)
        opts=self.preparer(ai,checker).prepare(ch,slot)
        self.assertEqual(len(ai.calls),2)
        self.assertEqual(ai.calls[1][1]['replace_categories'],['shoes'])
        self.assertFalse(opts[0]['meta']['unverified'])

    def test_prices_changed_after_check_trigger_only_budget_repair(self):
        ch,slot=self.fashion_slot(); ai=ProductAI()
        bad=100100+sorted(REQUIRED).index('outerwear')
        def checker(p,n):
            return dict(p,price=8000,verified=True,checked_at=n,available=True) if f'/{bad}/' in p['url'] else self.verified(p,n)
        opts=self.preparer(ai,checker).prepare(ch,slot)
        self.assertEqual(len(ai.calls),2)
        self.assertIn('outerwear',ai.calls[1][1]['replace_categories'])
        self.assertLessEqual(opts[0]['meta']['total'],10000)

    def test_cache_expires_at_twenty_four_hours(self):
        ch,slot=self.fashion_slot();self.preparer().prepare(ch,slot)
        saved=self.store.get('fashion_cache:5')
        for p in saved['items']: p['found_at']=self.now-86401
        for b in saved['outfits']: b['found_at']=self.now-86401
        self.store.put('fashion_cache:5',saved)
        ch,slot=self.fashion_slot(60);ai=ProductAI();self.preparer(ai).prepare(ch,slot)
        self.assertEqual(len(ai.calls),1)
        self.assertTrue(ai.calls[0][0])

    def test_changed_topic_does_not_reuse_old_style(self):
        ch,slot=self.fashion_slot();self.preparer().prepare(ch,slot)
        ch,slot=self.fashion_slot(60);ch['config']['topic']='Другой стиль и другая аудитория'
        ai=ProductAI();self.preparer(ai).prepare(ch,slot)
        self.assertEqual(len(ai.calls),1)

    def test_used_single_outfit_cache_starts_new_outfit_search(self):
        ch,slot=self.fashion_slot();opts=self.preparer().prepare(ch,slot)
        self.store.add_candidates(slot['id'],opts,self.now)
        ch,slot=self.fashion_slot(60);ai=ProductAI()
        # Distinguish a newly found outfit from the already proposed product IDs.
        original=ai.call
        def provider(*a,**kw):
            result,urls=original(*a,**kw)
            for o in result.get('options',[]):
                for p in o.get('items',[]): p['url']=p['url'].replace('/100','/900')
            return result,{u.replace('/100','/900') for u in urls}
        ai.call=provider
        self.preparer(ai).prepare(ch,slot)
        self.assertEqual(len(ai.calls),1)
        self.assertTrue(ai.calls[0][0])

    def test_malformed_options_stop_after_one_call(self):
        ch,slot=self.fashion_slot();ai=ProductAI()
        ai.call=lambda *a,**kw:({'options':None},set())
        with self.assertRaisesRegex(AIError,'формат'): self.preparer(ai).prepare(ch,slot)
        self.assertEqual(self.store.get('fashion_steps:'+str(slot['id'])),1)

    def real_provider(self,fake,usage=None):
        payloads=[]
        def opener(req,timeout):
            payload=json.loads(req.data);payloads.append(payload)
            obj,urls=fake.call(5,1,'',json.loads(payload['input'][0]['content']),web='tools' in payload)
            output=[{'type':'web_search_call','id':'search','action':{'sources':[{'url':u} for u in urls]}}] if urls else []
            return events({'type':'response.output_text.delta','delta':json.dumps(obj)},
                          {'type':'response.completed','response':{'output':output,'usage':usage or {'input_tokens':100,'output_tokens':50}}})
        return AI(self.store,FakeAuth(),opener),payloads

    def test_accounting_and_low_reasoning_preserve_selected_model(self):
        ch,slot=self.fashion_slot();self.store.put('model','gpt-5.6-sol')
        ai,payloads=self.real_provider(ProductAI())
        self.preparer(ai).prepare(ch,slot)
        self.assertEqual(len(payloads),1)
        self.assertEqual(payloads[0]['model'],'gpt-5.6-sol')
        self.assertEqual(payloads[0]['reasoning'],{'effort':'low'})
        self.assertNotIn('max_output_tokens',payloads[0])
        self.assertEqual(Budget(self.store).usage()['tokens'],150)
        self.assertEqual(self.store.get('fashion_last:5')['requests'],1)

    def test_completed_outfit_survives_known_token_halt_as_pending_proposal(self):
        ch,slot=self.fashion_slot();ai,payloads=self.real_provider(ProductAI(),dict(input_tokens=1000,output_tokens=12000))
        limits=self.store.get('limits');limits['daily_tokens']=13000;self.store.put('limits',limits)
        self.store.execute("UPDATE slots SET state='new' WHERE id=?",(slot['id'],))
        with patch('smm.fashion.verify_product') as network:
            Engine(self.store,self.tg,ai).prepare(slot['id'],self.now)
            network.assert_not_called()
        self.assertTrue(self.store.get('paused'))
        self.assertEqual(self.store.slot(slot['id'])['state'],'pending')
        self.assertEqual(len(self.store.rows('SELECT * FROM candidates WHERE slot_id=?',(slot['id'],))),1)
        self.assertEqual(len(payloads),1)
        self.assertEqual(Budget(self.store).usage()['tokens'],13000)
        Engine(self.store,self.tg,ai).publish(slot['due'])
        self.assertEqual(self.tg.published,[])

    def test_known_fashion_reserve_overrun_closes_only_that_slot(self):
        ch,slot=self.fashion_slot();ai,payloads=self.real_provider(ProductAI(),dict(input_tokens=1000,output_tokens=12000))
        opts=self.preparer(ai).prepare(ch,slot)
        self.assertEqual(len(opts),1)
        self.assertFalse(self.store.get('paused'))
        self.assertTrue(self.store.get('fashion_spend_closed:'+str(slot['id'])))
        with self.assertRaises(BudgetStop): Budget(self.store).reserve(5,slot['id'],100,self.now)
        self.assertIsInstance(Budget(self.store).reserve(1,999,100,self.now),int)
        self.assertEqual(len(payloads),1)

    def test_clothing_daily_limit_does_not_halt_other_channels(self):
        ch,slot=self.fashion_slot();limits=self.store.get('limits');limits['channel_daily_tokens']=12000;self.store.put('limits',limits)
        b=Budget(self.store);rid=b.reserve(5,slot['id'],100,self.now)
        b.finish(rid,dict(input_tokens=100,output_tokens=50),now=self.now)
        with self.assertRaisesRegex(BudgetStop,'канала одежды'): b.reserve(5,slot['id']+1,100,self.now)
        self.assertFalse(self.store.get('paused'))
        self.assertIsInstance(b.reserve(1,888,100,self.now),int)

    def test_slot_budget_blocks_extra_call_without_halting_other_channels(self):
        ch,slot=self.fashion_slot();b=Budget(self.store)
        limits=self.store.get('limits');limits['fashion_slot_tokens']=12000;self.store.put('limits',limits)
        rid=b.reserve(5,slot['id'],100,self.now);b.finish(rid,dict(input_tokens=100,output_tokens=50),now=self.now)
        with self.assertRaisesRegex(BudgetStop,'одной подборки'): b.reserve(5,slot['id'],100,self.now)
        self.assertFalse(self.store.get('paused'))
        self.assertIsInstance(b.reserve(1,999,100,self.now),int)

    def test_slot_request_limit_is_atomic_and_not_reset_by_restart(self):
        ch,slot=self.fashion_slot();b=Budget(self.store)
        for _ in range(2):
            rid=b.reserve(5,slot['id'],100,self.now);b.finish(rid,dict(input_tokens=10,output_tokens=10),now=self.now)
        with self.assertRaises(BudgetStop): Budget(Store(self.store.path)).reserve(5,slot['id'],100,self.now)
        self.assertFalse(self.store.get('paused'))

    def test_existing_product_only_cache_needs_one_selection_call_without_web(self):
        ch,slot=self.fashion_slot();self.preparer().prepare(ch,slot)
        saved=self.store.get('fashion_cache:5');saved.pop('outfits');self.store.put('fashion_cache:5',saved)
        ch,slot=self.fashion_slot(60);ai=ProductAI();self.preparer(ai).prepare(ch,slot)
        self.assertEqual(len(ai.calls),1)
        self.assertFalse(ai.calls[0][0])

    def test_invalid_usage_is_not_treated_as_a_known_paid_result(self):
        ch,slot=self.fashion_slot();ai,_=self.real_provider(ProductAI(),dict(input_tokens=-1,output_tokens=100))
        with self.assertRaises(BudgetStop): self.preparer(ai).prepare(ch,slot)
        self.assertTrue(self.store.get('paused'))
        self.assertEqual(Budget(self.store).usage()['uncertain'],1)


class FashionMigrationTests(Base):
    def test_optimization_caps_only_fashion_settings_once_and_preserves_old_spend(self):
        limits=self.store.get('limits')
        limits.update(daily_tokens=88000,fashion_steps=10,fashion_web_calls=4,fashion_seconds=150,fashion_output_bytes=24000)
        limits.pop('fashion_slot_tokens',None)
        self.store.put('limits',limits)
        self.store.put('fashion_optimized_v2',False)
        self.store.put('model','gpt-5.6-sol')
        self.store.put('fashion_steps:99',7)
        rid=Budget(self.store).reserve(1,99,100,self.now)
        Budget(self.store).finish(rid,dict(input_tokens=100,output_tokens=40),now=self.now)
        upgraded=Store(self.store.path)
        self.assertEqual(upgraded.get('limits')['fashion_steps'],2)
        self.assertEqual(upgraded.get('limits')['fashion_slot_tokens'],24000)
        self.assertEqual(upgraded.get('limits')['daily_tokens'],88000)
        self.assertEqual(upgraded.get('fashion_steps:99'),7)
        self.assertEqual(upgraded.get('model'),'gpt-5.6-sol')
        self.assertEqual(Budget(upgraded).usage(self.now)['tokens'],140)
        limits=upgraded.get('limits');limits['fashion_steps']=1;upgraded.put('limits',limits)
        self.assertEqual(Store(self.store.path).get('limits')['fashion_steps'],1)

    def test_existing_database_upgrades_once_without_resetting_user_data(self):
        limits = self.store.get('limits')
        limits.update(daily_tokens=50000,rolling_tokens=30000,channel_daily_tokens=25000,daily_requests=20,hourly_requests=8)
        self.store.put('limits', limits)
        self.store.put('fashion_upgrade', 0)
        self.store.put('model', 'gpt-5.6-sol')
        self.store.put('owner', 42)
        self.store.put('coowners', [43])
        ch = self.store.channel(5)
        ch['config']['web_search'] = False
        ch['config']['budgets']['15:00'] = 11000
        self.store.update_channel(5, config=ch['config'])
        rid = Budget(self.store).reserve(1,1,100,self.now)
        Budget(self.store).finish(rid, {'input_tokens':100,'output_tokens':30}, now=self.now)
        self.store.halt('Ручная пауза')
        upgraded = Store(self.store.path)
        self.assertEqual(upgraded.get('model'), 'gpt-5.6-sol')
        self.assertEqual(upgraded.get('owner'), 42)
        self.assertEqual(upgraded.get('coowners'), [43])
        self.assertEqual(upgraded.get('limits')['daily_tokens'], 300000)
        self.assertEqual(upgraded.channel(5)['config']['budgets']['15:00'], 11000)
        self.assertTrue(upgraded.channel(5)['config']['web_search'])
        self.assertTrue(upgraded.get('paused'))
        self.assertEqual(Budget(upgraded).usage(self.now)['tokens'], 130)
        limits = upgraded.get('limits')
        limits['daily_tokens'] = 150000
        upgraded.put('limits', limits)
        ch = upgraded.channel(5)
        ch['config']['web_search'] = False
        upgraded.update_channel(5, config=ch['config'])
        again = Store(self.store.path)
        self.assertEqual(again.get('limits')['daily_tokens'], 150000)
        self.assertFalse(again.channel(5)['config']['web_search'])

    def test_higher_existing_limits_are_not_reduced(self):
        limits = self.store.get('limits')
        limits['daily_tokens'] = 900000
        self.store.put('limits', limits)
        self.store.put('fashion_upgrade', 0)
        self.assertEqual(Store(self.store.path).get('limits')['daily_tokens'], 900000)


class PublicPriceTests(Base):
    def product(self):
        return dict(name='Куртка', category='outerwear', price=1000, winter=True,
                    url='https://www.wildberries.ru/catalog/100001/detail.aspx')

    def check(self, offer, **extra):
        obj = dict({'@type':'Product','url':self.product()['url'],'offers':offer}, **extra)
        raw = ('<script type="application/ld+json">'+json.dumps(obj)+'</script>').encode()
        with patch('smm.editor.fetch', return_value=(raw,{},self.product()['url'])):
            return verify_product(self.product(), self.now)

    def test_available_ruble_offer_updates_actual_price(self):
        p = self.check(dict(price=1299,priceCurrency='RUB',availability='https://schema.org/InStock'))
        self.assertTrue(p['verified'])
        self.assertEqual(p['price'],1299)

    def test_missing_or_out_of_stock_is_not_confirmed(self):
        for availability in ('', 'https://schema.org/OutOfStock'):
            p = self.check(dict(price=1299,priceCurrency='RUB',availability=availability))
            self.assertFalse(p['verified'])

    def test_recommended_other_product_does_not_verify_this_card(self):
        p = self.check(dict(price=1299,priceCurrency='RUB',availability='https://schema.org/InStock'),
                       url='https://www.wildberries.ru/catalog/999999/detail.aspx')
        self.assertFalse(p['verified'])
