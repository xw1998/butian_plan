"""Prepare contexts, not model answers. Run once into a fresh directory."""
from pathlib import Path
import sys,json,hashlib,subprocess,time
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from butian.runtime.service import ButianService
out=ROOT/'docs/论文/experiment_results/within-agent-pilot-20261015'
out.mkdir(exist_ok=False)
package=json.loads((ROOT/'docs/论文/任务包.v1.json').read_text(encoding='utf-8'))
manifest=[]
for i,case in enumerate(package['cases']):
 order=['B0','B1','B2'];order=order[i%3:]+order[:i%3]
 for pos,v in enumerate(order):
  rid=case['case_id']+'-'+v;d=out/rid;d.mkdir()
  payload={'run_id':rid,'case':case,'variant':v,'position':pos+1,'host_agent':'current_conversation','prior_answer_exposure':True,'context_carryover':'cannot_reset','scope':'nonblind_within_agent_pilot','model_seed_controlled':False}
  if v=='B0':context='No additional long-term memory supplied.\n'+json.dumps(case['input'],ensure_ascii=False)
  elif v=='B1':context='Fixed retrieval only; no lifecycle update.\n'+json.dumps(case.get('training',{'source':'NIST fixed snapshot','status':'external claim candidate'}),ensure_ascii=False)+'\n'+json.dumps(case['input'],ensure_ascii=False)
  else:
   svc=ButianService(d/'production')
   try:
    svc.add_member('pilot','Current host Agent')
    event=svc.store.append_event('pilot','evidence_recorded',{'training':case.get('training'),'input':case['input'],'scope':'nonblind pilot fixture'},'pilot-'+rid)
    mem=svc.memory('pilot')
    try:mem.put('pilot-memory','lesson',json.dumps({'case':case['case_id'],'training':case.get('training'),'constraints':case['constraints']},ensure_ascii=False),status='warm',source_event_ids=[event])
    finally:mem.close()
    context,report=svc.compose_context('pilot',case['goal']+' '+case['case_id'])
    payload['production_recalled_memory_ids']=report.recalled_memory_ids
   finally:svc.close()
  (d/'context.txt').write_text(context,encoding='utf-8')
  payload['context_sha256']=hashlib.sha256(context.encode()).hexdigest()
  (d/'input.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8');manifest.append(payload)
(out/'manifest.json').write_text(json.dumps({'code_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'created_at':time.time(),'runs':manifest,'model_calls':None,'cost':None,'limitations':['Nonblind single current Agent','No complete recovery or practice lifecycle enacted by this context preparation']},ensure_ascii=False,indent=2),encoding='utf-8')
print(out)
for p in sorted(out.glob('*/context.txt')):print(p.parent.name,p.read_text(encoding='utf-8')[:900])
