"""Paper execution only. This module has no broker connector or live-order path."""
from dataclasses import dataclass,asdict
from datetime import datetime,timezone,timedelta
import csv,json,math,re
from decimal import Decimal,ROUND_FLOOR
def positive(value,name):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:raise ValueError(name+' must be finite and positive')
    return float(value)
@dataclass(frozen=True)
class Bar:
    time:datetime
    symbol:str
    open:float
    high:float
    low:float
    close:float
    def __post_init__(self):
        if not isinstance(self.time,datetime) or self.time.tzinfo is None:raise ValueError('Timestamps must include a timezone')
        if not re.fullmatch(r'[A-Z0-9._-]{1,32}',self.symbol):raise ValueError('Invalid symbol')
        for name in ['open','high','low','close']:positive(getattr(self,name),name)
        if self.low>min(self.open,self.close) or self.high<max(self.open,self.close) or self.low>self.high:raise ValueError('Invalid OHLC range')
def read_bars(path):
    bars=[];last={}
    with open(path,newline='',encoding='utf-8-sig') as stream:
        reader=csv.DictReader(stream)
        if not {'time','symbol','open','high','low','close'}<=set(reader.fieldnames or []):raise ValueError('Missing OHLC CSV columns')
        for row in reader:
            bar=Bar(datetime.fromisoformat(row['time'].replace('Z','+00:00')),row['symbol'],*[float(row[k]) for k in ['open','high','low','close']])
            if bar.symbol in last and bar.time<=last[bar.symbol]:raise ValueError('Bars must be strictly chronological per symbol')
            last[bar.symbol]=bar.time;bars.append(bar)
            if len(bars)>250000:raise ValueError('CSV row limit is 250000')
    if not bars:raise ValueError('CSV contains no bars')
    return sorted(bars,key=lambda b:(b.time,b.symbol))
def price_context(bars):
    if len(bars)<20:raise ValueError('At least 20 past bars are required')
    symbol=bars[-1].symbol
    if any(b.symbol!=symbol for b in bars):raise ValueError('Context must contain one symbol')
    recent=bars[-20:]
    return {'symbol':symbol,'price':bars[-1].close,'time':bars[-1].time.isoformat(),'ma20':sum(b.close for b in recent)/20,'ma50':sum(b.close for b in bars[-50:])/len(bars[-50:]),'recent_high':max(b.high for b in recent),'recent_low':min(b.low for b in recent),'last_five_closes':[b.close for b in bars[-5:]]}
def parse_signal(raw):
    if not isinstance(raw,str) or len(raw)>20000:raise ValueError('Invalid response size/type')
    def unique(pairs):
        obj={}
        for key,value in pairs:
            if key in obj:raise ValueError('Duplicate JSON key')
            obj[key]=value
        return obj
    obj=json.loads(raw,object_pairs_hook=unique)
    expected={'signal','entry','stop_loss','take_profit','reasoning'}
    if not isinstance(obj,dict) or set(obj)!=expected or obj['signal'] not in {'BUY','SELL','NO_TRADE'}:raise ValueError('Invalid signal schema')
    if not isinstance(obj['reasoning'],str) or len(obj['reasoning'])>2000:raise ValueError('Invalid reasoning')
    if obj['signal']=='NO_TRADE':
        if any(obj[k] is not None for k in ['entry','stop_loss','take_profit']):raise ValueError('NO_TRADE must have null prices')
    else:
        for name in ['entry','stop_loss','take_profit']:positive(obj[name],name)
    return obj
@dataclass
class Position:
    symbol:str
    signal:str
    entry:float
    stop_loss:float
    take_profit:float
    units:float
    initial_risk:float
    mark:float
    opened:str
class PaperAccount:
    def __init__(self,capital=10000.,risk_fraction=.01,max_open=3,min_rr=2.,point_value=1.,unit_step=.01,margin_per_unit=1.,fee_per_unit=0.,max_drawdown=.03):
        for name,value in locals().copy().items():
            if name not in {'self','max_open','fee_per_unit'}:positive(value,name)
        if not isinstance(max_open,int) or isinstance(max_open,bool) or not 1<=max_open<=3:raise ValueError('Maximum open trades must be 1–3')
        if risk_fraction>.01 or max_drawdown>.03:raise ValueError('Risk limits cannot exceed 1% per trade or 3% session drawdown')
        if min_rr<1.5:raise ValueError('Minimum reward/risk must be at least 1.5')
        if isinstance(fee_per_unit,bool) or not math.isfinite(fee_per_unit) or fee_per_unit<0:raise ValueError('Invalid fee')
        self.capital=self.balance=float(capital);self.risk_fraction=risk_fraction;self.max_open=max_open;self.min_rr=min_rr;self.point_value=point_value;self.unit_step=unit_step;self.margin_per_unit=margin_per_unit;self.fee_per_unit=fee_per_unit;self.max_drawdown=max_drawdown
        self.positions=[];self.ledger=[];self.last={}
    @property
    def equity(self):return self.balance+sum((p.mark-p.entry)*(1 if p.signal=='BUY' else -1)*p.units*self.point_value for p in self.positions)
    def open(self,symbol,signal,time,current_price):
        positive(current_price,'current price');atom=Bar(time,symbol,current_price,current_price,current_price,current_price)
        if symbol in self.last and time<self.last[symbol]:raise ValueError('Cannot open a position before the latest observed bar')
        signal=parse_signal(json.dumps(signal,allow_nan=False))
        if signal['signal']=='NO_TRADE':return False
        if len(self.positions)>=self.max_open or any(p.symbol==symbol for p in self.positions):return False
        if self.equity<=self.capital*(1-self.max_drawdown):return False
        entry,stop,target=[signal[k] for k in ['entry','stop_loss','take_profit']]
        if not math.isclose(entry,current_price,rel_tol=1e-6):raise ValueError('Paper entry must equal the observed close')
        side=1 if signal['signal']=='BUY' else -1
        if (entry-stop)*side<=0 or (target-entry)*side<=0:raise ValueError('Stops/targets must bracket entry in the correct direction')
        distance=abs(entry-stop)
        if abs(target-entry)/distance+1e-10<self.min_rr:raise ValueError('Insufficient reward/risk')
        budget=min(self.equity*self.risk_fraction,max(0.,self.equity*.03-sum(p.initial_risk for p in self.positions)))
        per_unit=distance*self.point_value+self.fee_per_unit
        free_margin=max(0.,self.equity-sum(p.units*self.margin_per_unit for p in self.positions))
        raw=min(budget/per_unit,free_margin/self.margin_per_unit)
        units=float((Decimal(str(raw))/Decimal(str(self.unit_step))).to_integral_value(rounding=ROUND_FLOOR)*Decimal(str(self.unit_step)))
        if units<=0:return False
        risk=units*per_unit
        if risk>budget+1e-8:raise ValueError('Rounded size exceeds risk budget')
        self.balance-=units*self.fee_per_unit
        position=Position(symbol,signal['signal'],entry,stop,target,units,risk,entry,time.isoformat());self.positions.append(position)
        self.ledger.append({'event':'OPEN',**asdict(position),'risk_reward':abs(target-entry)/distance,'fee':units*self.fee_per_unit})
        return True
    def on_bar(self,bar):
        if bar.symbol in self.last and bar.time<=self.last[bar.symbol]:raise ValueError('Duplicate/out-of-order bar')
        self.last[bar.symbol]=bar.time
        for p in list(self.positions):
            if p.symbol!=bar.symbol or bar.time<=datetime.fromisoformat(p.opened):continue
            p.mark=bar.close;side=1 if p.signal=='BUY' else -1
            stop=bar.low<=p.stop_loss if side==1 else bar.high>=p.stop_loss
            target=bar.high>=p.take_profit if side==1 else bar.low<=p.take_profit
            if not stop and not target:continue
            # If both levels were touched, assume the stop first. Gaps worsen
            # stop fills; favorable target gaps do not improve target fills.
            fill=min(bar.open,p.stop_loss) if side==1 else max(bar.open,p.stop_loss)
            if not stop:fill=p.take_profit
            pnl=(fill-p.entry)*side*p.units*self.point_value;self.balance+=pnl;self.positions.remove(p)
            self.ledger.append({'event':'CLOSE','symbol':p.symbol,'time':bar.time.isoformat(),'exit':fill,'reason':'STOP' if stop else 'TARGET','pnl_before_entry_fee':pnl,'balance':self.balance})
    def report(self):
        return {'mode':'paper','execution_enabled':False,'balance':self.balance,'equity':self.equity,'open_positions':[asdict(p) for p in self.positions],'ledger':self.ledger,'limits':{'risk_fraction':self.risk_fraction,'max_open':self.max_open,'min_rr':self.min_rr,'point_value':self.point_value,'unit_step':self.unit_step,'margin_per_unit':self.margin_per_unit,'fee_per_unit':self.fee_per_unit},'limitation':'Synthetic/paper research only. Gap losses can exceed intended stop risk. No spreads, market liquidity or live execution are modeled.'}
def demo_bars():
    start=datetime(2026,1,1,tzinfo=timezone.utc)
    return [Bar(start+timedelta(hours=i),'DEMO',100+i*.1,100.2+i*.1,99.8+i*.1,100.1+i*.1) for i in range(80)]
def demo_signal(context,min_rr=2.):
    price=context['price']
    if context['ma20']<=context['ma50']:return {'signal':'NO_TRADE','entry':None,'stop_loss':None,'take_profit':None,'reasoning':'Deterministic demo rule; no trend.'}
    return {'signal':'BUY','entry':price,'stop_loss':price-1,'take_profit':price+min_rr,'reasoning':'Deterministic demonstration of paper order handling; no predictive claim.'}
def replay(bars,provider,account):
    history={};decisions=[]
    for bar in bars:
        account.on_bar(bar);past=history.setdefault(bar.symbol,[]);past.append(bar)
        if len(past)<20:continue
        context=price_context(past[-50:])
        try:
            signal=provider(context);opened=account.open(bar.symbol,signal,bar.time,bar.close)
            decisions.append({'time':bar.time.isoformat(),'symbol':bar.symbol,'status':'OPENED' if opened else 'NO_NEW_POSITION'})
        except (ValueError,RuntimeError,json.JSONDecodeError):
            decisions.append({'time':bar.time.isoformat(),'symbol':bar.symbol,'status':'REJECTED_INVALID_OR_UNAVAILABLE_SIGNAL'})
    result=account.report();result['decisions']=decisions;return result
