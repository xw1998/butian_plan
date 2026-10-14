"""Run business and official SDK acceptance; security evidence is reported separately."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.acceptance.operator_backend import OperatorBackend


def main():
    folder = ROOT / '.agent_tmp' / ('functional-release-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    env = dict(os.environ)
    env['BUTIAN_OPERATOR_ACCEPTANCE'] = '1'
    env['BUTIAN_TEST_PYTHON'] = sys.executable
    report = {'status': 'incomplete', 'scope': 'business_and_official_sdk', 'checks': [], 'security_acceptance': 'deferred_by_user'}
    backend = None
    try:
        commands = [('tool_business', [sys.executable, 'tests/acceptance/tool_business.py'])]
        commands.extend(('official_mcp_' + name, [os.environ.get('BUTIAN_TEST_NODE', 'node'), 'tests/acceptance/official-mcp/' + name + '.mjs']) for name in ('acceptance', 'team', 'source', 'extraction', 'queue', 'dream', 'dream-dispatch', 'workflows'))
        for name, command in commands:
            if name.startswith('official_mcp_') and backend is None:
                backend = OperatorBackend(folder / 'sdk-executor', operator_confirm=True)
                env['BUTIAN_EXECUTOR_CONNECTION'] = str(backend.connection_path)
            log = folder / (name + '.log')
            print('Running ' + name + '; log: ' + str(log), flush=True)
            with log.open('wb') as output:
                result = subprocess.run(command, cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
            report['checks'].append({'name': name, 'exit_code': result.returncode, 'log': str(log)})
            if result.returncode:
                report['status'] = 'failed'
                break
        else:
            report['status'] = 'passed'
    finally:
        if backend is not None:
            try:
                backend.close()
                report['checks'].append({'name': 'executor_shutdown', 'exit_code': 0})
            except Exception as exc:
                report['status'] = 'failed'
                report['checks'].append({'name': 'executor_shutdown', 'exit_code': 1, 'error_type': type(exc).__name__})
        path = folder / 'report.json'
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'status': report['status'], 'report': str(path)}, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
