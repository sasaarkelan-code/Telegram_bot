import json
import itertools
from unittest.mock import patch

from test_core import Base, FakeAuth, events
from smm.ai import AI, AIError
from smm.budget import Budget, BudgetStop
from smm.editor import REQUIRED, fashion_options, verify_product
from smm.engine import Engine
from smm.fashion import Fashion
from smm.store import Store


class ProductAI:
    """A deterministic provider; no marketplace or model network calls."""
    def __init__(self, missing=(), ungrounded=False):
        self.calls = []
        self.missing = set(missing)
        self.ungrounded = ungrounded

    def call(self, cid, sid, instructions, context, web=False):
        self.calls.append((web, context))
        if web:
            cat = context['category']
            if cat in self.missing:
                return {'items': []}, set()
            article = 100000 + sorted(REQUIRED).index(cat) + len(self.calls)*100
            url = f'https://www.wildberries.ru/catalog/{article}/detail.aspx'
            return {'items': [dict(name=cat, category=cat, price=1000, winter=True, url=url)]}, set() if self.ungrounded else {url}
        choices = [[x['id'] for x in context['catalog'] if x['category'] == cat] for cat in sorted(REQUIRED)]
        ids = next((list(combination) for combination in itertools.product(*choices)
                    if sorted(combination) not in [sorted(x) for x in context.get('recent', [])]), [])
        return {'options': [dict(title='Зимний образ', description='Полный комплект', item_ids=ids)]}, set()


class FashionTests(Base):
    def fashion_slot(self, offset=0):
        cid = 5
        self.channel(cid)
        sid = self.store.execute("INSERT INTO slots(channel_id,due,label,state) VALUES(?,?,?,'preparing')",
                                 (cid, self.now+2400+offset, '15:00'))
        return self.store.channel(cid), self.store.slot(sid)

    def preparer(self, ai=None, verifier=None):
        return Fashion(self.store, ai or ProductAI(), verifier=verifier or self.verified,
                       clock=lambda:self.now)

    @staticmethod
    def verified(p, now):
        return dict(p, verified=True, checked_at=now)

    def test_empty_catalog_discovers_all_six_categories_and_composes(self):
        ch, slot = self.fashion_slot()
        ai = ProductAI()
        options = self.preparer(ai).prepare(ch, slot)
        self.assertEqual(len(ai.calls), 7)
        self.assertEqual({c['category'] for web,c in ai.calls if web}, REQUIRED)
        self.assertFalse(options[0]['meta']['unverified'])
        self.assertEqual(options[0]['meta']['total'], 6000)
        self.assertFalse(options[0]['media'])
        self.assertIn('подписку', options[0]['meta']['image_status'])

    def test_warm_cache_uses_one_compose_request(self):
        ch, slot = self.fashion_slot()
        self.preparer().prepare(ch, slot)
        ch, second = self.fashion_slot(60)
        ai = ProductAI()
        self.preparer(ai).prepare(ch, second)
        self.assertEqual([web for web,c in ai.calls], [False])

    def test_missing_category_saves_successes_and_next_slot_only_fills_gap(self):
        ch, slot = self.fashion_slot()
        ai = ProductAI(missing={'bag'})
        with self.assertRaisesRegex(AIError, 'Не найдены'):
            self.preparer(ai).prepare(ch, slot)
        self.assertEqual(len(self.store.get('fashion_cache:5')['items']), 5)
        ch, slot = self.fashion_slot(60)
        ai = ProductAI()
        self.preparer(ai).prepare(ch, slot)
        self.assertEqual([ctx['category'] for web,ctx in ai.calls if web], ['bag'])

    def test_ungrounded_items_never_enter_cache(self):
        ch, slot = self.fashion_slot()
        with self.assertRaisesRegex(AIError, 'Не найдены'):
            self.preparer(ProductAI(ungrounded=True)).prepare(ch, slot)
        self.assertEqual(self.store.get('fashion_cache:5')['items'], [])

    def test_retry_cannot_reset_persistent_step_cap(self):
        ch, slot = self.fashion_slot()
        limits = self.store.get('limits')
        limits['fashion_steps'] = 4
        self.store.put('limits', limits)
        ai = ProductAI()
        with self.assertRaises(AIError):
            self.preparer(ai).prepare(ch, slot)
        self.assertEqual(self.store.get('fashion_steps:'+str(slot['id'])), 3)
        before = len(ai.calls)
        with self.assertRaises(AIError):
            self.preparer(ai).prepare(ch, slot)
        self.assertEqual(len(ai.calls), before)

    def test_pause_prevents_all_requests(self):
        ch, slot = self.fashion_slot()
        self.store.put('paused', True)
        ai = ProductAI()
        with self.assertRaises(AIError):
            self.preparer(ai).prepare(ch, slot)
        self.assertEqual(ai.calls, [])

    def test_deadline_prevents_all_requests(self):
        ch, slot = self.fashion_slot()
        ai = ProductAI()
        with self.assertRaises(AIError):
            Fashion(self.store, ai, clock=lambda:slot['due']-170).prepare(ch, slot)
        self.assertEqual(ai.calls, [])

    def test_blocked_cards_are_unverified_and_approval_requires_confirmation(self):
        ch, slot = self.fashion_slot()
        opts = self.preparer(verifier=lambda p,n:dict(p, verified=False, checked_at=0)).prepare(ch, slot)
        self.assertTrue(opts[0]['meta']['unverified'])
        opts[0]['media'] = 'file-id'
        ids = self.store.add_candidates(slot['id'], opts, self.now)
        with self.assertRaises(ValueError):
            self.store.approve(ids[0], self.now)

    def test_automatic_item_is_not_verified_by_timestamp_alone(self):
        ch, slot = self.fashion_slot()
        opts = self.preparer().prepare(ch, slot)
        products = opts[0]['meta']['products']
        for p in products:
            p.update(automatic=True, verified=False, checked_at=self.now)
        result = {'options':[dict(title='Образ', item_ids=list(range(1,7)))]}
        options = fashion_options(result,ch['config'],10000,self.now,catalog=products)
        self.assertTrue(options[0]['meta']['unverified'])

    def test_expired_cache_is_searched_again(self):
        ch, slot = self.fashion_slot()
        self.preparer().prepare(ch, slot)
        saved = self.store.get('fashion_cache:5')
        for p in saved['items']:
            p['found_at'] = self.now-43201
        self.store.put('fashion_cache:5', saved)
        ch, slot = self.fashion_slot(60)
        ai = ProductAI()
        self.preparer(ai).prepare(ch, slot)
        self.assertEqual(len(ai.calls), 7)

    def test_changed_topic_does_not_reuse_wrong_cache(self):
        ch, slot = self.fashion_slot()
        self.preparer().prepare(ch, slot)
        ch['config']['topic'] = 'Новая тема'
        ch, slot2 = dict(ch), self.fashion_slot(60)[1]
        ai = ProductAI()
        self.preparer(ai).prepare(ch, slot2)
        self.assertEqual(len(ai.calls), 7)

    def test_prices_over_budget_never_create_cheaper_fiction(self):
        ch, slot = self.fashion_slot()
        self.preparer().prepare(ch, slot)
        cached = self.store.get('fashion_cache:5')
        for p in cached['items']:
            p['price'] = 2000
        self.store.put('fashion_cache:5', cached)
        ch, slot = self.fashion_slot(60)
        ch['config']['web_search'] = False
        ai = ProductAI()
        with self.assertRaisesRegex(AIError, '12000'):
            self.preparer(ai).prepare(ch, slot)
        self.assertEqual(ai.calls, [])

    def test_malformed_search_items_are_reported(self):
        ch, slot = self.fashion_slot()
        ai = ProductAI()
        ai.call = lambda *a,**kw:({'items':None}, set())
        with self.assertRaisesRegex(AIError, 'формат'):
            self.preparer(ai).prepare(ch, slot)

    def test_all_stages_use_real_budget_accounting(self):
        ch, slot = self.fashion_slot()
        fake = ProductAI()
        payloads = []
        def opener(req, timeout):
            payload = json.loads(req.data)
            payloads.append(payload)
            context = json.loads(payload['input'][0]['content'])
            obj, urls = fake.call(ch['id'], slot['id'], '', context, web='tools' in payload)
            output = [{'type':'web_search_call', 'action':{'sources':[{'url':u} for u in urls]}}] if urls else []
            return events({'type':'response.output_text.delta','delta':json.dumps(obj)},
                          {'type':'response.completed','response':{'output':output,'usage':{'input_tokens':100,'output_tokens':50}}})
        ai = AI(self.store, FakeAuth(), opener)
        self.preparer(ai).prepare(ch, slot)
        self.assertEqual(len(payloads), 7)
        self.assertEqual(Budget(self.store).usage()['tokens'], 7*150)
        self.assertEqual(Budget(self.store).usage()['requests'], 7)
        self.assertNotIn('tools', payloads[-1])

    def test_daily_limit_stops_multistage_search_before_extra_call(self):
        ch, slot = self.fashion_slot()
        limits = self.store.get('limits')
        limits['daily_requests'] = 2
        self.store.put('limits', limits)
        fake = ProductAI()
        def opener(req, timeout):
            payload = json.loads(req.data)
            obj, urls = fake.call(5, slot['id'], '', json.loads(payload['input'][0]['content']), web=True)
            output = [{'type':'web_search_call','action':{'sources':[{'url':u} for u in urls]}}]
            return events({'type':'response.output_text.delta','delta':json.dumps(obj)},
                          {'type':'response.completed','response':{'output':output,'usage':{'input_tokens':100,'output_tokens':50}}})
        with self.assertRaises(BudgetStop):
            self.preparer(AI(self.store, FakeAuth(), opener)).prepare(ch, slot)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(len(self.store.get('fashion_cache:5')['items']), 2)

    def test_engine_accepts_new_outfit_with_same_generic_title(self):
        ch, slot = self.fashion_slot()
        old_slot = self.fashion_slot(-60)[1]
        self.store.add_candidates(old_slot['id'], [dict(title='Зимний образ', body='', media='',
            event_key='outfit:https://www.ozon.ru/product/old-123/', meta={'mode':'fashion'})], self.now-10)
        self.store.execute("UPDATE slots SET state='new' WHERE id=?", (slot['id'],))
        ai = ProductAI()
        with patch('smm.fashion.verify_product', side_effect=self.verified):
            Engine(self.store,self.tg,ai).prepare(slot['id'], self.now)
        self.assertEqual(self.store.slot(slot['id'])['state'], 'pending')

    def test_previous_outfit_does_not_repeat_from_warm_cache(self):
        ch, slot = self.fashion_slot()
        opts = self.preparer().prepare(ch, slot)
        self.store.add_candidates(slot['id'], opts, self.now)
        ch, slot = self.fashion_slot(60)
        ai = ProductAI()
        self.preparer(ai).prepare(ch, slot)
        self.assertEqual(sum(web for web,c in ai.calls), 2)


class FashionMigrationTests(Base):
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
