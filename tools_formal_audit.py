from itertools import product
from math import log1p, exp

# Requirement-level memory lifecycle graph extracted from docs.
mem = {
    'candidate': {'warm', 'archived', 'disputed'},
    'warm': {'hot', 'cold', 'disputed', 'superseded', 'invalidated', 'archived'},
    'hot': {'warm', 'cold', 'disputed', 'superseded', 'invalidated', 'archived'},
    'cold': {'warm', 'candidate', 'disputed', 'superseded', 'invalidated', 'archived'},
    'disputed': {'candidate', 'warm', 'cold', 'invalidated', 'archived'},
    'superseded': {'archived'},
    'invalidated': {'archived'},
    'archived': set(),
}

task = {
    'todo': {'ready', 'cancelled'}, 'ready': {'claimed', 'blocked', 'cancelled'},
    'claimed': {'running', 'ready', 'cancelled'},
    'running': {'review', 'blocked', 'failed', 'cancelling', 'waiting_user', 'provider_lost', 'unknown'},
    'review': {'done', 'ready', 'failed', 'cancelled'},
    'waiting_user': {'ready', 'running', 'cancelled', 'unknown'},
    'cancelling': {'cancelled', 'provider_lost', 'unknown'},
    'blocked': {'ready', 'cancelled', 'failed'}, 'failed': {'ready', 'cancelled'},
    'provider_lost': {'ready', 'cancelled', 'unknown'}, 'unknown': {'ready', 'cancelled'},
    'done': set(), 'cancelled': set(),
}
peer = {'asked': {'candidate'}, 'candidate': {'verified', 'rejected', 'inconclusive'}, 'verified': set(), 'rejected': set(), 'inconclusive': {'candidate', 'verified', 'rejected'}}

def reachable(graph, start):
    seen, todo = {start}, [start]
    while todo:
        x = todo.pop()
        for y in graph[x] - seen:
            seen.add(y); todo.append(y)
    return seen

def cycles(graph):
    return {x for x in graph if x in set().union(*(reachable(graph, y) for y in graph[x]))}

print('MEMORY_REACHABLE_FROM_CANDIDATE', sorted(reachable(mem, 'candidate')))
print('MEMORY_TERMINAL', sorted(x for x, ys in mem.items() if not ys))
print('MEMORY_CYCLES', sorted(cycles(mem)))
print('TASK_TERMINAL', sorted(x for x, ys in task.items() if not ys))
print('TASK_DEAD_ENDS_FROM_TODO', sorted(set(task) - reachable(task, 'todo')))
print('PEER_REACHABLE', sorted(reachable(peer, 'asked')))
print('PEER_TERMINAL', sorted(x for x, ys in peer.items() if not ys))
print('PEER_CYCLES', sorted(cycles(peer)))

# Check monotonicity of confidence and weight under stated candidate->warm policy.
for success_count in (0, 1, 2, 10, 100, 10000):
    weight = 1.0 + 1.0 * log1p(success_count)
    print('LOG_WEIGHT', success_count, round(weight, 8))
print('HALF_LIFE_DECAY', [(age, round(2 ** (-age / 30), 8)) for age in (0, 30, 60, 300)])

# Counterexample search for an under-specified evidence gate:
# if one success is sufficient for warm, then n=1 passes despite docs saying a single success is insufficient.
print('SINGLE_SUCCESS_POLICY', {'attempts': 1, 'successes': 1, 'required_repeats': 2, 'expected_status': 'candidate'})

# Experience evolution model audit.
from butian.core.experience import SkillPhase, SkillState, phase_curve, transition_phase, composition_gain, decision_value
phases = list(SkillPhase)
for phase in phases:
    samples = [phase_curve(i / 100, phase) for i in range(101)]
    assert samples[0] == 0 and abs(samples[-1] - 1) < 1e-12
    assert all(0 <= x <= 1 for x in samples)
    assert all(a <= b for a, b in zip(samples, samples[1:]))
for state_phase in phases:
    state = SkillState(state_phase, .7)
    for contradiction in (False, True):
        for novel in (False, True):
            for successes in range(3):
                next_state = transition_phase(state, marginal=.01, contradiction=contradiction, novel_environment=novel, independent_successes=successes, composition_successes=successes)
                assert next_state.phase in phases
                assert next_state.activation_count >= state.activation_count
assert composition_gain([.4, .3]) == composition_gain([.4, .3])
assert composition_gain([.4, .3]) != composition_gain([.4, .3, .3])
for phase in phases:
    assert 0 <= decision_value(1, 1, 1, 1, 1, phase) <= 1
print('EXPERIENCE_CURVE_INVARIANTS PASS')
print('EXPERIENCE_PHASES', [p.value for p in phases])
print('COMPOSITION_DUPLICATE_POLICY PASS')
