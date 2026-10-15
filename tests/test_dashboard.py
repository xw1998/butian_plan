import sqlite3
from butian.runtime.service import ButianService
from butian.runtime.dashboard import snapshot


def test_dashboard_counts_and_missing_libraries(tmp_path):
    service = ButianService(tmp_path)
    baseline = snapshot(service, 'xu_wei')
    try:
        service.add_member('a', 'A')
        service.add_member('b', 'B')
        memory = service.memory('a')
        memory.put('one', 'experience', 'PRIVATE_CONTENT', status='candidate')
        memory.put('two', 'lesson', 'PRIVATE_LESSON', status='warm', source_event_ids=['evidence'])
        memory.close()
        result = snapshot(service, 'a')
        assert result['members'] == baseline['members'] + 2
        assert result['totals']['memories'] == baseline['totals'].get('memories', 0) + 2
        assert result['totals']['pending_validation'] == baseline['totals'].get('pending_validation', 0) + 1
        assert result['memory_kinds']['experience'] == baseline['memory_kinds'].get('experience', 0) + 1
        assert result['memory_kinds']['lesson'] == baseline['memory_kinds'].get('lesson', 0) + 1
        assert result['avoided_errors']['count'] is None
        assert 'PRIVATE_' not in str(result)
        assert result['coverage']['readable'] == baseline['coverage'].get('readable', 0) + 2
        assert not result['partial']
        broken = tmp_path / 'members' / 'b' / 'memory.db'
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_bytes(b'not a sqlite database')
        result = snapshot(service, 'a')
        assert result['partial']
        assert result['coverage']['unavailable'] == 1
        assert result['totals']['memories'] == baseline['totals'].get('memories', 0) + 2
    finally:
        service.close()


def test_dashboard_evidence_counts_are_not_avoidance(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('a', 'A')
        memory = service.memory('a')
        memory.put('one', 'lesson', 'private', status='candidate')
        memory.record_usage_feedback('one', outcome='useful', evidence_ref='proof', evidence_digest='digest')
        memory.record_usage_feedback('one', outcome='useful', evidence_ref='proof', evidence_digest='digest')
        memory.close()
        result = snapshot(service, 'a')
        assert result['totals']['useful_evidence'] == 1
        assert result['avoided_errors']['count'] is None
    finally:
        service.close()


def test_dashboard_http_authentication(tmp_path):
    import json
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    from butian.runtime.http_api import ApiHandler
    service = ButianService(tmp_path)
    service.store.set_member_token('xu_wei', 'test-dashboard-only')
    handler = type('DashboardHandler', (ApiHandler,), {'service': service})
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = 'http://127.0.0.1:' + str(server.server_port) + '/api/dashboard'
    try:
        for headers in ({}, {'X-Butian-Member': 'xu_wei', 'X-Butian-Token': 'invalid'}):
            try:
                urlopen(Request(url, headers=headers))
                raise AssertionError('unauthenticated read succeeded')
            except HTTPError as error:
                assert error.code == 401
        request = Request(url, headers={'X-Butian-Member': 'xu_wei', 'X-Butian-Token': 'test-dashboard-only'})
        with urlopen(request) as response:
            result = json.load(response)
        assert result['status'] == 'ok'
        assert result['data']['scope'] == 'team_counts_only'
        assert result['data']['avoided_errors']['count'] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        service.close()
