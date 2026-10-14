import hashlib
import json
import subprocess
import sys

from butian.mcp_server import call_tool
from butian.runtime.service import ButianService


def test_reviewed_source_practice_then_business_reuse_and_counterevidence(tmp_path):
    service = ButianService(tmp_path)
    auth = {'actor_member_id': 'worker', 'member_id': 'worker', 'token': 'source-test'}
    try:
        service.add_member('worker', 'Worker')
        service.store.set_member_token('worker', auth['token'])
        raw = 'Convert centimeters to meters by dividing by 100.'.encode()
        source = service.learning.add_source('manual', 'https://example.org/units', 'Units', hashlib.sha256(raw).hexdigest(), owner_member_id='worker')
        service.learning.save_source_snapshot(source, raw, owner_member_id='worker')
        service.learning.verify_source_content(source, raw, owner_member_id='worker')
        task = service.dispatch_learning_extractions()[0]['task_id']
        package = service.export_execution(task, actor_member_id='worker')
        output = json.dumps({'claims': [{'claim': raw.decode(), 'quote': raw.decode(), 'conditions': ['length in centimeters'], 'limitations': ['length conversion only'], 'predictions': ['125 cm equals 1.25 m'], 'falsifiers': ['conversion disagrees with measured meter scale']}]})
        artifact = tmp_path / 'extracted.json'
        artifact.write_text(output)
        evidence = service.record_evidence(artifact, owner_member_id='worker', actor_member_id='worker')
        delivered = call_tool(service, 'butian_execution_submit', dict(auth, task_id=task, expected_revision=package['task_revision'], problem_revision=package['problem_revision'], submission_id='extract', output=output, evidence_ids=[evidence]))
        service.review_task(task, delivered['revision'], 'approve', reviewer_member_id='worker', actor_member_id='worker')
        claim = service.learning.claims_for_source(source, owner_member_id='worker')[0]
        prepared = service.prepare_claim_practice(claim['claim_id'], owner_member_id='worker', description='Check a physical-unit conversion with an independent Python computation')
        service.grant_hypothesis_authorization(prepared['action_id'], actor_member_id='worker', reason='Local bounded calculation authorized', authorized=True, owner_member_id='worker')
        service.start_hypothesis_action(prepared['action_id'], owner_member_id='worker')
        measured = subprocess.check_output([sys.executable, '-I', '-c', 'print(125 / 100)'], text=True).strip()
        assert measured == '1.25'
        artifact = tmp_path / 'practice.txt'
        artifact.write_text('125 centimeters = ' + measured + ' meters')
        evidence = service.record_evidence(artifact, owner_member_id='worker', actor_member_id='worker')
        result = service.observe_hypothesis(prepared['action_id'], 'supported', [evidence], independent_key='practice-125', actor_member_id='worker', member_id='worker', owner_member_id='worker')
        memory_id = result['candidate_memory_id']
        for index, (cm, observed, outcome) in enumerate(((230, 2.3, 'success'), (300, 30, 'failure'))):
            goal = call_tool(service, 'butian_goal_submit', dict(auth, goal='Convert centimeters to meters', problem={'acceptance_criteria': ['divide centimeter length by 100']}))
            package = service.export_execution(goal['task_id'], actor_member_id='worker')
            assert raw.decode() in package['context']
            expected = float(subprocess.check_output([sys.executable, '-I', '-c', f'print({cm}/100)'], text=True))
            assert (expected == observed) == (outcome == 'success')
            artifact = tmp_path / f'business-{index}.json'
            artifact.write_text(json.dumps({'cm': cm, 'expected': expected, 'observed': observed}))
            evidence = service.record_evidence(artifact, owner_member_id='worker', actor_member_id='worker')
            delivered = call_tool(service, 'butian_execution_submit', dict(auth, task_id=goal['task_id'], expected_revision=package['task_revision'], problem_revision=package['problem_revision'], submission_id=f'business-{index}', output=artifact.read_text(), evidence_ids=[evidence], method_uses=[{'memory_id': memory_id, 'outcome': outcome, 'evidence_id': evidence}]))
            service.review_task(goal['task_id'], delivered['revision'], 'approve', reviewer_member_id='worker', actor_member_id='worker')
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        memory = service.memory('worker')
        try:
            assert memory.get(memory_id)['status'] == 'disputed'
            assert memory.db.execute('SELECT COUNT(*) FROM practice_observations WHERE memory_id=?', (memory_id,)).fetchone()[0] == 3
        finally:
            memory.close()
    finally:
        service.close()
