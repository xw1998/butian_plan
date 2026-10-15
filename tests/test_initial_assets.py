import json
import pytest
from butian.runtime.service import ButianService
from butian.assets.tool import run


def test_seed_assets_are_recallable_unpractised_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        assert service.initial_assets == {'memories_created': 51, 'tools_created': 19}
        rows = service.store.db.execute('SELECT member_id FROM members').fetchall()
        assert len(rows) == 10
        for row in rows:
            memory = service.memory(row[0])
            try:
                records = memory.db.execute('SELECT * FROM memories').fetchall()
                assert len(records) == {'xu_wei': 7, 'shen_heng': 8, 'lin_zhi': 5, 'wen_lan': 5, 'song_xi': 5, 'ye_qing': 5}.get(row[0], 4)
                assert all(r['status'] == 'candidate' and r['confidence'] == 0 for r in records)
                assert memory.db.execute('SELECT COUNT(*) FROM practice_observations').fetchone()[0] == 0
                assert memory.search('工具')
            finally:
                memory.close()
        service.store.db.execute("UPDATE members SET name='自定义' WHERE member_id='xu_wei'")
        memory = service.memory('xu_wei')
        memory.put('user-memory', 'knowledge', '用户自己记录的知识')
        memory.close()
        tools = service.store.db.execute('SELECT owner_member_id,status FROM tool_assets').fetchall()
        assert len(tools) == 19
        assert {r['owner_member_id'] for r in tools} == {r[0] for r in rows}
        assert all(r['status'] == 'draft' for r in tools)
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        assert service.initial_assets == {'memories_created': 0, 'tools_created': 0}
        assert service.store.db.execute("SELECT name FROM members WHERE member_id='xu_wei'").fetchone()[0] == '自定义'
        memory = service.memory('xu_wei')
        assert memory.get('user-memory')['content'] == '用户自己记录的知识'
        memory.close()
    finally:
        service.close()


def test_foundation_tools_do_real_calculation_and_report_limits():
    assert run({'operation': 'statistics', 'values': [1, 2, 3]})['mean'] == 2
    assert run({'operation': 'uncertainty', 'standard_contributions': [3, 4], 'independent': True})['expanded_uncertainty'] == 10
    with pytest.raises(ValueError, match='independence'):
        run({'operation': 'uncertainty', 'standard_contributions': [3, 4]})
    with pytest.raises(ValueError):
        run({'operation': 'statistics', 'values': [float('nan')]})
    assert run({'operation': 'checklist', 'checks': {'a': True, 'b': False}})['missing'] == ['b']
    assert len(run({'operation': 'fingerprint', 'text': '补天'})['sha256']) == 64


def test_initial_methods_do_not_trigger_practice_dreams(tmp_path, monkeypatch):
    monkeypatch.setenv('BUTIAN_MAINTENANCE_ENABLED', '0')
    service = ButianService(tmp_path)
    try:
        assert service.maintenance_tick()['dream_reasoning_tasks'] == []
    finally:
        service.close()


def test_export_refuses_existing_runtime(tmp_path):
    from butian.assets.export import build_initial_database
    target = tmp_path / 'existing'
    target.mkdir()
    sentinel = target / 'user.txt'
    sentinel.write_text('keep', encoding='utf-8')
    with pytest.raises(FileExistsError):
        build_initial_database(target)
    assert sentinel.read_text(encoding='utf-8') == 'keep'


def test_learned_office_research_and_web_helpers():
    from butian.assets.tools.research_records import run as bibliography
    from butian.assets.tools.table_audit import run as table
    from butian.assets.tools.web_audit import run as web
    records = bibliography({'records': [{'doi': 'https://doi.org/10.1/test', 'title': 'A'}, {'doi': '10.1/test', 'title': 'B'}, {'title': 'A'}]})
    assert len(records['unique']) == 2
    assert records['duplicates'] == [{'index': 1, 'same_doi_as': 0}]
    result = table({'csv_text': 'id,value\n001,=1+1\n1234567890123456,2\nshort\n'})
    assert result['ragged_rows'] == [4]
    assert len(result['keep_as_text']) == 2
    assert len(result['formula_like_cells']) == 1
    result = web({'html': '<html><img src="x"><div id="x"></div><p id="x"></p></html>'})
    assert result['missing_language'] and result['images_missing_alt'] == 1
    assert result['duplicate_ids'] == ['x']


def test_portable_database_matches_public_assets(tmp_path):
    import sqlite3
    from importlib.resources import files
    from butian.assets.build import build
    target = tmp_path / 'initial.sqlite3'
    assert build(target) == {'personas': 10, 'knowledge': 32, 'tools': 19}
    bundled = files('butian.assets').joinpath('initial_knowledge.sqlite3')
    with sqlite3.connect(target) as generated, sqlite3.connect(str(bundled)) as shipped:
        for table in ('personas', 'knowledge', 'tools', 'metadata'):
            assert generated.execute('SELECT * FROM ' + table + ' ORDER BY 1').fetchall() == shipped.execute('SELECT * FROM ' + table + ' ORDER BY 1').fetchall()
        assert generated.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    with pytest.raises(FileExistsError):
        build(target)
