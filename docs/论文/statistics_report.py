"""Offline research statistics. SciPy is research-only, not a runtime dependency."""
import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist


def wilson(successes, trials):
    if isinstance(successes, bool) or isinstance(trials, bool) or not isinstance(successes, int) or not isinstance(trials, int) or not 0 <= successes <= trials:
        raise ValueError('invalid_binomial_counts')
    if trials == 0:
        return {'successes': successes, 'trials': 0, 'rate': None, 'ci95': None, 'method': 'wilson'}
    z = NormalDist().inv_cdf(0.975)
    p = successes / trials
    denominator = 1 + z*z/trials
    center = (p + z*z/(2*trials)) / denominator
    margin = z * math.sqrt(p*(1-p)/trials + z*z/(4*trials*trials)) / denominator
    return {'successes': successes, 'trials': trials, 'rate': p, 'ci95': [max(0, center-margin), min(1, center+margin)], 'method': 'wilson'}


def continuous(values):
    present = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)]
    result = {'n': len(present), 'missing': len(values)-len(present), 'mean': statistics.fmean(present) if present else None, 'stddev': None, 'ci95': None, 'method': 'student_t'}
    if len(present) < 2:
        return result
    from scipy.stats import t
    result['stddev'] = statistics.stdev(present)
    result['df'] = len(present)-1
    result['critical_value'] = float(t.ppf(0.975, result['df']))
    margin = result['critical_value'] * result['stddev'] / math.sqrt(len(present))
    result['ci95'] = [result['mean']-margin, result['mean']+margin]
    return result


def report(rows):
    groups = defaultdict(list)
    seen = set()
    for row in rows:
        key = (row['case_id'], row['seed'], row['repeat'], row['variant'], row['provider'], row.get('model_version'), row['execution_kind'])
        if key in seen:
            raise ValueError('duplicate_observation')
        seen.add(key)
        groups[(row['execution_kind'], row['provider'], row.get('model_version'), row['category'], row['variant'])].append(row)
    output = []
    for key, items in sorted(groups.items(), key=lambda entry: str(entry[0])):
        rates = {}
        metric_names = sorted({name for item in items for name in item.get('rates', {})})
        for name in metric_names:
            counts = [item['rates'][name] for item in items if name in item.get('rates', {})]
            rates[name] = wilson(sum(c['successes'] for c in counts), sum(c['trials'] for c in counts))
        measures = sorted({name for item in items for name in item.get('measures', {})})
        output.append({'stratum': dict(zip(('execution_kind', 'provider', 'model_version', 'category', 'variant'), key)), 'observations': len(items), 'rates': rates, 'measures': {name: continuous([item.get('measures', {}).get(name) for item in items]) for name in measures}})
    return {'strata': output, 'claim_scope': 'recorded cases and providers only; mechanism_control is not model efficacy', 'independence_assumption': 'Verify independent execution contexts before interpreting confidence intervals.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.input.read_text(encoding='utf-8').splitlines() if line.strip()]
    result = report(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Never silently overwrite prior research results.
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
