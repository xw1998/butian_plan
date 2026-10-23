"""Public, unpractised knowledge assets; never imports a user's runtime database."""
from __future__ import annotations
import hashlib
import json
from importlib.resources import files


def bootstrap_initial_assets(service):
    pending = []
    with service.store._write_lock:
        service.store.db.execute('BEGIN IMMEDIATE')
        try:
            result = _bootstrap_initial_assets_locked(service, pending)
            service.store.db.execute('COMMIT')
        except BaseException:
            service.store.db.execute('ROLLBACK')
            raise
    # Member databases are separate SQLite stores. Publish guides only after
    # their tool registrations commit; a restart safely resumes missing cards.
    # 按成员分组：以前每条资产都 service.memory(member_id) 开一次库再关，
    # 51 条资产就是 51 次 connect + schema 检查；同一成员只开一次即可。
    by_member = {}
    for member_id, memory_id, content, environment in pending:
        by_member.setdefault(member_id, []).append((memory_id, content, environment))
    for member_id, items in by_member.items():
        memory = service.memory(member_id)
        try:
            for memory_id, content, environment in items:
                if memory.get(memory_id):
                    continue
                memory.put(memory_id, 'knowledge', content, confidence=0.0, status='candidate',
                           environment=environment,
                           change_reason='Initial knowledge, not validated practical experience')
                result['memories_created'] += 1
        finally:
            memory.close()
    return result


def _bootstrap_initial_assets_locked(service, pending):
    catalog = json.loads(files('butian.assets').joinpath('catalog.json').read_text(encoding='utf-8'))
    professional = json.loads(files('butian.assets').joinpath('professional_tools.json').read_text(encoding='utf-8'))
    # Validate every executable before making any persistent changes.
    for item in professional:
        content = files('butian.assets').joinpath(item['file']).read_bytes()
        if hashlib.sha256(content).hexdigest() != item['wrapper_sha256']:
            raise ValueError('initial_tool_hash_mismatch:' + item['id'])
    result = {'memories_created': 0, 'tools_created': 0}
    tool_content = files('butian.assets').joinpath('tool.py').read_bytes()
    service.store.db.execute('CREATE TABLE IF NOT EXISTS initial_asset_tools (asset_id TEXT PRIMARY KEY, tool_id TEXT NOT NULL)')
    for member_id, assets in catalog['members'].items():
        asset_id = member_id + ':foundation-tools:v1'
        row = service.store.db.execute('SELECT tool_id FROM initial_asset_tools WHERE asset_id=?', (asset_id,)).fetchone()
        if row is None:
            tool_id = service.propose_tool(member_id, '基础复核工具', '统计、独立分量不确定度、检查清单和文本指纹；未实践验证', reuse_lower=0, manual_cost_upper=0, call_cost_lower=0, build_cost_upper=0, maintenance_cost_upper=0, risk_cost_upper=0)['tool_id']
            manifest = {'input_contract': {'type': 'object', 'required': ['operation'], 'properties': {'operation': {'type': 'string', 'enum': ['statistics', 'uncertainty', 'checklist', 'fingerprint']}}}, 'output_contract': {'type': 'object', 'required': ['ok'], 'properties': {'ok': {'type': 'boolean'}}}, 'permissions': ['execute'], 'failure_conditions': ['invalid input', 'correlated uncertainty contributions', 'unknown operation'], 'entrypoint': ['python', 'tool.py'], 'source_kind': 'internal', 'license': 'MIT', 'dependencies': ['Python >=3.11 standard library'], 'learning_state': 'unpractised'}
            service.add_tool_version(member_id, tool_id, tool_content, manifest)
            service.store.db.execute('INSERT INTO initial_asset_tools VALUES (?,?)', (asset_id, tool_id))
            result['tools_created'] += 1
        else:
            tool_id = row['tool_id']
        assets = list(assets) + [{'id': 'tool-guide-' + member_id, 'content': '基础复核工具 tool_id=' + tool_id + ' version=1。通过宿主调用现有工具执行接口，仍需任务授权及执行后端。stdin JSON：statistics 提供 values 数组；uncertainty 提供 standard_contributions、independent=true、k（默认2）；checklist 提供 checks 名称到布尔值；fingerprint 提供 text。例如 {"operation":"statistics","values":[1,2,3]}。工具保持 draft，未授予自动执行权，未记录实践成功。', 'sources': [{'reference': 'butian.assets/tool.py', 'use': 'original stdlib helper'}]}]
        for asset in assets:
            raw = asset['content']
            digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
            memory_id = 'seed:' + asset['id'] + ':' + digest[:16]
            pending.append((member_id, memory_id, raw,
                            {'origin': 'butian_initial_asset', 'asset_id': asset['id'],
                             'sha256': digest, 'sources': asset['sources'],
                             'learning_state': 'unpractised', 'asset_version': catalog['version']}))
    professional = json.loads(files('butian.assets').joinpath('professional_tools.json').read_text(encoding='utf-8'))
    for item in professional:
        content = files('butian.assets').joinpath(item['file']).read_bytes()
        if hashlib.sha256(content).hexdigest() != item['wrapper_sha256']:
            raise ValueError('initial_tool_hash_mismatch:' + item['id'])
        asset_id = item['owner'] + ':' + item['id'] + ':v1'
        row = service.store.db.execute('SELECT tool_id FROM initial_asset_tools WHERE asset_id=?', (asset_id,)).fetchone()
        if row is None:
            tool_id = service.propose_tool(item['owner'], item['id'], item['description'], reuse_lower=0, manual_cost_upper=0, call_cost_lower=0, build_cost_upper=0, maintenance_cost_upper=0, risk_cost_upper=0)['tool_id']
            manifest = {'input_contract': item.get('input_contract', {'type': 'object', 'required': ['args'], 'properties': {'args': {'type': 'array', 'items': {'type': 'string'}}}}), 'output_contract': {'type': 'object', 'required': ['exit_code']}, 'permissions': item['permissions'], 'failure_conditions': ['invalid arguments', 'missing optional dependency', 'host capability unavailable'], 'entrypoint': ['python', 'tool.py'], 'source_kind': 'internal', 'source_skill': item['source_skill'], 'source_sha256': item['source_sha256'], 'dependencies': item['dependencies'], 'learning_state': 'unpractised'}
            service.add_tool_version(item['owner'], tool_id, content, manifest)
            service.store.db.execute('INSERT INTO initial_asset_tools VALUES (?,?)', (asset_id, tool_id))
            result['tools_created'] += 1
        else:
            tool_id = row['tool_id']
        content = (item['id'] + ' 专业工具 ' + item['description'] +
                   '\n工具ID=' + tool_id + ' version=1。输入契约见工具manifest；命令行包装器使用args数组，先传--help查看参数。输出exit_code；非0不能解释为计算成功。依赖：' +
                   ', '.join(item['dependencies']) + '。通过宿主现有工具执行接口授权调用；尚未形成实践验证。')
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        pending.append((item['owner'], 'seed:professional-tool:' + item['id'] + ':' + digest[:16], content,
                        {'origin': 'butian_initial_asset', 'source_skill': item['source_skill'],
                         'source_sha256': item['source_sha256'], 'learning_state': 'unpractised'}))
    return result
