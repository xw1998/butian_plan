import hashlib
import json
from butian.runtime.service import ButianService

def test_captured_source_dispatches_one_host_extraction_task_after_restart(tmp_path):
    service=ButianService(tmp_path)
    try:
        service.add_member('worker','Worker')
        raw=b'Only original measured source content.'
        source=service.learning.add_source('manual','https://example.org/source','source',hashlib.sha256(raw).hexdigest(),owner_member_id='worker')
        service.learning.save_source_snapshot(source,raw,owner_member_id='worker')
        service.learning.verify_source_content(source,raw,owner_member_id='worker')
        results=service.dispatch_learning_extractions()
        task=next(item for item in results if item['source_id']==source)['task_id']
        assert service.dispatch_learning_extractions()==[]
        package=service.export_execution(task,actor_member_id='worker')
        assert 'butian_knowledge_source_read' in package['context']
        assert source in package['context']
        assert service.learning.claims_for_source(source,owner_member_id='worker')==[]
    finally:service.close()
    service=ButianService(tmp_path)
    try:
        assert service.dispatch_learning_extractions()==[]
        payload=json.loads(service.store.db.execute('SELECT payload FROM tasks WHERE task_id=?',(task,)).fetchone()[0])
        assert payload['learning_source_id']==source
    finally:service.close()


def test_two_runtime_connections_dispatch_source_once(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    service=ButianService(tmp_path)
    try:
        service.add_member('worker','Worker')
        raw=b'Original independent source.'
        source=service.learning.add_source('manual','https://example.org/race','Race',hashlib.sha256(raw).hexdigest(),owner_member_id='worker')
        service.learning.save_source_snapshot(source,raw,owner_member_id='worker')
        service.learning.verify_source_content(source,raw,owner_member_id='worker')
    finally:service.close()
    barrier=threading.Barrier(2)
    def dispatch(_):
        local=ButianService(tmp_path)
        try:
            barrier.wait(timeout=10)
            return local.dispatch_learning_extractions()
        finally:local.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(dispatch,range(2)))
    assert sum(len(result) for result in results)==1
    service=ButianService(tmp_path)
    try:
        assert service.store.db.execute('SELECT COUNT(*) FROM source_extraction_tasks WHERE source_id=?',(source,)).fetchone()[0]==1
        assert service.store.db.execute("SELECT COUNT(*) FROM tasks WHERE json_extract(payload,'$.learning_source_id')=?",(source,)).fetchone()[0]==1
    finally:service.close()


def test_failed_dispatch_rolls_back_task_and_retries(tmp_path):
    import pytest
    service=ButianService(tmp_path)
    try:
        service.add_member('worker','Worker')
        raw=b'Original recovery source.'
        source=service.learning.add_source('manual','https://example.org/recovery','Recovery',hashlib.sha256(raw).hexdigest(),owner_member_id='worker')
        service.learning.save_source_snapshot(source,raw,owner_member_id='worker')
        service.learning.verify_source_content(source,raw,owner_member_id='worker')
        service.store.db.execute("CREATE TRIGGER reject_extraction BEFORE INSERT ON source_extraction_tasks BEGIN SELECT RAISE(ABORT,'injected_extraction_failure'); END")
        with pytest.raises(Exception,match='injected_extraction_failure'):
            service.dispatch_learning_extractions()
        assert service.store.db.execute('SELECT COUNT(*) FROM source_extraction_tasks').fetchone()[0]==0
        assert service.store.db.execute("SELECT COUNT(*) FROM tasks WHERE json_extract(payload,'$.learning_source_id')=?",(source,)).fetchone()[0]==0
        service.store.db.execute('DROP TRIGGER reject_extraction')
        assert len(service.dispatch_learning_extractions())==1
    finally:service.close()


def test_undecodable_source_does_not_starve_valid_source_after_restart(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('worker', 'Worker')
        sources = []
        for index, raw in enumerate((b'bad\xffsource', b'Valid original evidence.')):
            source = service.learning.add_source('manual', 'https://example.org/fair-' + str(index), 'source', hashlib.sha256(raw).hexdigest(), owner_member_id='worker')
            service.learning.save_source_snapshot(source, raw, owner_member_id='worker')
            service.learning.verify_source_content(source, raw, owner_member_id='worker')
            sources.append(source)
        first = service.dispatch_learning_extractions(limit=1)
        assert first[0]['source_id'] == sources[0]
        assert first[0]['status'] == 'blocked'
        assert first[0]['reason'] == 'source_snapshot_decode_failed'
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        second = service.dispatch_learning_extractions(limit=1)
        assert second[0]['source_id'] == sources[1]
        assert second[0]['status'] == 'awaiting_host'
        assert service.store.db.execute('SELECT COUNT(*) FROM source_extraction_tasks').fetchone()[0] == 1
        assert not service.store.db.in_transaction
    finally:
        service.close()
