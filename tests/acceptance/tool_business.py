"""Real subprocess measurements, reviewed tasks and original-reporter repair acceptance."""
import json
import sys
import tempfile
from uuid import uuid4
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from butian.runtime.service import ButianService
from butian.mcp_server import call_tool

home = Path(tempfile.mkdtemp(prefix='tool-business-', dir=ROOT / '.agent_tmp'))
from operator_backend import OperatorBackend
backend = OperatorBackend(home / 'executor')
service = ButianService(home)
backend.attach(service)
manifest = {'input_contract': {'type': 'object'}, 'output_contract': {'type': 'object'}, 'permissions': ['execute'], 'entrypoint': ['python', 'tool.py'], 'risk_level': 'low', 'failure_conditions': ['unsupported unit']}
source = "import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({'meters': x['value']/({'cm':100,'mm':1000}[x['unit']])}))"
try:
    service.add_member('author', 'Conversion author')
    service.add_member('reporter', 'Conversion user')
    tokens = {actor: uuid4().hex for actor in ('author','reporter')}
    for actor, token in tokens.items():
        service.store.set_member_token(actor, token)
    tid = service.propose_tool('author', 'Length conversion', 'Convert cm and mm to meters', reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1, manual_cost_lower=10, call_cost_upper=1, cost_unit='minute', manifest=manifest)['tool_id']
    service.add_tool_version('author', tid, source, manifest)
    def measure(actor, version, value, unit, expected, key, share=None, verification_only=False):
        goal = call_tool(service, 'butian_goal_submit', {'actor_member_id':actor, 'token':tokens[actor], 'goal':f'Convert {value} {unit} to meters', 'problem':{'acceptance_criteria':[f'Result must equal {expected} meters']}})
        task = goal['task_id']
        run = service.execute_tool(actor, tid, version, {'value': value, 'unit': unit}, task_id=task, share_id=share, verification_only=verification_only)
        observed = json.loads(run['stdout']) if run['status'] == 'success' else {'execution_error': run['status']}
        proof = home / (key + '.json')
        proof.write_text(json.dumps({'input': {'value': value, 'unit': unit}, 'observed': observed, 'expected': expected, 'execution_id': run['execution_id']}), encoding='utf-8')
        eid = service.record_evidence(proof, owner_member_id=actor, actor_member_id=actor)
        if observed == {'meters': expected}:
            package = service.export_execution(task, actor_member_id=actor)
            delivered = service.submit_execution(task, expected_revision=package['task_revision'], problem_revision=package['problem_revision'], submission_id=key, output=json.dumps(observed), evidence_ids=[eid], actor_member_id=actor)
            service.review_task(task, delivered['revision'], 'approve', reviewer_member_id=actor, actor_member_id=actor)
        return run, eid, task, observed
    for i, (value, unit, expected) in enumerate([(125,'cm',1.25),(230,'cm',2.3),(3100,'mm',3.1),(4200,'mm',4.2)]):
        run,eid,task,observed = measure('author',1,value,unit,expected,f'author-{i}',verification_only=True)
        assert observed == {'meters':expected}
        service.verify_tool('author',tid,1,'success',independent_key=f'author-{i}',task_id=task,environment={'input_unit':unit},evidence_ids=[eid],execution_id=run['execution_id'],input_hash=run['input_hash'],expected_output=observed)
    assert service.tools._tool(tid)['status'] == 'active'
    share = service.share_tool('author',tid,'reporter',permission='team_execute')['share_id']
    service.accept_tool_share('reporter',share)
    run,eid,task,observed = measure('reporter',1,125,'cm',1.25,'recipient-trial',share=share)
    service.trial_tool('reporter',share,'success',independent_key='recipient',task_id=task,evidence_ids=[eid],execution_id=run['execution_id'],input_hash=run['input_hash'],expected_output=observed)
    failed,eid,task,observed = measure('reporter',1,2,'m',2,'recipient-failure',share=share)
    assert failed['status'] != 'success'
    issue = service.report_tool_issue('reporter',tid,1,description='Meters input must preserve value',execution_id=failed['execution_id'])
    service.add_tool_version('author',tid,source.replace("'mm':1000", "'mm':1000,'m':1"),{**manifest,'description':'Support meters'})
    repair = service.repair_tool_issue('author',issue['issue_id'],repair_version=2,diagnosis='Add identity conversion for meters')
    run,eid,task,observed = measure('reporter',2,2,'m',2,'reporter-repaired',share=repair['trial_share_id'])
    assert observed == {'meters':2}
    service.trial_tool('reporter',repair['trial_share_id'],'success',independent_key='repair',task_id=task,evidence_ids=[eid],execution_id=run['execution_id'],input_hash=run['input_hash'],expected_output=observed)
    assert service.confirm_tool_repair('reporter',issue['issue_id'],execution_id=run['execution_id'],resolved=True)['status'] == 'resolved'
    assert service.tools._tool(tid)['current_version'] == 1
    assert service.archive_tool('author',tid,reason='Business acceptance archive')['status'] == 'archived'
    assert service.restore_tool('author',tid)['status'] == 'candidate'
    for i, (value, unit, expected) in enumerate([(510,'cm',5.1),(620,'cm',6.2),(7300,'mm',7.3),(8400,'mm',8.4)]):
        run,eid,task,observed = measure('author',1,value,unit,expected,f'restored-{i}',verification_only=service.tools._tool(tid)['status'] == 'candidate')
        assert observed == {'meters':expected}
        service.verify_tool('author',tid,1,'success',independent_key=f'restored-{i}',task_id=task,environment={'input_unit':unit},evidence_ids=[eid],execution_id=run['execution_id'],input_hash=run['input_hash'],expected_output=observed)
    assert service.tools._tool(tid)['status'] == 'active'
    service.close()
    service = ButianService(home)
    backend.attach(service)
    assert service.tools._tool(tid)['status'] == 'active'
    assert service.store.db.execute('SELECT status FROM tool_issues WHERE issue_id=?',(issue['issue_id'],)).fetchone()[0] == 'resolved'
    print('PASS real conversion, reviewed tasks, activation, recipient trial, failure, original-reporter repair, archive/restore, revalidation and restart')
    print('ARTIFACT_HOME=' + str(home))
finally:
    try:
        service.close()
    finally:
        backend.close()
