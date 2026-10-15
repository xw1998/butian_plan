"""JSON-in/JSON-out helpers. No network, file access or subprocess execution."""
import hashlib
import json
import math
import statistics
import sys


def run(data):
    operation = data.get('operation')
    if operation == 'statistics':
        values = data['values']
        if not values or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in values):
            raise ValueError('finite_numeric_samples_required')
        return {'count': len(values), 'mean': statistics.mean(values), 'minimum': min(values), 'maximum': max(values), 'sample_stdev': statistics.stdev(values) if len(values) > 1 else None}
    if operation == 'uncertainty':
        values = data['standard_contributions']
        k = data.get('k', 2)
        if not values or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0 for x in values) or isinstance(k, bool) or not isinstance(k, (int, float)) or not math.isfinite(k) or k <= 0:
            raise ValueError('nonnegative_finite_contributions_and_positive_k_required')
        if data.get('independent') is not True:
            raise ValueError('independence_confirmation_required')
        u = math.hypot(*values)
        return {'standard_uncertainty': u, 'expanded_uncertainty': k*u, 'k': k, 'assumption': 'independent contributions; no coverage probability inferred'}
    if operation == 'checklist':
        checks = data['checks']
        if not isinstance(checks, dict) or not checks or any(type(v) is not bool for v in checks.values()):
            raise ValueError('nonempty_boolean_checklist_required')
        return {'complete': all(checks.values()), 'missing': [key for key, value in checks.items() if not value], 'basis': 'caller supplied assertions; not independent verification'}
    if operation == 'fingerprint':
        return {'sha256': hashlib.sha256(data['text'].encode('utf-8')).hexdigest()}
    raise ValueError('unsupported_operation')


if __name__ == '__main__':
    try:
        print(json.dumps({'ok': True, 'result': run(json.load(sys.stdin))}, ensure_ascii=False, allow_nan=False))
    except (ValueError, KeyError, TypeError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False))
        sys.exit(1)
