"""Same resolver, equal source access; production-memory retrieval vs source scans.
Mechanism benchmark, not three independent LLM runs. No invented model latency.
"""
from pathlib import Path
import sys,json,re,time,random,hashlib,subprocess,tempfile
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from butian.runtime.service import ButianService
out=Path(__file__).parent/'experiment_results/same-family-benefit-20261015';out.mkdir(exist_ok=False)
rows=[]; contexts=[]
def parse(text,probe,version):
 matches=re.findall(r'CAL (probe_\d+) version=(\d+) scale=(\d+)',text)
 values={int(scale) for p,v,scale in matches if p==probe and int(v)==version}
 return next(iter(values)) if len(values)==1 else None
with tempfile.TemporaryDirectory() as wd:
 for seed in [1103,2207,3301]:
  rng=random.Random(seed)
  for case in range(8):
   folder=Path(wd)/f'{seed}-{case}';folder.mkdir();archive=folder/'archive';archive.mkdir()
   probe=f'probe_{case:03}';version=2;raw=rng.randrange(100,999);scale=rng.choice([250,500,1000,2000]);reference=raw/scale
   records=[f'CAL probe_{i:03} version=2 scale={scale if i==case else 375+i}' for i in range(8)]
   records += [f'CAL {probe} version=1 scale=100']
   rng.shuffle(records)
   for i,text in enumerate(records):(archive/f'{i:02}.txt').write_text(text,encoding='utf8')
   svc=ButianService(folder/'db')
   try:
    svc.add_member('host','Same host Agent')
    event=svc.store.append_event('host','evidence_recorded',{'calibration':f'CAL {probe} version=2 scale={scale}','kind':'synthetic verified calibration'},f'evidence-{seed}-{case}')
    mem=svc.memory('host')
    try:mem.put('calibration','lesson',f'CAL {probe} version=2 scale={scale}; source event {event}; supersedes version=1 without deleting history',status='warm',source_event_ids=[event])
    finally:mem.close()
   finally:svc.close()
   # Changed raw inputs after the experience is frozen; new task is not a literal answer cache.
   order=['B0','B1','B2'];offset=case%3;order=order[offset:]+order[:offset]
   for variant in order:
    start=time.perf_counter();source_reads=0;context_queries=0;chosen=None;trace=[]
    if variant=='B1':
     context_queries+=1;ctx=f'CAL {probe} version=1 scale=100';chosen=parse(ctx,probe,version);trace.append('fixed-old-version-rejected')
    if variant=='B2':
     svc=ButianService(folder/'db')
     try:ctx,report=svc.compose_context('host',f'{probe} version=2 calibration');context_queries+=1
     finally:svc.close()
     chosen=parse(ctx,probe,version);trace.append('production-compose_context')
     contexts.append({'seed':seed,'case':case,'context':ctx,'recalled_memory_ids':report.recalled_memory_ids})
    if chosen is None:
     candidates=[]
     for p in sorted(archive.glob('*.txt')):
      text=p.read_text(encoding='utf8');source_reads+=1;v=parse(text,probe,version)
      if v is not None:candidates.append(v)
     chosen=candidates[0] if len(set(candidates))==1 else None
     trace.append('source-archive-scan')
    answer=None if chosen is None else raw/chosen;correct=answer is not None and abs(answer-reference)<1e-12
    rows.append({'seed':seed,'case':case,'variant':variant,'raw':raw,'current_version':version,'reference':reference,'answer':answer,'correct':correct,'source_reads':source_reads,'context_queries':context_queries,'total_data_accesses':source_reads+context_queries,'elapsed_seconds':time.perf_counter()-start,'trace':trace})
summary={}
for v in ['B0','B1','B2']:
 rr=[x for x in rows if x['variant']==v];summary[v]={'tasks':len(rr),'correct':sum(x['correct'] for x in rr),'source_reads':sum(x['source_reads'] for x in rr),'context_queries':sum(x['context_queries'] for x in rr),'total_data_accesses':sum(x['total_data_accesses'] for x in rr),'mean_elapsed_seconds':sum(x['elapsed_seconds'] for x in rr)/len(rr)}
assert all(x['correct'] for x in rows)
result={'execution_kind':'mechanism_control','resolver':'shared deterministic version-aware formula; not model answers','code_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'experience_preparation_cost':'one source event and one memory put per case, excluded from online query accesses; not claimed free','permissions':'All conditions may scan the same original archive on miss','rows':rows,'summary':summary,'gain_vs_B0':{'B2_online_access_reduction':1-summary['B2']['total_data_accesses']/summary['B0']['total_data_accesses']},'limitations':['Synthetic calibration family only','No model accuracy/latency improvement inferred','B1 intentionally stale fixed snapshot; no dynamic refresh','12? No: 8 cases x 3 seeds = 24 paired task fixtures; not 24 independent users','Offline writes and memory infrastructure have costs not counted as online reads','No keyword scoring of authored responses']}
(out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8');(out/'retrieved-contexts.json').write_text(json.dumps(contexts,ensure_ascii=False,indent=2),encoding='utf8');(out/'manifest.json').write_text(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir() if p.is_file()},indent=2),encoding='utf8');print(json.dumps({'summary':summary,'gain':result['gain_vs_B0']},indent=2))
