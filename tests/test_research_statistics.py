import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('research_statistics', Path(__file__).resolve().parents[1] / 'docs' / '论文' / 'statistics_report.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_wilson_bounds_and_missing_denominator():
    assert module.wilson(0, 0)['rate'] is None
    assert module.wilson(0, 0)['ci95'] is None
    assert module.wilson(0, 10)['ci95'][1] == pytest.approx(0.2775327999)
    assert module.wilson(10, 10)['ci95'][0] == pytest.approx(0.7224672001)
    with pytest.raises(ValueError):
        module.wilson(True, 1)


def test_student_t_matches_nist_published_example():
    pytest.importorskip('scipy')
    # The published NIST example has n=195, mean=9.261460, s=.022789.
    # A symmetric synthetic vector has the same sufficient statistics.
    import math
    n, mean, sd = 195, 9.261460, .022789
    offset = sd * math.sqrt((n-1)/2)
    values = [mean-offset, mean+offset] + [mean]*(n-2)
    result = module.continuous(values)
    assert result['ci95'] == pytest.approx([9.258242, 9.264679], abs=1e-6)
    assert result['method'] == 'student_t'
    assert result['df'] == 194


def test_missing_single_and_boolean_are_not_zero_uncertainty():
    result = module.continuous([3, None, True])
    assert result['n'] == 1 and result['missing'] == 2
    assert result['stddev'] is None and result['ci95'] is None


def test_research_does_not_pool_mechanism_with_model_or_providers():
    base = dict(case_id='case', seed=1, repeat=1, variant='B2', category='repeat', model_version=None, rates={'success': {'successes': 1, 'trials': 1}}, measures={'cost': None})
    rows = [{**base, 'provider': 'host', 'execution_kind': 'actual_model'}, {**base, 'provider': 'host', 'execution_kind': 'mechanism_control'}, {**base, 'provider': 'other', 'execution_kind': 'actual_model'}]
    result = module.report(rows)
    assert len(result['strata']) == 3
    assert all(group['measures']['cost']['mean'] is None for group in result['strata'])
    with pytest.raises(ValueError, match='duplicate_observation'):
        module.report([rows[0], rows[0]])
