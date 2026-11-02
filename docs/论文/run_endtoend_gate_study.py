"""End-to-end gate benchmark: experience formed through the production gate, then reused.

The published multiround benchmark writes a source rule straight to `warm` with
confidence 1.0 (`remember`), which skips the candidate gate, the second
independent practice and the counterexample path. This script replays the same
task stream but makes the gate part of the loop, so one run measures both how
experience is admitted and what it buys afterwards.

Run: python docs/论文/run_endtoend_gate_study.py --output <new-directory>
No language-model inference; no model weights change; every arm shares one
fixture stream per seed.
"""
from pathlib import Path
import argparse, hashlib, json, platform, random, subprocess, sys, time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from butian.runtime.service import ButianService
from run_multiround_study import FAMILIES, make_rule, parse_context, solve

METHODS = [
    'G0-index',           # no experience: exact version index
    'G1-direct',          # source rule becomes warm on first fetch (published benchmark path)
    'G2-gate',            # production gate: two independent successes before reuse
    'G3-gate-inject',     # gate + a wrong claim carrying one independent evidence (candidate)
    'G4-reuse-inject',    # no state gate + the same wrong claim
    'G5-inject-warm',     # gate + a wrong claim carrying two independent evidences (warm)
]
REPORT_SEEDS = [1103, 2207, 3301, 4409, 5519, 6619, 7723, 8837, 9941, 10151,
                11257, 12301, 13309, 14387, 15401, 16411, 17417, 18427, 19433, 20441]
PILOT_SEEDS = [777001]
INJECT_ROUNDS = (6, 10)          # injected before the tasks of these rounds
REUSABLE_STATUSES = {'warm', 'hot'}
CANDIDATE_REUSABLE = {'G4-reuse-inject'}
INJECT_ARMS = {'G3-gate-inject', 'G4-reuse-inject', 'G5-inject-warm'}


def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def build_stream(seed, rounds, entities_per_family):
    """Same RNG consumption order as the published main study, so fixtures replicate."""
    rng = random.Random(seed)
    entities = [(f'{family}{i:03}', family) for family in FAMILIES for i in range(entities_per_family)]
    archive, active, stream = [], {}, []
    for entity, family in entities:
        rule = make_rule(family, entity, 1, rng)
        active[entity] = rule
        archive.append(rule)
    for i in range(12):
        archive.append(make_rule('calibration', f'distractor{i:03}', 1, rng))
    for rnd in range(1, rounds + 1):
        changed = set()
        if rnd in (5, 9):
            for entity, family in entities:
                if rng.random() < 0.5:
                    previous = active[entity]
                    rule = make_rule(family, entity, previous['version'] + 1, rng)
                    while {k: v for k, v in rule.items() if k != 'version'} == {k: v for k, v in previous.items() if k != 'version'}:
                        rule = make_rule(family, entity, previous['version'] + 1, rng)
                    active[entity] = rule
                    archive.append(rule)
                    changed.add(entity)
        tasks = []
        for entity, family in entities:
            rule = active[entity]
            raw = rng.randrange(100, 10000)
            payload = {f'{k}{v}': rng.randrange(1, 10000) for k in ['f', 'h'] for v in [1, 2, 3]}
            probe = dict(raw=raw, payload=payload)
            available = not (rnd in (6, 7) and rng.random() < 0.25)
            tasks.append(dict(seed=seed, round=rnd, entity=entity, family=family, version=rule['version'],
                              raw=raw, payload=payload, source_available=available,
                              drift_now=entity in changed, reference=solve(rule, probe)))
        rng.shuffle(tasks)
        stream.append((rnd, tasks))
    return entities, archive, stream


def load_published_fixtures(path):
    """Published task stream, used only as an independent replication check."""
    if not path.exists():
        return {}
    return {(r['seed'], r['round'], r['entity']): r for r in json.loads(path.read_text(encoding='utf-8'))}

def load_published_arm(path, method):
    """Published per-task rows for one condition, the target of the row level check."""
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding='utf-8'))
    return {(r['seed'], r['round'], r['entity']): r for r in rows if r['method'] == method}

def replication_rows(rows, published_arm, method):
    """Row level check: the replayed arm must reproduce the published condition exactly."""
    if not published_arm:
        return []
    report = []
    for seed in sorted({r['seed'] for r in rows if r['method'] == method}):
        mine = {(r['seed'], r['round'], r['entity']): r
                for r in rows if r['method'] == method and r['seed'] == seed}
        checked = mismatches = 0
        for key, want in published_arm.items():
            if key[0] != seed or key not in mine:
                continue
            checked += 1
            got = mine[key]
            if any(bool(got[field]) != bool(want[field]) for field in ('correct', 'memory_hit')):
                mismatches += 1
            elif got['source_reads'] != want['source_reads'] or got['writes'] != want['writes']:
                mismatches += 1
        report.append(dict(seed=seed, checked=checked, mismatches=mismatches,
                           published_rows=sum(1 for k in published_arm if k[0] == seed)))
    return report


class Runner:
    def __init__(self, home, name, budget=2048):
        self.home, self.name, self.budget = home, name, budget
        self.svc = None
        self.open()

    def open(self):
        self.svc = ButianService(self.home)
        if not self.svc.store.db.execute("SELECT 1 FROM members WHERE member_id='host'").fetchone():
            self.svc.add_member('host', 'Same deterministic host resolver')

    def close(self):
        self.svc.close()

    def restart(self):
        self.close()
        self.open()

    def context(self, entity):
        return self.svc.compose_context('host', f'resolve {entity}', budgets={'L3': self.budget})

    def status(self, memory_id):
        mem = self.svc.memory('host')
        try:
            row = mem.get(memory_id)
            return None if row is None else row['status']
        finally:
            mem.close()

    def remember_direct(self, rule):
        """Published-benchmark path: a source rule becomes warm without independent practice."""
        event = self.svc.store.append_event('host', 'evidence_recorded',
                                           {'rule': rule, 'synthetic': True},
                                           f"direct-{rule['entity']}-v{rule['version']}")
        mem = self.svc.memory('host')
        try:
            mem.put(rule['entity'], 'lesson', 'RULE ' + json.dumps(rule, sort_keys=True),
                    status='warm', confidence=1.0, source_event_ids=[event],
                    change_reason='verified_source_direct')
        finally:
            mem.close()

    def align(self, rule, content):
        """A practice observation is about the rule that was actually observed.

        If the stored memory still holds different content (a claim the gate just
        rejected, for instance), the host first records the verified rule and then
        the observation. Status is carried over, so this is a content rewrite and
        not a promotion; practice counters live in their own table and are kept.
        """
        mem = self.svc.memory('host')
        try:
            row = mem.get(rule['entity'])
            if row is None or row['content'] == content:
                return False
            mem.put(rule['entity'], 'lesson', content, status=row['status'],
                    confidence=row['confidence'], weight=row['weight'],
                    environment=json.loads(row['environment']),
                    source_event_ids=json.loads(row['source_event_ids']),
                    change_reason='verified_source_rewrite')
            return True
        finally:
            mem.close()

    def practice(self, rule, label, outcome='success'):
        """One independent practice observation, with its own evidence document."""
        content = 'RULE ' + json.dumps(rule, sort_keys=True)
        self.align(rule, content)
        path = self.home / 'practice-evidence' / f"{rule['entity']}-{label}-{outcome}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"entity={rule['entity']}; label={label}; outcome={outcome}; "
                        f"rule={json.dumps(rule, sort_keys=True)}", encoding='utf-8')
        evidence = self.svc.record_evidence(path, owner_member_id='host', actor_member_id='host')
        return self.svc.record_practice_outcome('host', memory_id=rule['entity'], kind='lesson',
                                                content=content,
                                                outcome=outcome, independent_key=label,
                                                evidence_ref=evidence)

    def revise(self, rule):
        mem = self.svc.memory('host')
        try:
            mem.put(rule['entity'], 'lesson', 'RULE ' + json.dumps(rule, sort_keys=True),
                    status='candidate', change_reason='counterexample_revision')
        finally:
            mem.close()


def stale_rule(archive, entity):
    """Pre-drift content carrying the current version label: wrong, but version-consistent."""
    history = [d for d in archive if d['entity'] == entity]
    current = max(d['version'] for d in history)
    stale = [d for d in history if d['version'] < current]
    if not stale:
        return None
    wrong = dict(stale[-1])
    wrong['version'] = current
    return wrong


def main_study(out, seeds, rounds, entities_per_family, published):
    rows, audits, injections, replication = [], [], [], []
    work = out / 'production'
    for seed in seeds:
        entities, archive, stream = build_stream(seed, rounds, entities_per_family)
        if published:
            mismatches = 0
            checked = 0
            for rnd, tasks in stream:
                for task in tasks:
                    want = published.get((seed, rnd, task['entity']))
                    if not want:
                        continue
                    checked += 1
                    if (want['version'] != task['version'] or want['source_available'] != task['source_available']
                            or want['reference'] != task['reference'] or want['payload'] != task['payload']):
                        mismatches += 1
            replication.append(dict(seed=seed, checked=checked, mismatches=mismatches))
        drifted = {rnd: {t['entity'] for t in tasks if t['drift_now']} for rnd, tasks in stream}
        runners = {m: Runner(work / str(seed) / m, m) for m in METHODS if m != 'G0-index'}
        try:
            for rnd, tasks in stream:
                if rnd in INJECT_ROUNDS:
                    targets = sorted(drifted.get(rnd - 1, set()))
                    for method in sorted(INJECT_ARMS):
                        runner = runners[method]
                        for entity in targets:
                            wrong = stale_rule(archive, entity)
                            if wrong is None:
                                continue
                            if method == 'G5-inject-warm':
                                runner.remember_direct(wrong)
                                for index in ('a', 'b'):
                                    try:
                                        runner.practice(wrong, f'inject-{rnd}-{index}')
                                    except ValueError as exc:
                                        injections.append(dict(seed=seed, round=rnd, entity=entity,
                                                               method=method, rejected=str(exc)))
                            else:
                                mem = runner.svc.memory('host')
                                try:
                                    mem.put(entity, 'lesson', 'RULE ' + json.dumps(wrong, sort_keys=True),
                                            status='candidate', change_reason='injected_wrong_claim')
                                finally:
                                    mem.close()
                            injections.append(dict(seed=seed, round=rnd, entity=entity, method=method,
                                                   claimed_version=wrong['version']))
                if rnd == 8:
                    for runner in runners.values():
                        runner.restart()
                for task in tasks:
                    entity, version, reference = task['entity'], task['version'], task['reference']
                    for method in METHODS:
                        start = time.perf_counter()
                        source_reads = writes = status_checks = gate_rejections = 0
                        hit = False
                        chosen = None
                        if method != 'G0-index':
                            runner = runners[method]
                            ctx, report = runner.context(entity)
                            recalled = parse_context(ctx, entity, version)
                            if recalled is not None:
                                status = runner.status(entity)
                                status_checks = 1
                                allowed = REUSABLE_STATUSES | ({'candidate', 'pending_validation'}
                                                               if method in CANDIDATE_REUSABLE else set())
                                if status in allowed:
                                    chosen = recalled
                                    hit = True
                                else:
                                    gate_rejections = 1
                        fetched = False
                        if chosen is None and task['source_available']:
                            matches = [d for d in archive if d['entity'] == entity and d['version'] == version]
                            source_reads = 1
                            chosen = matches[0] if len(matches) == 1 else None
                            fetched = chosen is not None
                        answer = solve(chosen, task)
                        correct = answer is not None and answer == reference
                        unknown = answer is None
                        wrong = not unknown and not correct
                        corrected = False
                        status_after_failure = None
                        if wrong and task['source_available'] and method != 'G0-index':
                            runner = runners[method]
                            truth = [d for d in archive if d['entity'] == entity and d['version'] == version][0]
                            source_reads += 1
                            corrected = True
                            observed = runner.practice(truth, f'r{rnd}-counterexample', outcome='failure')
                            status_after_failure = observed['status']
                            runner.revise(truth)
                            writes += 2
                        elif fetched and method != 'G0-index':
                            runner = runners[method]
                            if method == 'G1-direct':
                                runner.remember_direct(chosen)
                            else:
                                runner.practice(chosen, f'r{rnd}-success')
                            writes += 1
                        rows.append(dict(task, method=method, answer=answer, correct=correct, wrong=wrong,
                                         unknown=unknown, memory_hit=hit, feedback_corrected=corrected,
                                         status_after_failure=status_after_failure,
                                         source_reads=source_reads, status_checks=status_checks,
                                         gate_rejections=gate_rejections, writes=writes,
                                         elapsed_s=time.perf_counter() - start))
            for method, runner in runners.items():
                mem = runner.svc.memory('host')
                try:
                    for entity, family in entities:
                        row = mem.get(entity)
                        if row:
                            audits.append(dict(seed=seed, method=method, entity=entity,
                                               status=row['status'], content=row['content']))
                finally:
                    mem.close()
        finally:
            for runner in runners.values():
                runner.close()
    return rows, audits, injections, replication


def aggregate(rows):
    groups = {}
    for method in METHODS:
        rr = [r for r in rows if r['method'] == method]
        n = max(1, len(rr))
        groups[method] = dict(tasks=len(rr), correct=sum(r['correct'] for r in rr), wrong=sum(r['wrong'] for r in rr),
                              unknown=sum(r['unknown'] for r in rr),
                              accuracy=sum(r['correct'] for r in rr) / n,
                              coverage=sum(not r['unknown'] for r in rr) / n,
                              source_reads=sum(r['source_reads'] for r in rr),
                              writes=sum(r['writes'] for r in rr),
                              status_checks=sum(r['status_checks'] for r in rr),
                              gate_rejections=sum(r['gate_rejections'] for r in rr),
                              memory_hits=sum(r['memory_hit'] for r in rr),
                              mean_ms=1000 * sum(r['elapsed_s'] for r in rr) / n)
    return groups


def paired(rows):
    """Seed is the resampling unit; turns inside a seed are correlated."""
    rng = random.Random(20261102)
    result = {}
    seeds = sorted({r['seed'] for r in rows})
    for method in METHODS:
        if method == 'G0-index':
            continue
        per_seed = {}
        for seed in seeds:
            rr = [r for r in rows if r['seed'] == seed and r['method'] == method]
            bb = [r for r in rows if r['seed'] == seed and r['method'] == 'G0-index']
            dd = [r for r in rows if r['seed'] == seed and r['method'] == 'G1-direct']
            per_seed[seed] = dict(
                accuracy_delta_vs_index=sum(x['correct'] for x in rr) / len(rr) - sum(x['correct'] for x in bb) / len(bb),
                accuracy_delta_vs_direct=sum(x['correct'] for x in rr) / len(rr) - sum(x['correct'] for x in dd) / len(dd),
                read_delta_vs_index=sum(x['source_reads'] for x in rr) - sum(x['source_reads'] for x in bb),
                read_delta_vs_direct=sum(x['source_reads'] for x in rr) - sum(x['source_reads'] for x in dd),
                wrong_total=sum(x['wrong'] for x in rr))
        out = {}
        for metric in ('accuracy_delta_vs_index', 'accuracy_delta_vs_direct',
                       'read_delta_vs_index', 'read_delta_vs_direct'):
            values = [v[metric] for v in per_seed.values()]
            draws = sorted(sum(rng.choice(values) for _ in values) / len(values) for _ in range(10000))
            out[metric] = {'mean': sum(values) / len(values), 'seed_values': values,
                           'cluster_bootstrap_95': [draws[249], draws[9749]]}
        result[method] = {'paired': out,
                          'wrong_total': sum(v['wrong_total'] for v in per_seed.values()),
                          'wrong_seeds': sum(1 for v in per_seed.values() if v['wrong_total'] > 0)}
    return result


def round_table(rows):
    table = {}
    for method in METHODS:
        for rnd in sorted({r['round'] for r in rows}):
            rr = [r for r in rows if r['method'] == method and r['round'] == rnd]
            if not rr:
                continue
            table.setdefault(method, {})[rnd] = dict(tasks=len(rr), correct=sum(r['correct'] for r in rr),
                                                     wrong=sum(r['wrong'] for r in rr),
                                                     unknown=sum(r['unknown'] for r in rr),
                                                     source_reads=sum(r['source_reads'] for r in rr))
    return table


def injected_damage(rows, injections):
    """How many wrong first answers landed on each injected claim, and in which round."""
    events = [item for item in injections if 'rejected' not in item]
    report = []
    for item in events:
        hit = [r for r in rows if r['wrong'] and r['seed'] == item['seed'] and r['method'] == item['method']
               and r['entity'] == item['entity']]
        report.append(dict(item, wrong_first_answers=len(hit),
                           rounds=sorted({r['round'] for r in hit})))
    return report


def lifecycle_probe():
    """Direct check of the automatic path: can a disputed memory be re-verified?"""
    import tempfile
    root = Path(tempfile.mkdtemp(prefix='butian-gate-probe-'))
    service = ButianService(root)
    trace = []
    try:
        service.add_member('probe', 'Gate probe')
        rule = dict(family='calibration', entity='probe00', version=1, scale=250, offset=7)
        content = 'RULE ' + json.dumps(rule, sort_keys=True)
        mem = service.memory('probe')
        mem.put('probe00', 'lesson', content, status='candidate', change_reason='probe_start')
        trace.append(['put_candidate', mem.get('probe00')['status']])
        mem.close()

        def observe(label, outcome):
            path = root / 'evidence' / f'{label}.txt'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'{label}:{outcome}:{content}', encoding='utf-8')
            proof = service.record_evidence(path, owner_member_id='probe', actor_member_id='probe')
            result = service.record_practice_outcome('probe', memory_id='probe00', kind='lesson', content=content,
                                                     outcome=outcome, independent_key=label, evidence_ref=proof)
            trace.append([label, outcome, result['status'], result['successes'], result['failures']])

        observe('success-1', 'success')
        observe('success-2', 'success')
        observe('failure-1', 'failure')
        observe('success-3', 'success')
        observe('success-4', 'success')
        mem = service.memory('probe')
        mem.put('probe00', 'lesson', content, status='candidate', change_reason='explicit_revision')
        trace.append(['explicit_revision', mem.get('probe00')['status']])
        mem.close()
    finally:
        service.close()
    last = {step[0]: step for step in trace}
    return dict(steps=trace,
                automatic_rewarm_after_failure=last.get('success-4', [None, None, None])[2],
                explicit_revision_restores_candidate=last.get('explicit_revision', [None, None])[1],
                automatic_path_closes_loop=last.get('success-4', [None, None, None])[2] == 'warm')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--seeds', default='report', choices=['report', 'pilot'])
    parser.add_argument('--seed-list', default='', help='comma separated explicit seeds, overrides --seeds')
    parser.add_argument('--rounds', type=int, default=12)
    parser.add_argument('--entities-per-family', type=int, default=4)
    parser.add_argument('--published-fixtures',
                        default='docs/论文/experiment_results/multiround-ieee-20261015-r2/task-stream.json')
    parser.add_argument('--published-rows',
                        default='docs/论文/experiment_results/multiround-ieee-20261015-r2/rows.json')
    parser.add_argument('--published-arm', default='B2-full')
    args = parser.parse_args()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    if args.seed_list:
        seeds = [int(item) for item in args.seed_list.split(',') if item.strip()]
    else:
        seeds = REPORT_SEEDS if args.seeds == 'report' else PILOT_SEEDS
    published = load_published_fixtures(ROOT / args.published_fixtures)
    published_arm = load_published_arm(ROOT / args.published_rows, args.published_arm)
    dump(out / 'protocol.json', dict(protocol_version=1, methods=METHODS, seeds=seeds, rounds=args.rounds,
                                     entities_per_family=args.entities_per_family,
                                     inject_rounds=list(INJECT_ROUNDS),
                                     reusable_statuses=sorted(REUSABLE_STATUSES),
                                     candidate_reusable=sorted(CANDIDATE_REUSABLE),
                                     bootstrap_draws=10000, written_before_execution=True))
    rows, audits, injections, replication = main_study(out, seeds, args.rounds, args.entities_per_family, published)
    dump(out / 'rows.json', rows)
    dump(out / 'memory-audit.json', audits)
    dump(out / 'injections.json', injections)
    dump(out / 'damage.json', injected_damage(rows, injections))
    dump(out / 'lifecycle-probe.json', lifecycle_probe())
    dump(out / 'replication-check.json', dict(stream=replication,
                                              row_level=replication_rows(rows, published_arm, 'G1-direct'),
                                              compared_arm=args.published_arm))
    dump(out / 'summary.json', dict(execution_kind='end_to_end_gate_benchmark', llm_inference_calls=0,
                                    model_weights_changed=False, seeds=seeds, rounds=args.rounds,
                                    methods=METHODS, summary=aggregate(rows), paired=paired(rows),
                                    round_table=round_table(rows),
                                    limitations=['Synthetic contracts; the wrong claim is an experimenter shock, not an observed host error',
                                                 'Turns inside a seed are correlated; the seed is the statistical unit',
                                                 'No LLM token, dollar or model-weight measurement',
                                                 'Normal restart at round 8, not crash or power-loss recovery']))
    dump(out / 'environment.json', dict(python=platform.python_version(), platform=platform.platform(),
                                        base_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                                        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
    dump(out / 'manifest.json', {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out.glob('*.json')})
    print(json.dumps(aggregate(rows), indent=2), flush=True)
    print('COMPLETE', out, flush=True)


if __name__ == '__main__':
    main()
