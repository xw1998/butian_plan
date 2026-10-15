"""Paired, deterministic replay of host-authored hypotheses through production dream APIs.
Not a blind LLM trial. The proposed affine rule is fixed before held-out evaluation.
"""
from pathlib import Path
import argparse, hashlib, json, random, subprocess, sys, time
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from butian.runtime.service import ButianService

def run(output):
    output.mkdir(parents=True, exist_ok=False)
    rows, traces = [], []
    for seed in range(10):
        rng = random.Random(seed)
        for family in ('affine', 'saturating'):
            a, b = rng.randint(2, 7), rng.randint(1, 9)
            def oracle(x):
                return a * (min(x, 6) if family == 'saturating' else x) + b
            # Host-authored induction from x=1,2,3, before validation/test access.
            train = [(x, oracle(x)) for x in (1, 2, 3)]
            slope = train[1][1] - train[0][1]
            intercept = train[0][1] - slope
            for condition in ('off', 'unverified', 'verified'):
                home = output / 'production' / f'{seed}-{family}-{condition}'
                s = ButianService(home)
                s.add_member('worker', 'Dream paired replay')
                m = s.memory('worker')
                ids = []
                for x,y in train:
                    mid = f'practice-{x}'
                    m.put(mid, 'experience', json.dumps({'x':x,'y':y}), status='candidate')
                    ids.append(mid)
                m.close()
                source_reads, verification_reads, accepted, states = 0, 0, False, []
                started = time.perf_counter()
                if condition != 'off':
                    gate = s.dream_run('worker', min_interval=0)
                    assert gate['status'] == 'completed', gate
                    dist = s.dream_distill('worker', ids, summary=f'Observed three points; affine candidate y={slope}*x+{intercept}')
                    abstract = s.dream_abstract('worker', ids, pattern=f'y={slope}*x+{intercept}', applicability=['integer input, same sensor configuration'], failure_conditions=['unobserved nonlinear or saturation behavior'])
                    hyp = s.dream_propose_hypothesis('worker', abstract['abstraction_id'], f'y={slope}*x+{intercept}', prior_reliability=0.5, argument='Host-authored constant first difference from three observations')
                    hid = hyp['hypothesis_id']
                    accepted = True
                    if condition == 'verified':
                        action = s.dream_schedule_verification('worker', hid, 'Probe held-out x=5 and x=10', cost=0, risk=0, expected_information_gain=1, authorized=True)
                        s.dream_start_verification('worker', action['action_id'])
                        probes = [{'x':x,'observed':oracle(x),'predicted':slope*x+intercept} for x in (5,10)]
                        verification_reads = len(probes)
                        accepted = all(p['observed']==p['predicted'] for p in probes)
                        proof = home / 'verification.json'
                        proof.write_text(json.dumps(probes), encoding='utf-8')
                        ev = s.record_evidence(proof, owner_member_id='worker', actor_member_id='worker')
                        result = s.dream_record_verification('worker', hid, outcome='supported' if accepted else 'rejected', evidence_refs=[ev], action_id=action['action_id'], new_memory_id='general-rule' if accepted else None, new_content=f'y={slope}*x+{intercept}' if accepted else None)
                        traces.append({'seed':seed,'family':family,'training':train,'hypothesis':f'y={slope}*x+{intercept}','probes':probes,'verdict':result['status']})
                correct = wrong = 0
                for round_no in range(1,5):
                    for x in (4,7,9,12):
                        expected = oracle(x)
                        predicted = slope*x+intercept if accepted else oracle(x)
                        source_reads += int(not accepted)
                        ok = predicted == expected
                        correct += int(ok); wrong += int(not ok)
                        if condition == 'verified' and accepted:
                            proof = home / f'reuse-{round_no}-{x}.json'
                            proof.write_text(json.dumps({'round':round_no,'x':x,'observed':expected,'predicted':predicted}), encoding='utf-8')
                            ev = s.record_evidence(proof, owner_member_id='worker', actor_member_id='worker')
                            observed = s.record_practice_outcome('worker', memory_id='general-rule', kind='experience', content=f'y={slope}*x+{intercept}', outcome='success' if ok else 'failure', independent_key=f'{round_no}-{x}', evidence_ref=ev)
                            states.append(observed['status'])
                        rows.append({'seed':seed,'family':family,'condition':condition,'round':round_no,'x':x,'expected':expected,'predicted':predicted,'correct':ok,'source_read':not accepted})
                elapsed = (time.perf_counter()-started)*1000
                traces.append({'seed':seed,'family':family,'condition':condition,'correct':correct,'wrong':wrong,'source_reads':source_reads,'verification_reads':verification_reads,'accepted':accepted,'states':states,'elapsed_ms':elapsed})
                s.close()
    summary = {}
    for condition in ('off','unverified','verified'):
        records = [r for r in traces if r.get('condition')==condition]
        summary[condition] = {k:sum(r[k] for r in records) for k in ('correct','wrong','source_reads','verification_reads')}
        summary[condition]['accepted_hypotheses'] = sum(r['accepted'] for r in records)
    assert summary['off']['correct']==320 and summary['verified']['correct']==320
    assert summary['unverified']['wrong']==120
    assert summary['verified']['source_reads']==160 and summary['verified']['verification_reads']==40
    result = {'protocol':{'seeds':10,'families':['affine','saturating'],'training_x':[1,2,3],'verification_x':[5,10],'test_x':[4,7,9,12],'rounds':4,'conditions':['off','unverified','verified'],'host_role':'Current host authored the affine induction template; Python replays it through production APIs. No blind or independent model samples.','limitation':'Verification values are disjoint from test values, but family design and host hypothesis are not blinded. Source is always available. Rejecting a hypothesis falls back to source; no automatic nonlinear discovery is claimed.','common_training_reads_per_condition':60},'summary':summary,'traces':traces,'rows':rows,'model_calls_in_script':0,'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()}
    target=output/'results.json'; target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'manifest.json').write_text(json.dumps({'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'results_sha256':hashlib.sha256(target.read_bytes()).hexdigest()},indent=2),encoding='utf-8')
    print(json.dumps(summary))
if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--output',type=Path,required=True); run(p.parse_args().output)
