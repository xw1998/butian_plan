import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('research_preparation', ROOT / 'docs/论文/prepare_runs.py')
preparation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preparation)


def test_preparation_separates_references_and_preserves_execution_unknown(tmp_path):
    output = tmp_path / 'prepared'
    manifest = preparation.prepare(ROOT, output)
    assert len(manifest['runs']) == 45
    assert manifest['execution_status'] == 'prepared_not_executed'
    assert manifest['model_calls'] is None
    assert manifest['cost'] is None
    for run in manifest['runs']:
        path = output / run['input_path']
        payload = json.loads(path.read_text(encoding='utf-8'))
        assert preparation.sha256(path.read_bytes()) == run['input_sha256']
        assert run['status'] == 'pending'
        assert run['model_context_id'] is None
        assert payload['required_host_context'] == 'fresh_independent_context'
        assert 'references' not in payload
        assert all('scoring' not in item for item in run['read_allowlist'])
        if payload['variant'] == 'B0':
            assert payload['training_fixture'] is None
    assert len(list((output / 'scoring').glob('*.json'))) == 5
    before = (output / 'manifest.json').read_bytes()
    with pytest.raises(ValueError, match='research_output_already_exists'):
        preparation.prepare(ROOT, output)
    assert (output / 'manifest.json').read_bytes() == before


def test_scoring_statistical_reference_matches_independent_distribution(tmp_path):
    scipy = pytest.importorskip('scipy.stats')
    preparation.prepare(ROOT, tmp_path / 'prepared')
    reference = json.loads((tmp_path / 'prepared/scoring/source-practice-v1.json').read_text(encoding='utf-8'))
    margin = scipy.t.ppf(.975, 4) * reference['sample_stddev'] / (5 ** .5)
    assert reference['ci95'] == pytest.approx([3 - margin, 3 + margin], abs=1e-12)
    assert reference['single_sample_ci95'] is None
