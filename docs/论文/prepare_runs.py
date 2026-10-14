"""Prepare fresh model input bundles; this script never performs model inference.

Inputs and scoring references occupy separate directories. This is an input
boundary, not OS isolation: the host must enforce the declared read allowlist.
"""
import argparse
import hashlib
import json
import platform
import subprocess
from pathlib import Path


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write('\n')


def prepare(root, output):
    root = Path(root).resolve()
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('research_output_already_exists')
    package_path = root / 'docs/论文/任务包.v1.json'
    package_bytes = package_path.read_bytes()
    package = json.loads(package_bytes)
    snapshot = root / 'docs/论文/fixtures/nist-eda352-20261014.html'
    snapshot_bytes = snapshot.read_bytes()
    snapshot_hash = sha256(snapshot_bytes)
    if snapshot_hash != 'a8b0dc8fb3a64f98368b2c75246b58e5fb53f42c4ef0a4b09b184f31aeed0725':
        raise ValueError('research_source_snapshot_changed')
    files = {}
    for directory in ('butian', 'web'):
        for path in sorted((root / directory).rglob('*')):
            if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts and path.suffix in {'.py', '.js', '.html', '.css', '.json'}:
                files[path.relative_to(root).as_posix()] = sha256(path.read_bytes())
    files['pyproject.toml'] = sha256((root / 'pyproject.toml').read_bytes())
    fingerprint = sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode())
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    output.mkdir(parents=True)
    manifest = {'dataset_version': package['dataset_version'], 'task_package_sha256': sha256(package_bytes), 'code_commit': commit, 'working_tree_sha256': fingerprint, 'source_files': files, 'source_snapshot_sha256': snapshot_hash, 'python': platform.python_version(), 'platform': platform.platform(), 'execution_status': 'prepared_not_executed', 'model_calls': None, 'cost': None, 'runs': []}
    # Independent scoring material is never embedded in the model input bundle.
    references = {
        'repeat-pressure-v1': {'gauge_pressure_pa': 3400},
        'transfer-pressure-v1': {'gauge_pressure_pa': 1700, 'gauge_pressure_kpa': 1.7},
        'conflict-scale-v1': {'current_value': 0.34, 'firmware': '2.0', 'unknown_probe': 'unknown'},
        'source-practice-v1': {'mean': 3.0, 'sample_stddev': 1.5811388300841898, 'df': 4, 'ci95': [1.036756838522439, 4.9632431614775605], 'single_sample_ci95': None},
        'recovery-context-v1': {'value': 2.3, 'unit': 'm/s', 'requires_current_revision': True, 'requires_confirmed_stop': True},
    }
    for case in package['cases']:
        write_json(output / 'scoring' / (case['case_id'] + '.json'), references[case['case_id']])
        for seed in package['seeds']:
            offset = package['seeds'].index(seed) % len(package['variants'])
            ordered_variants = package['variants'][offset:] + package['variants'][:offset]
            for variant in ordered_variants:
                run_id = f"{case['case_id']}-{seed}-{variant}-r1"
                run_dir = output / 'inputs' / run_id
                model_case = {key: value for key, value in case.items() if key != 'training'}
                payload = {'run_id': run_id, 'case': model_case, 'seed': seed, 'repeat': 1, 'variant': variant, 'max_model_calls': package['max_model_calls'], 'max_tool_calls': package['max_tool_calls'], 'allowed_tools': package['allowed_tools'], 'permissions': package['permissions'], 'memory_mode': {'B0': 'none', 'B1': 'fixed_retrieval_only', 'B2': 'production_lifecycle'}[variant], 'training_fixture': case.get('training') if variant != 'B0' else None, 'required_host_context': 'same_host_agent', 'comparison_design': 'within_agent_repeated_measures', 'context_carryover': 'must_record_not_assumed_reset', 'order_policy': 'counterbalanced_by_seed', 'condition_order': ordered_variants, 'condition_position': ordered_variants.index(variant) + 1, 'model_seed_controlled': False, 'prior_answer_exposure': 'must_record', 'source_snapshot_sha256': snapshot_hash if 'source' in case else None}
                write_json(run_dir / 'input.json', payload)
                if 'source' in case:
                    (run_dir / 'source.html').write_bytes(snapshot_bytes)
                input_hash = sha256((run_dir / 'input.json').read_bytes())
                manifest['runs'].append({'run_id': run_id, 'input_path': (run_dir / 'input.json').relative_to(output).as_posix(), 'input_sha256': input_hash, 'read_allowlist': [run_dir.relative_to(output).as_posix()], 'status': 'pending', 'model_context_id': None})
    write_json(output / 'manifest.json', manifest)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.root, args.output)
    print(json.dumps({'runs_prepared': len(result['runs']), 'working_tree_sha256': result['working_tree_sha256'], 'execution_status': result['execution_status']}))
