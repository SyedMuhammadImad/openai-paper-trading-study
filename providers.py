"""Optional provider transport; imports and demo runs never call an external API."""
import json,os,urllib.request,urllib.error
from paper import parse_signal
SCHEMA={'type':'object','properties':{'signal':{'type':'string','enum':['BUY','SELL','NO_TRADE']},'entry':{'type':['number','null']},'stop_loss':{'type':['number','null']},'take_profit':{'type':['number','null']},'reasoning':{'type':'string'}},'required':['signal','entry','stop_loss','take_profit','reasoning'],'additionalProperties':False}
INSTRUCTION='Analyze only the supplied historical price summary for paper research. NO_TRADE is valid. Return JSON matching '+json.dumps(SCHEMA)+'. Entry must equal the supplied price; brackets must match direction. Do not assert institutional expertise, certainty or guaranteed returns.'
def http_post(url,payload,headers):
    request=urllib.request.Request(url,data=json.dumps(payload,allow_nan=False).encode(),headers={'Content-Type':'application/json',**headers},method='POST')
    try:
        with urllib.request.urlopen(request,timeout=30) as response:
            raw=response.read(1000001)
            if len(raw)>1000000:raise RuntimeError('Provider response exceeds limit')
            return json.loads(raw)
    except Exception:raise RuntimeError('Provider request failed; no order created') from None
class Provider:
    def __init__(self,kind,model=None,key=None,transport=http_post):
        if kind not in {'openai','groq','anthropic'}:raise ValueError('Unknown provider')
        self.kind=kind;self.model=model or os.environ.get(kind.upper()+'_MODEL','');self._key=key or os.environ.get(kind.upper()+'_API_KEY','');self.transport=transport
        if not self.model or not self._key:raise ValueError('Set provider MODEL and API_KEY environment variables')
    def __call__(self,context):
        try:return self._call(context)
        except Exception:raise RuntimeError('Invalid or unavailable provider signal; no order created') from None
    def _call(self,context):
        prompt=json.dumps(context,allow_nan=False)
        if self.kind=='openai':
            payload={'model':self.model,'instructions':INSTRUCTION,'input':prompt,'store':False,'text':{'format':{'type':'json_schema','name':'paper_signal','strict':True,'schema':SCHEMA}}}
            result=self.transport('https://api.openai.com/v1/responses',payload,{'Authorization':'Bearer '+self._key})
            if result.get('status')!='completed':raise RuntimeError('Provider response was incomplete')
            content=[c for item in result.get('output',[]) if item.get('type')=='message' for c in item.get('content',[])]
            if any(c.get('type')=='refusal' for c in content):raise RuntimeError('Provider refused')
            raw=''.join(c.get('text','') for c in content if c.get('type')=='output_text')
        elif self.kind=='groq':
            payload={'model':self.model,'messages':[{'role':'system','content':INSTRUCTION},{'role':'user','content':prompt}],'response_format':{'type':'json_object'},'max_completion_tokens':500}
            result=self.transport('https://api.groq.com/openai/v1/chat/completions',payload,{'Authorization':'Bearer '+self._key})
            try:
                choice=result['choices'][0]
                if choice.get('finish_reason')!='stop':raise RuntimeError('Provider response was incomplete')
                raw=choice['message']['content']
            except (KeyError,IndexError,TypeError):raise RuntimeError('Malformed provider response') from None
        else:
            payload={'model':self.model,'system':INSTRUCTION,'messages':[{'role':'user','content':prompt}],'max_tokens':500}
            result=self.transport('https://api.anthropic.com/v1/messages',payload,{'x-api-key':self._key,'anthropic-version':'2023-06-01'})
            if result.get('stop_reason')!='end_turn':raise RuntimeError('Provider response was incomplete')
            raw=''.join(c.get('text','') for c in result.get('content',[]) if c.get('type')=='text')
        try:return parse_signal(raw)
        except (ValueError,TypeError):raise RuntimeError('Provider returned an invalid signal') from None
