"""Production lifecycle controls; no language-model outputs or weight updates."""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from butian.runtime.service import ButianService


def run(output, cases):
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for index in range(cases):
        home = output / 'production' / str(index)
        service = ButianService(home)
        states = []
        rejected = []
        mid = f'calibration-{index}'
        query = f'calibration probe_{index:03d}'
        content = f'{query}: y=(raw-{index + 1})/2; applicability=version1'
        def proof(label):
            path = home / (label + '.txt')
            path.write_text(f'case={index}; observation={label}; measured={index * 17 + len(label)}', encoding='utf-8')
            return service.record_evidence(path, owner_member_id='worker', actor_member_id='worker')
        def observe(key, evidence, outcome='success', text=content):
            result = service.record_practice_outcome('worker', memory_id=mid, kind='lesson', content=text,
                outcome=outcome, independent_key=key, evidence_ref=evidence)
            states.append(result['status'])
            return result
        try:
            service.add_member('worker', 'Lifecycle mechanism worker')
            first_proof = proof('first')
            observe('first', first_proof)
            try:
                observe('copied-proof', first_proof)
            except ValueError as exc:
                rejected.append(str(exc))
            else:
                raise AssertionError('duplicate proof accepted')
            observe('second', proof('second'))
            observe('counterexample', proof('counterexample'), 'failure')
            memory = service.memory('worker')
            try:
                old = memory.get(mid)
                revised = content.replace('version1', 'version2; requires fresh source')
                memory.put(mid, 'lesson', revised, status='candidate', source_event_ids=old['source_event_ids'],
                    change_reason='counterexample_revision')
                states.append(memory.get(mid)['status'])
            finally:
                memory.close()
        finally:
            service.close()
        service = ButianService(home)
        try:
            chain = service.memory_versions('worker', mid)
            context, report = service.compose_context('worker', query)
            memory = service.memory('worker')
            try:
                current = memory.get(mid)
            finally:
                memory.close()
            row = {'case': index, 'states': states, 'rejected': rejected,
                'chain_valid': chain['chain']['valid'], 'version_count': len(chain['versions']),
                'restart_status': current['status'], 'revised_content_persisted': current['content'] == revised,
                'recalled': mid in report.recalled_memory_ids, 'context': context}
            assert states == ['candidate', 'warm', 'disputed', 'candidate'], row
            assert row['chain_valid'] and row['revised_content_persisted'] and row['recalled'], row
            assert rejected == ['independent_evidence_required'], row
            rows.append(row)
        finally:
            service.close()
    result = {'execution_kind': 'production_lifecycle_control', 'neural_model_runs': 0,
        'model_weights_changed': False, 'cases': cases, 'rows': rows,
        'code_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'limitations': ['Synthetic distinct evidence documents are not independent scientific replications.',
            'Manual candidate revision is tested; autonomous revision and language-model answer improvement are not measured.',
            'Normal close/open is tested, not crash or power-loss recovery.']}
    target = output / 'results.json'
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'manifest.json').write_text(json.dumps({'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'results_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}, indent=2), encoding='utf-8')
    print(json.dumps({'cases': cases, 'passed': len(rows), 'states': states}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cases', type=int, default=24)
    args = parser.parse_args()
    if args.cases <= 0:
        parser.error('cases must be positive')
    run(args.output, args.cases)
