"""Run the complete test suite and current executor acceptance in an operator terminal."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def sdk_environment(environment, backend):
    selected = dict(environment)
    selected['BUTIAN_EXECUTOR_CONNECTION'] = str(backend.connection_path)
    return selected


def preflight():
    if sys.version_info < (3, 11):
        return 'PythonVersionUnsupported', 'Python >=3.11 is required; selected: ' + sys.executable
    if importlib.util.find_spec('pytest') is None:
        return 'PytestUnavailable', 'pytest is required in the selected interpreter: ' + sys.executable
    return None

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operator-confirm', action='store_true')
    args = parser.parse_args()
    if not args.operator_confirm or os.name != 'nt':
        parser.error('Run from an independent Windows terminal with --operator-confirm')
    folder = ROOT / '.agent_tmp' / ('release-operator-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    def hashes():
        return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for base in ('butian', 'tests') for p in sorted((ROOT / base).rglob('*'))
                if p.is_file() and p.suffix in {'.py', '.mjs'} and 'node_modules' not in p.parts}
    problem = preflight()
    if problem is not None:
        report = {'status': 'failed', 'source_changed': False, 'checks': [
            {'name': 'environment_preflight', 'exit_code': 1,
             'error_type': problem[0], 'message': problem[1]}]}
        path = folder / 'report.json'
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(problem[1], file=sys.stderr)
        print(json.dumps({'status': 'failed', 'report': str(path)}, ensure_ascii=False))
        return 1
    before = hashes()
    report = {'status': 'incomplete', 'source_hashes': before, 'checks': []}
    environment = dict(os.environ)
    environment['BUTIAN_OPERATOR_ACCEPTANCE'] = '1'
    environment['BUTIAN_TEST_PYTHON'] = str(Path(sys.executable).resolve())
    junit = folder / 'pytest.xml'
    commands = [
        ('full_pytest', [sys.executable, '-m', 'pytest', '-q', '--junitxml=' + str(junit)]),
        ('executor_control', [sys.executable, 'tests/acceptance/executor_operator.py', '--operator-confirm']),
    ]
    commands.append(('tool_business', [sys.executable, 'tests/acceptance/tool_business.py']))
    commands.extend(('official_mcp_' + name, ['node', 'tests/acceptance/official-mcp/' + name + '.mjs'])
                    for name in ('acceptance', 'team', 'source', 'extraction', 'queue', 'dream', 'dream-dispatch', 'workflows'))
    if os.environ.get('BUTIAN_WEB_E2E') == '1':
        # 网页表单的易用性只能在真浏览器里验收，但它需要 playwright + chromium；
        # 默认不跑，发版环境装了浏览器时用 BUTIAN_WEB_E2E=1 把它纳入同一份报告。
        commands.append(('web_ui', [sys.executable, 'tests/acceptance/web_ui.py']))
    backend = None
    try:
        for name, command in commands:
            log = folder / (name + '.log')
            print('Running ' + name + '; log: ' + str(log), flush=True)
            check = {'name': name, 'log': str(log)}
            try:
                command_environment = environment
                if name.startswith('official_mcp_'):
                    if backend is None:
                        from tests.acceptance.operator_backend import OperatorBackend
                        backend = OperatorBackend(folder / 'sdk-executor', operator_confirm=args.operator_confirm)
                    command_environment = sdk_environment(environment, backend)
                with log.open('wb') as output:
                    result = subprocess.run(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, shell=False, env=command_environment)
                check['exit_code'] = result.returncode
                if name == 'full_pytest' and result.returncode == 0:
                    root = ET.parse(junit).getroot()
                    cases = root.findall('.//testcase')
                    incomplete = [case for case in cases if any(case.find(tag) is not None for tag in ('skipped', 'failure', 'error'))]
                    check['test_count'] = len(cases)
                    check['incomplete_count'] = len(incomplete)
                    check['junit'] = str(junit)
                    if not cases or incomplete:
                        check['exit_code'] = 1
                        check['error_type'] = 'IncompleteTestEvidence'
            except (OSError, ET.ParseError, RuntimeError, ValueError, sqlite3.Error) as exc:
                check['exit_code'] = 1
                check['error_type'] = type(exc).__name__
            report['checks'].append(check)
            if check['exit_code']:
                report['status'] = 'failed'
                break
        else:
            report['status'] = 'passed'
    finally:
        if backend is not None:
            try:
                backend.close()
                report['checks'].append({'name': 'sdk_executor_shutdown', 'exit_code': 0})
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                report['checks'].append({'name': 'sdk_executor_shutdown', 'exit_code': 1,
                                         'error_type': type(exc).__name__})
                report['status'] = 'failed'
    report['source_changed'] = hashes() != before
    if report['source_changed']:
        report['status'] = 'failed'
    path = folder / 'report.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': report['status'], 'report': str(path)}, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
