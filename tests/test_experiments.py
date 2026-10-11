import tempfile
from pathlib import Path

from butian.runtime.experiments import ExperimentStore, environment_fingerprint, stable_hash


def test_batch_variants_share_task_package_and_compare_multiple_metrics():
    with tempfile.TemporaryDirectory() as d:
        store = ExperimentStore(Path(d) / 'batch.db')
        try:
            package = {'tasks': ['repeat', 'transfer']}
            batch = store.run_batch('B0-B2', [
                {'name': 'B0', 'provider_version': 'model-x@1'},
                {'name': 'B1', 'provider_version': 'model-x@1', 'configuration': {'memory': 'retrieval'}},
                {'name': 'B2', 'provider_version': 'model-x@1', 'configuration': {'memory': 'full'}},
            ], task_package=package, dataset_version='ds-v1', code_commit='abc', configuration={'seed': 11}, repeats=2)
            assert len(batch['experiment_ids']) == 6
            assert len({store.get(i)['task_package_hash'] for i in batch['experiment_ids']}) == 1
            for index, experiment_id in enumerate(batch['experiment_ids']):
                store.add_event(experiment_id, 'metric', {'success': index / 5, 'latency': 10 + index})
            result = store.batch_compare(batch['experiment_ids'], ['success', 'latency'])
            assert set(result['metrics']) == {'success', 'latency'}
            assert [x['summary']['sample_count'] for x in result['metrics']['success']] == [1] * 6
            assert result['matching']['task_package_hash'] == batch['task_package_hash']
            mismatched = store.create('other', task_package={'different': True}, dataset_version='ds-v1', code_commit='abc', configuration={'seed': 11, 'variant': 'other'}, provider_version='model-x@1')
            try:
                store.batch_compare([batch['experiment_ids'][0], mismatched], ['success'])
            except ValueError as exc:
                assert str(exc).startswith('experiment_match_mismatch:')
            else:
                raise AssertionError('mismatched task packages accepted')
        finally:
            store.close()

def test_experiment_reproducibility_and_summary():
    assert stable_hash({'b': 2, 'a': 1}) == stable_hash({'a': 1, 'b': 2})
    assert environment_fingerprint({'toolchain': 'test'})
    with tempfile.TemporaryDirectory() as d:
        store = ExperimentStore(Path(d) / 'experiments.db')
        try:
            experiment_id = store.create('B0', task_package={'steps': ['a', 'b']}, dataset_version='ds-1', code_commit='abc123', configuration={'seed': 7}, provider_version='manual-1')
            for value in (1.0, 2.0, 3.0):
                store.add_event(experiment_id, 'metric', {'latency': value})
            store.add_event(experiment_id, 'context', {'retrieved': True})
            store.add_event(experiment_id, 'context', {'retrieved': False})
            store.add_event(experiment_id, 'failure', {'case_id': 'case-3', 'reason': 'timeout'})
            comparison = store.compare([experiment_id], 'latency')
            assert comparison[0]['failures'][0]['case_id'] == 'case-3'
            assert comparison[0]['context_black_hole']['loss_rate'] == 0.5
            summary = store.summarize(experiment_id, 'latency')
            assert summary['sample_count'] == 3 and summary['mean'] == 2.0
            assert summary['ci95'][0] < 2.0 < summary['ci95'][1]
            assert store.context_black_hole(experiment_id)['loss_rate'] == 0.5
            assert store.get(experiment_id)['dataset_version'] == 'ds-1'
        finally:
            store.close()
