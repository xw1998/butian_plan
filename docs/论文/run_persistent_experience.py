from pathlib import Path
import sys,json,hashlib,platform,subprocess,tempfile
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from butian.runtime.service import ButianService
out=Path(__file__).parent/'experiment_results/persistent-experience-20261015';out.mkdir(exist_ok=False)
rows=[]
with tempfile.TemporaryDirectory() as work:
 for i in range(12):
  home=Path(work)/str(i); query=f'calibration serial_probe_{i:03}'; token=hashlib.sha256(f'heldout-calibration-{i}'.encode()).hexdigest()[:12]
  svc=ButianService(home)
  try:
   svc.add_member('worker','Host Agent')
   before,rb=svc.compose_context('worker',query)
   eid=svc.store.append_event('worker','evidence_recorded',{'probe':query,'calibration_token':token,'kind':'synthetic_verified_fixture'},f'cal-{i}')
   mem=svc.memory('worker')
   try:mem.put(f'calibration-{i}','lesson',f'{query}: calibration token {token}; source event {eid}; synthetic fixture only',status='warm',source_event_ids=[eid])
   finally:mem.close()
  finally:svc.close()
  svc=ButianService(home)
  try:
   after,ra=svc.compose_context('worker',query)
   rec={'probe':query,'before_has_token':token in before,'after_restart_has_token':token in after,'memory_ids':ra.recalled_memory_ids,'source_event_id':eid,'before':before,'after':after}
   assert not rec['before_has_token'] and rec['after_restart_has_token'] and f'calibration-{i}' in ra.recalled_memory_ids
   rows.append(rec)
  finally:svc.close()
result={'execution_kind':'mechanism_control','model_weights_changed':False,'neural_model_runs':0,'code_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'python':platform.python_version(),'rows':rows,'summary':{'before_retrieval_hits':0,'after_restart_retrieval_hits':12,'cases':12},'interpretation':'Production persistence and retrieval of newly inserted synthetic experience verified; no task success or learning gain inferred.'}
(out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');(out/'manifest.json').write_text(json.dumps({'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'results_sha256':hashlib.sha256((out/'results.json').read_bytes()).hexdigest()},indent=2),encoding='utf-8');print(json.dumps(result['summary']))
