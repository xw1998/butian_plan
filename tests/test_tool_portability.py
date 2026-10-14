import copy
import json
import pytest
from butian.runtime.service import ButianService

MANIFEST = {'input_contract': {'type': 'object'}, 'output_contract': {'type': 'object'}, 'permissions': ['cpu'], 'failure_conditions': ['invalid_input'], 'license': 'MIT'}

@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    value = ButianService(tmp_path)
    value.add_member('author', 'Author')
    value.add_member('recipient', 'Recipient')
    yield value
    value.close()

def asset(service):
    tool = service.propose_tool('author', 'Calculator', 'Public description', reuse_lower=3, manual_cost_upper=10, call_cost_lower=1, build_cost_upper=1, maintenance_cost_upper=1, risk_cost_upper=1)
    service.add_tool_version('author', tool['tool_id'], 'print(1)', {**MANIFEST, 'private_note': 'MEMORY-MUST-NOT-TRAVEL'})
    service.add_tool_version('author', tool['tool_id'], 'print(2)', MANIFEST)
    return tool['tool_id']

def test_tool_bundle_round_trip_has_no_private_state(service):
    tool_id = asset(service)
    bundle = service.export_tool_bundle('author', tool_id)
    text = json.dumps(bundle)
    assert 'MEMORY-MUST-NOT-TRAVEL' not in text
    assert 'source_path' not in text and 'authorization' not in text
    imported = service.import_tool_bundle('recipient', bundle, license='MIT', scan_status='passed')
    tools = service.list_tools('recipient')
    assert len(tools) == 1
    assert tools[0]['status'] != 'active'
    versions = service.store.db.execute('SELECT version,content_hash FROM tool_versions WHERE tool_id=? ORDER BY version', (imported['tool_id'],)).fetchall()
    assert [r['version'] for r in versions] == [1, 2]
    assert [r['content_hash'] for r in versions] == [v['content_sha256'] for v in bundle['versions']]
    assert imported['requires_reverification'] is True

def test_tool_bundle_owner_and_integrity(service):
    tool_id = asset(service)
    with pytest.raises(ValueError, match='tool_access_denied'):
        service.export_tool_bundle('recipient', tool_id)
    bundle = service.export_tool_bundle('author', tool_id)
    changed = copy.deepcopy(bundle)
    changed['versions'][0]['content_base64'] = 'cHJpbnQoOTkp'
    with pytest.raises(ValueError, match='checksum'):
        service.import_tool_bundle('recipient', changed, license='MIT', scan_status='passed')
    with pytest.raises(ValueError, match='supply_chain'):
        service.import_tool_bundle('recipient', bundle, license='MIT', scan_status='pending')
    assert service.list_tools('recipient') == []

def test_tool_bundle_mcp_authentication(service):
    from butian.mcp_server import call_tool
    import secrets
    token = secrets.token_urlsafe(24)
    service.store.set_member_token('author', token)
    tool_id = asset(service)
    with pytest.raises(ValueError, match='actor_member_id_required'):
        call_tool(service, 'butian_tool_bundle_export', {'member_id': 'author', 'tool_id': tool_id})
    bundle = call_tool(service, 'butian_tool_bundle_export', {'member_id': 'author', 'actor_member_id': 'author', 'token': token, 'tool_id': tool_id})
    assert len(bundle['versions']) == 2

def test_tool_import_event_fault_rolls_back_all_database_changes(service):
    import sqlite3
    tool_id = asset(service)
    bundle = service.export_tool_bundle('author', tool_id)
    db = service.store.db
    db.execute("CREATE TEMP TRIGGER fail_tool_import BEFORE INSERT ON tool_events WHEN NEW.event_type='bundle_imported' BEGIN SELECT RAISE(ABORT, 'import_event_fault'); END")
    with pytest.raises(sqlite3.IntegrityError, match='import_event_fault'):
        service.import_tool_bundle('recipient', bundle, license='MIT', scan_status='passed')
    assert service.list_tools('recipient') == []
    assert db.execute("SELECT COUNT(*) FROM tool_versions v LEFT JOIN tool_assets a ON a.tool_id=v.tool_id WHERE a.tool_id IS NULL").fetchone()[0] == 0

def test_tool_bundle_preserves_required_host_capabilities(service):
    tool = service.propose_tool('author', 'Trusted', 'Requires host grant', reuse_lower=1, manual_cost_upper=1, call_cost_lower=0, build_cost_upper=0, maintenance_cost_upper=0, risk_cost_upper=0)
    service.add_tool_version('author', tool['tool_id'], 'print(1)', {**MANIFEST, 'isolation': {'profile': 'host_trusted', 'requires': ['network_isolation']}, 'host_capabilities': ['filesystem'], 'business_assertions': [{'path': '$', 'expected': 1}]})
    bundle = service.export_tool_bundle('author', tool['tool_id'])
    imported = service.import_tool_bundle('recipient', bundle, license='MIT', scan_status='passed')
    row = service.store.db.execute('SELECT manifest FROM tool_versions WHERE tool_id=?', (imported['tool_id'],)).fetchone()
    manifest = json.loads(row[0])
    assert manifest['isolation'] == {'profile': 'host_trusted', 'requires': ['network_isolation']}
    assert manifest['host_capabilities'] == ['filesystem']
    assert manifest['business_assertions'] == [{'path': '$', 'expected': 1}]
    assert service.list_host_authorizations('recipient') == []

def test_bundle_preserves_identical_source_revisions(service):
    from butian.runtime.tool_portability import digest
    bundle = service.export_tool_bundle('author', asset(service))
    bundle['versions'][1] = {**copy.deepcopy(bundle['versions'][0]), 'version': 2}
    bundle['bundle_sha256'] = digest({k: v for k, v in bundle.items() if k != 'bundle_sha256'})
    imported = service.import_tool_bundle('recipient', bundle, license='MIT', scan_status='passed')
    rows = service.store.db.execute('SELECT version,content_hash FROM tool_versions WHERE tool_id=? ORDER BY version', (imported['tool_id'],)).fetchall()
    assert [r['version'] for r in rows] == [1, 2]
    assert rows[0]['content_hash'] == rows[1]['content_hash']

def test_bundle_import_preserves_outer_transaction(service):
    bundle = service.export_tool_bundle('author', asset(service))
    db = service.store.db
    db.execute('BEGIN')
    imported = service.import_tool_bundle('recipient', bundle, license='MIT', scan_status='passed')
    assert db.in_transaction
    assert service.tools._tool(imported['tool_id'])['status'] == 'draft'
    db.rollback()
    assert service.list_tools('recipient') == []

def test_rolled_back_import_sources_are_quarantined_on_recovery(service):
    from butian.runtime.tools import ToolStore
    bundle = service.export_tool_bundle('author', asset(service))
    service.store.db.execute('BEGIN')
    imported = service.import_tool_bundle('recipient', bundle, license='MIT', scan_status='passed')
    paths = [row[0] for row in service.store.db.execute('SELECT source_path FROM tool_versions WHERE tool_id=?', (imported['tool_id'],))]
    service.store.db.rollback()
    recovered = ToolStore(service.store, service.home)
    assert any(item['tool_id'] == imported['tool_id'] and item['action'] == 'quarantined_orphan' for item in recovered.recovery_events)
    from pathlib import Path
    assert all(not Path(path).exists() for path in paths)
    assert service.list_tools('recipient') == []
