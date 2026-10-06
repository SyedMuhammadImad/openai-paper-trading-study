import unittest,json,tempfile,csv,random,math
from pathlib import Path
from datetime import datetime,timezone,timedelta
from paper import *
from providers import Provider,http_post
T=datetime(2026,1,1,tzinfo=timezone.utc)
def signal(side='BUY',entry=100.,distance=1.,rr=2.):
    direction=1 if side=='BUY' else -1
    return {'signal':side,'entry':entry,'stop_loss':entry-direction*distance,'take_profit':entry+direction*distance*rr,'reasoning':'Unit test'}
class PaperTests(unittest.TestCase):
    def test_json_rejects_malformed_nonfinite_extra_and_duplicate_fields(self):
        for value in ['not JSON','[]','{}','{"signal":"BUY","signal":"SELL"}',json.dumps({**signal(),'entry':float('nan')}),json.dumps({**signal(),'entry':True}),json.dumps({**signal(),'extra':0})]:
            with self.assertRaises(ValueError):parse_signal(value)
        self.assertEqual(parse_signal(json.dumps(signal()))['signal'],'BUY')
    def test_hold_has_null_prices_and_never_opens(self):
        account=PaperAccount();hold={'signal':'NO_TRADE','entry':None,'stop_loss':None,'take_profit':None,'reasoning':'No evidence'}
        self.assertFalse(account.open('X',hold,T,100));self.assertEqual(account.balance,10000)
        with self.assertRaises(ValueError):parse_signal(json.dumps({**hold,'entry':100}))
    def test_brackets_entry_and_ratio_are_recomputed(self):
        for value in [signal('BUY',distance=-1),signal('SELL',distance=-1),signal(rr=.5),signal(entry=105)]:
            with self.assertRaises(ValueError):PaperAccount().open('X',value,T,100)
    def test_lot_size_never_rounds_up_randomized(self):
        rng=random.Random(42)
        for _ in range(200):
            capital=rng.uniform(1000,100000);distance=rng.uniform(.01,4);point=rng.uniform(1,50);fee=rng.uniform(0,2);step=rng.choice([.001,.01,.1,1])
            account=PaperAccount(capital=capital,point_value=point,unit_step=step,fee_per_unit=fee)
            opened=account.open('X',signal(distance=distance),T,100)
            if not opened:
                self.assertGreater(step*(distance*point+fee),capital*.01)
                continue
            p=account.positions[0]
            self.assertLessEqual(p.initial_risk,capital*.01+1e-8)
            self.assertAlmostEqual(p.units/step,round(p.units/step),places=6)
    def test_maximum_positions_duplicates_aggregate_risk_and_margin(self):
        account=PaperAccount()
        for symbol in ['A','B','C']:self.assertTrue(account.open(symbol,signal(),T,100))
        self.assertFalse(account.open('D',signal(),T,100));self.assertFalse(account.open('A',signal(),T,100))
        self.assertLessEqual(sum(p.initial_risk for p in account.positions),account.capital*.03+1e-8)
        margin=PaperAccount(margin_per_unit=500)
        margin.open('X',signal(),T,100);self.assertLessEqual(margin.positions[0].units*500,10000)
        self.assertFalse(PaperAccount(unit_step=10000).open('X',signal(),T,100))
    def test_buy_sell_stop_target_and_fees_reconcile(self):
        for side in ['BUY','SELL']:
            account=PaperAccount(fee_per_unit=.1);account.open('X',signal(side),T,100);units=account.positions[0].units
            target=102 if side=='BUY' else 98
            bar=Bar(T+timedelta(hours=1),'X',100,max(100,target),min(100,target),target)
            account.on_bar(bar);self.assertFalse(account.positions)
            self.assertAlmostEqual(account.balance,10000+units*(2-.1))
            self.assertEqual(account.ledger[-1]['reason'],'TARGET')
    def test_both_brackets_touched_uses_stop_and_gap_worsens_fill(self):
        account=PaperAccount();account.open('X',signal(),T,100)
        account.on_bar(Bar(T+timedelta(hours=1),'X',100,103,98,101))
        self.assertEqual(account.balance,9900);self.assertEqual(account.ledger[-1]['reason'],'STOP')
        account=PaperAccount();account.open('X',signal(),T,100)
        account.on_bar(Bar(T+timedelta(hours=1),'X',95,96,94,95))
        self.assertEqual(account.balance,9500);self.assertFalse(account.open('Y',signal(),T,100))
    def test_bad_ohlc_timestamps_and_empty_csv(self):
        for args in [(T,'X',100,99,98,100),(T,'X',100,101,101,100),(T,'X',0,1,0,1),(T,'invalid name',1,1,1,1),(T.replace(tzinfo=None),'X',1,1,1,1)]:
            with self.assertRaises(ValueError):Bar(*args)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'bars.csv';path.write_text('time,symbol,open,high,low,close\n')
            with self.assertRaises(ValueError):read_bars(path)
            row=T.isoformat()+',X,100,101,99,100\n';path.write_text('time,symbol,open,high,low,close\n'+row+row)
            with self.assertRaises(ValueError):read_bars(path)
    def test_true_ma50_not_fiftieth_bar_and_no_lookahead(self):
        bars=demo_bars();context=price_context(bars[:50]);self.assertAlmostEqual(context['ma50'],sum(b.close for b in bars[:50])/50)
        seen=[]
        def provider(context):seen.append(context['price']);return demo_signal(context)
        result=replay(bars[:25],provider,PaperAccount());self.assertEqual(seen,[b.close for b in bars[19:25]])
        self.assertFalse(result['execution_enabled']);self.assertTrue(result['open_positions'])
    def test_duplicate_bar_and_invalid_account_settings(self):
        account=PaperAccount();bar=demo_bars()[0];account.on_bar(bar)
        with self.assertRaises(ValueError):account.on_bar(bar)
        for settings in [{'capital':0},{'risk_fraction':.02},{'max_open':4},{'max_open':True},{'point_value':float('inf')},{'fee_per_unit':-1}]:
            with self.assertRaises(ValueError):PaperAccount(**settings)
    def test_failed_model_never_creates_order(self):
        def fail(context):raise RuntimeError('mock provider failed')
        result=replay(demo_bars()[:22],fail,PaperAccount());self.assertFalse(result['ledger']);self.assertEqual(result['equity'],10000);self.assertEqual(len(result['decisions']),3)
class ProviderTests(unittest.TestCase):
    def test_three_transports_validate_requests_and_parse_signal(self):
        raw=json.dumps(signal())
        for kind in ['openai','groq','anthropic']:
            called=[]
            def transport(url,payload,headers):
                called.append((url,payload,headers))
                if kind=='openai':return {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':raw}]}]}
                if kind=='groq':return {'choices':[{'finish_reason':'stop','message':{'content':raw}}]}
                return {'stop_reason':'end_turn','content':[{'type':'text','text':raw}]}
            provider=Provider(kind,model='unit-test-model',key='unit-test-placeholder',transport=transport)
            self.assertEqual(provider({'price':100})['signal'],'BUY');self.assertEqual(len(called),1)
            url,payload,headers=called[0];self.assertTrue(url.startswith('https://'));self.assertEqual(payload['model'],'unit-test-model')
            if kind=='openai':self.assertFalse(payload['store']);self.assertEqual(payload['text']['format']['type'],'json_schema')
            if kind=='groq':self.assertEqual(payload['response_format']['type'],'json_object')
            if kind=='anthropic':self.assertEqual(headers['anthropic-version'],'2023-06-01')
    def test_incomplete_refused_and_invalid_responses_fail_closed(self):
        cases=[('openai',None),('groq',[]),('anthropic',{'stop_reason':'end_turn','content':None}),('openai',{'status':'incomplete'}),('openai',{'status':'completed','output':[{'type':'message','content':[{'type':'refusal','refusal':'no'}]}]}),('groq',{'choices':[{'finish_reason':'length','message':{'content':'{}'}}]}),('groq',{}),('anthropic',{'stop_reason':'max_tokens'}),('anthropic',{'stop_reason':'end_turn','content':[{'type':'text','text':'not JSON'}]})]
        for kind,response in cases:
            with self.assertRaises(RuntimeError):Provider(kind,model='unit-test-model',key='unit-test-placeholder',transport=lambda *args:response)({'price':100})
if __name__=='__main__':unittest.main()
