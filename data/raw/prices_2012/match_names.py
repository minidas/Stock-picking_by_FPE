import json, re, difflib
from pathlib import Path
u=json.loads(Path('data/universe_2012_members.json').read_text(encoding='utf8'))['members']
p=json.loads(Path('data/research_2012_prices.json').read_text(encoding='utf8'))['prices']
def norm(s):
    s=re.sub('[^a-z0-9]','',s.lower())
    for a in ['classanvs','classbnvs','classa','classb','registered','the']:
        s=s.replace(a,'')
    for suffix in ['incorporated','corporation','company','inc','corp','plc','ltd','co','nv']:
        s=re.sub(suffix+'$','',s)
    return s
aliases={x['symbol']:{norm(x['name']),norm(x.get('lawcal_record',{}).get('name',''))} for x in u}
matched=[]; missing=[]
for row in p:
    n=norm(row['company_name'])
    candidates=[x['symbol'] for x in u if n in aliases[x['symbol']]]
    if len(candidates)==1:
        matched.append((row,candidates[0]))
    else:
        scores=sorted(((max(difflib.SequenceMatcher(None,n,a).ratio() for a in aliases[x['symbol']]),x['symbol'],x['name']) for x in u),reverse=True)[:3]
        missing.append({'name':row['company_name'],'normalized':n,'suggestions':scores})
Path('data/raw/prices_2012/unmatched_prices.json').write_text(json.dumps(missing,ensure_ascii=False,indent=2),encoding='utf8')
print('matched',len(matched),'missing',len(missing))
for x in missing:print(ascii(x))
