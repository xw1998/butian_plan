import hashlib
import json
import pytest
from butian.runtime.service import ButianService
from butian.runtime.extraction import validate_extraction

@pytest.mark.parametrize('quote', ['fabricated quotation', ''])
def test_extraction_rejects_missing_original_quote(tmp_path, quote):
    service=ButianService(tmp_path)
    try:
        service.add_member('reader','Reader')
        raw=b'Measured pressure was 2 Pa under room temperature.'
        source=service.learning.add_source('manual','https://example.org/pressure','Pressure',hashlib.sha256(raw).hexdigest(),owner_member_id='reader')
        service.learning.save_source_snapshot(source,raw,owner_member_id='reader')
        service.learning.verify_source_content(source,raw,owner_member_id='reader')
        task=service.dispatch_learning_extractions()[0]['task_id']
        payload=json.loads(service.store.db.execute('SELECT payload FROM tasks WHERE task_id=?',(task,)).fetchone()[0])
        output=json.dumps({'claims':[{'claim':'Pressure was 2 Pa','quote':quote,'conditions':['room temperature'],'limitations':['one sample'],'predictions':['repeat measurement is 2 Pa'],'falsifiers':['repeat measurement differs']}]})
        with pytest.raises(ValueError,match='extraction_quote_not_in_source'):
            validate_extraction(service,payload,'reader',output)
        assert service.learning.claims_for_source(source,owner_member_id='reader')==[]
    finally: service.close()


def test_valid_extraction_is_candidate_data_and_hash_bound(tmp_path):
    service=ButianService(tmp_path)
    try:
        service.add_member('reader','Reader')
        raw=b'Measured pressure was 2 Pa under room temperature.'
        source=service.learning.add_source('manual','https://example.org/pressure','Pressure',hashlib.sha256(raw).hexdigest(),owner_member_id='reader')
        service.learning.save_source_snapshot(source,raw,owner_member_id='reader')
        service.learning.verify_source_content(source,raw,owner_member_id='reader')
        task=service.dispatch_learning_extractions()[0]['task_id']
        payload=json.loads(service.store.db.execute('SELECT payload FROM tasks WHERE task_id=?',(task,)).fetchone()[0])
        claim={'claim':'Pressure was 2 Pa','quote':raw.decode(),'conditions':['room temperature'],'limitations':['one sample'],'predictions':['repeat measurement is 2 Pa'],'falsifiers':['repeat measurement differs']}
        assert validate_extraction(service,payload,'reader',json.dumps({'claims':[claim]}))['claims']==[claim]
        service.learning.verify_source_content(source,b'changed',owner_member_id='reader')
        with pytest.raises(ValueError,match='knowledge_source_hash_mismatch'):
            validate_extraction(service,payload,'reader',json.dumps({'claims':[claim]}))
    finally: service.close()


def test_reviewed_extraction_publishes_once_after_restart(tmp_path):
    from butian.runtime.extraction import ingest_reviewed_extractions
    service=ButianService(tmp_path)
    try:
        service.add_member('reader','Reader')
        raw=b'Measured pressure was 2 Pa under room temperature.'
        source=service.learning.add_source('manual','https://example.org/pressure','Pressure',hashlib.sha256(raw).hexdigest(),owner_member_id='reader')
        service.learning.save_source_snapshot(source,raw,owner_member_id='reader')
        service.learning.verify_source_content(source,raw,owner_member_id='reader')
        task=service.dispatch_learning_extractions()[0]['task_id']
        package=service.export_execution(task,actor_member_id='reader')
        claim={'claim':'Pressure was 2 Pa','quote':raw.decode(),'conditions':['room temperature'],'limitations':['one sample'],'predictions':['repeat measurement is 2 Pa'],'falsifiers':['repeat measurement differs']}
        proof=tmp_path/'extraction.json';proof.write_text(json.dumps({'claims':[claim]}),encoding='utf-8')
        evidence=service.record_evidence(str(proof),owner_member_id='reader',actor_member_id='reader')
        delivered=service.submit_execution(task,expected_revision=package['task_revision'],problem_revision=package['problem_revision'],submission_id='extraction-1',output=proof.read_text(encoding='utf-8'),evidence_ids=[evidence],actor_member_id='reader')
        assert service.learning.claims_for_source(source,owner_member_id='reader')==[]
        service.review_task(task,delivered['revision'],'approve',reviewer_member_id='reader',actor_member_id='reader')
        claims=service.learning.claims_for_source(source,owner_member_id='reader')
        assert len(claims)==1 and claims[0]['status']=='candidate' and claims[0]['confidence']==0
        assert json.loads(claims[0]['conditions'])['source_quote']==raw.decode()
        assert ingest_reviewed_extractions(service)==[]
    finally:service.close()
    service=ButianService(tmp_path)
    try:
        assert ingest_reviewed_extractions(service)==[]
        assert len(service.learning.claims_for_source(source,owner_member_id='reader'))==1
    finally:service.close()


def test_approved_extraction_publication_failure_recovers_without_duplicates(tmp_path):
    from butian.runtime.extraction import ingest_reviewed_extractions
    service=ButianService(tmp_path)
    try:
        service.add_member('reader','Reader')
        raw=b'Measured pressure was 2 Pa under room temperature.'
        source=service.learning.add_source('manual','https://example.org/recovery','Recovery',hashlib.sha256(raw).hexdigest(),owner_member_id='reader')
        service.learning.save_source_snapshot(source,raw,owner_member_id='reader')
        service.learning.verify_source_content(source,raw,owner_member_id='reader')
        task=service.dispatch_learning_extractions()[0]['task_id']
        package=service.export_execution(task,actor_member_id='reader')
        claim={'claim':'Pressure was 2 Pa','quote':raw.decode(),'conditions':['room temperature'],'limitations':['one sample'],'predictions':['repeat measurement is 2 Pa'],'falsifiers':['repeat measurement differs']}
        output=json.dumps({'claims':[claim]})
        proof=tmp_path/'extraction.json';proof.write_text(output,encoding='utf-8')
        evidence=service.record_evidence(str(proof),owner_member_id='reader',actor_member_id='reader')
        delivered=service.submit_execution(task,expected_revision=package['task_revision'],problem_revision=package['problem_revision'],submission_id='recover-extraction',output=output,evidence_ids=[evidence],actor_member_id='reader')
        service.store.db.execute('CREATE TABLE IF NOT EXISTS source_extraction_reviews (task_id TEXT PRIMARY KEY, result TEXT NOT NULL)')
        service.store.db.execute("CREATE TRIGGER reject_publication BEFORE INSERT ON source_extraction_reviews BEGIN SELECT RAISE(ABORT,'injected_publication_failure'); END")
        with pytest.raises(Exception,match='injected_publication_failure'):
            service.review_task(task,delivered['revision'],'approve',reviewer_member_id='reader',actor_member_id='reader')
        assert service.store.db.execute('SELECT status FROM tasks WHERE task_id=?',(task,)).fetchone()[0]=='done'
        assert service.learning.claims_for_source(source,owner_member_id='reader')==[]
        service.store.db.execute('DROP TRIGGER reject_publication')
    finally:service.close()
    service=ButianService(tmp_path)
    try:
        assert len(ingest_reviewed_extractions(service))==1
        assert ingest_reviewed_extractions(service)==[]
        assert len(service.learning.claims_for_source(source,owner_member_id='reader'))==1
    finally:service.close()


def test_invalid_source_does_not_block_other_approved_extractions(tmp_path):
    from butian.runtime.extraction import ingest_reviewed_extractions
    service = ButianService(tmp_path)
    try:
        service.add_member('reader', 'Reader')
        sources = []
        for number in range(2):
            raw = ('Measured pressure was %s Pa.' % (number + 2)).encode()
            source = service.learning.add_source('manual', 'https://example.org/batch/' + str(number), 'Pressure', hashlib.sha256(raw).hexdigest(), owner_member_id='reader')
            service.learning.save_source_snapshot(source, raw, owner_member_id='reader')
            service.learning.verify_source_content(source, raw, owner_member_id='reader')
            sources.append(source)
            task = service.dispatch_learning_extractions()[0]['task_id']
            claim = {'claim': raw.decode(), 'quote': raw.decode(), 'conditions': ['sample'], 'limitations': ['one sample'], 'predictions': ['repeat agrees'], 'falsifiers': ['repeat differs']}
            # Approved pending-publication fixtures model a restart after approval.
            service.store.db.execute("UPDATE tasks SET status='done',payload=json_set(payload,'$.review_verdict','approve','$.provider_output',?) WHERE task_id=?", (json.dumps({'claims': [claim]}), task))
        service.learning.verify_source_content(sources[0], b'changed', owner_member_id='reader')
        results = ingest_reviewed_extractions(service)
        assert any(item['source_id'] == sources[1] and item['status'] == 'candidate' for item in results)
        assert service.learning.claims_for_source(sources[0], owner_member_id='reader') == []
        assert len(service.learning.claims_for_source(sources[1], owner_member_id='reader')) == 1
        assert any(item['source_id'] == sources[0] and item['status'] == 'blocked' for item in results)
    finally:
        service.close()

@pytest.mark.parametrize('limit', [True, False, 0, -1, 21, 1.5, '1', None])
def test_extraction_publication_rejects_invalid_batch_limit(tmp_path, limit):
    from butian.runtime.extraction import ingest_reviewed_extractions
    service = ButianService(tmp_path)
    try:
        with pytest.raises(ValueError, match='invalid_learning_ingest_limit'):
            ingest_reviewed_extractions(service, limit=limit)
        assert not service.store.db.in_transaction
    finally:
        service.close()

def test_invalid_quote_does_not_starve_valid_publication_at_limit_one(tmp_path):
    from butian.runtime.extraction import ingest_reviewed_extractions
    service = ButianService(tmp_path)
    try:
        service.add_member('reader', 'Reader')
        sources = []
        for number in range(2):
            raw = ('Measured pressure was %s Pa.' % (number + 2)).encode()
            source = service.learning.add_source('manual', 'https://example.org/fair/' + str(number), 'Pressure', hashlib.sha256(raw).hexdigest(), owner_member_id='reader')
            service.learning.save_source_snapshot(source, raw, owner_member_id='reader')
            service.learning.verify_source_content(source, raw, owner_member_id='reader')
            sources.append(source)
            task = service.dispatch_learning_extractions()[0]['task_id']
            claim = {'claim': raw.decode(), 'quote': 'fabricated' if number == 0 else raw.decode(), 'conditions': ['sample'], 'limitations': ['one sample'], 'predictions': ['repeat agrees'], 'falsifiers': ['repeat differs']}
            service.store.db.execute("UPDATE tasks SET status='done',payload=json_set(payload,'$.review_verdict','approve','$.provider_output',?) WHERE task_id=?", (json.dumps({'claims': [claim]}), task))
        first = ingest_reviewed_extractions(service, limit=1)
        assert first[0]['status'] == 'blocked'
        service.close()
        service = ButianService(tmp_path)
        ingest_reviewed_extractions(service, limit=1)
        assert len(service.learning.claims_for_source(sources[1], owner_member_id='reader')) == 1
        assert service.learning.claims_for_source(sources[0], owner_member_id='reader') == []
    finally:
        service.close()


def test_ingestion_preserves_callers_outer_transaction(tmp_path):
    from butian.runtime.extraction import ingest_reviewed_extractions
    service = ButianService(tmp_path)
    try:
        db = service.store.db
        db.execute('CREATE TABLE caller_transaction_probe (value TEXT)')
        db.execute('BEGIN IMMEDIATE')
        db.execute("INSERT INTO caller_transaction_probe VALUES ('pending')")
        assert ingest_reviewed_extractions(service) == []
        assert db.in_transaction
        assert db.execute('SELECT value FROM caller_transaction_probe').fetchone()[0] == 'pending'
        db.rollback()
        assert db.execute('SELECT COUNT(*) FROM caller_transaction_probe').fetchone()[0] == 0
    finally:
        service.close()


def test_ingestion_failure_rolls_back_only_its_savepoint(tmp_path, monkeypatch):
    from butian.runtime.extraction import ingest_reviewed_extractions
    service = ButianService(tmp_path)
    try:
        service.add_member('reader', 'Reader')
        raw = b'Measured pressure was 2 Pa.'
        source = service.learning.add_source('manual', 'https://example.org/outer', 'Pressure', hashlib.sha256(raw).hexdigest(), owner_member_id='reader')
        service.learning.save_source_snapshot(source, raw, owner_member_id='reader')
        service.learning.verify_source_content(source, raw, owner_member_id='reader')
        task = service.dispatch_learning_extractions()[0]['task_id']
        claim = {'claim': raw.decode(), 'quote': raw.decode(), 'conditions': ['sample'], 'limitations': ['one sample'], 'predictions': ['repeat agrees'], 'falsifiers': ['repeat differs']}
        db = service.store.db
        db.execute("UPDATE tasks SET status='done',payload=json_set(payload,'$.review_verdict','approve','$.provider_output',?) WHERE task_id=?", (json.dumps({'claims': [claim]}), task))
        db.execute('CREATE TABLE caller_failure_probe (value TEXT)')
        db.execute('BEGIN IMMEDIATE')
        db.execute("INSERT INTO caller_failure_probe VALUES ('pending')")
        original = service.store.append_event
        def fail_publication(aggregate_id, event_type, *args, **kwargs):
            if event_type == 'source_candidates_reviewed':
                raise RuntimeError('publication_fault')
            return original(aggregate_id, event_type, *args, **kwargs)
        monkeypatch.setattr(service.store, 'append_event', fail_publication)
        with pytest.raises(RuntimeError, match='publication_fault'):
            ingest_reviewed_extractions(service)
        assert db.in_transaction
        assert db.execute('SELECT COUNT(*) FROM caller_failure_probe').fetchone()[0] == 1
        assert service.learning.claims_for_source(source, owner_member_id='reader') == []
        assert db.execute('SELECT COUNT(*) FROM source_extraction_reviews').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM source_extraction_attempts').fetchone()[0] == 0
        db.rollback()
        assert db.execute('SELECT COUNT(*) FROM caller_failure_probe').fetchone()[0] == 0
    finally:
        service.close()


@pytest.mark.parametrize('corruption', ['missing_source', 'different_source'])
def test_extraction_publication_requires_dispatch_source_binding(tmp_path, corruption):
    from butian.runtime.extraction import ingest_reviewed_extractions
    service = ButianService(tmp_path)
    try:
        service.add_member('reader', 'Reader')
        sources = []
        for number in range(2):
            raw = ('Measured pressure was %s Pa.' % (number + 2)).encode()
            source = service.learning.add_source('manual', 'https://example.org/binding/' + str(number), 'Pressure', hashlib.sha256(raw).hexdigest(), owner_member_id='reader')
            service.learning.save_source_snapshot(source, raw, owner_member_id='reader')
            service.learning.verify_source_content(source, raw, owner_member_id='reader')
            sources.append((source, raw))
        tasks = service.dispatch_learning_extractions()
        task = service.store.db.execute('SELECT task_id FROM source_extraction_tasks WHERE source_id=?', (sources[0][0],)).fetchone()[0]
        payload = json.loads(service.store.db.execute('SELECT payload FROM tasks WHERE task_id=?', (task,)).fetchone()[0])
        source, raw = sources[1]
        claim = {'claim': raw.decode(), 'quote': raw.decode(), 'conditions': ['sample'], 'limitations': ['one sample'], 'predictions': ['repeat agrees'], 'falsifiers': ['repeat differs']}
        payload.update(review_verdict='approve', provider_output=json.dumps({'claims': [claim]}))
        if corruption == 'missing_source':
            payload.pop('learning_source_id')
        else:
            payload.update(learning_source_id=source, learning_source_hash=hashlib.sha256(raw).hexdigest())
        service.store.db.execute("UPDATE tasks SET status='done',payload=? WHERE task_id=?", (json.dumps(payload), task))
        result = ingest_reviewed_extractions(service)
        assert result == [{'task_id': task, 'source_id': sources[0][0], 'status': 'blocked', 'reason': 'extraction_source_binding_mismatch'}]
        assert service.learning.claims_for_source(source, owner_member_id='reader') == []
        assert not service.store.db.in_transaction
    finally:
        service.close()

@pytest.mark.parametrize('corrupt_payload', ['{broken', 'null', '[]'])
def test_corrupt_extraction_payload_does_not_block_valid_publication(tmp_path, corrupt_payload):
    from butian.runtime.extraction import ingest_reviewed_extractions
    service = ButianService(tmp_path)
    try:
        service.add_member('reader', 'Reader')
        sources = []
        tasks = []
        for number in range(2):
            raw = ('Measured pressure was %s Pa.' % (number + 2)).encode()
            source = service.learning.add_source('manual', 'https://example.org/corrupt/' + str(number), 'Pressure', hashlib.sha256(raw).hexdigest(), owner_member_id='reader')
            service.learning.save_source_snapshot(source, raw, owner_member_id='reader')
            service.learning.verify_source_content(source, raw, owner_member_id='reader')
            sources.append(source)
            task = service.dispatch_learning_extractions()[0]['task_id']
            tasks.append(task)
            claim = {'claim': raw.decode(), 'quote': raw.decode(), 'conditions': ['sample'], 'limitations': ['one sample'], 'predictions': ['repeat agrees'], 'falsifiers': ['repeat differs']}
            service.store.db.execute("UPDATE tasks SET status='done',payload=json_set(payload,'$.review_verdict','approve','$.provider_output',?) WHERE task_id=?", (json.dumps({'claims': [claim]}), task))
        service.store.db.execute('UPDATE tasks SET payload=? WHERE task_id=?', (corrupt_payload, tasks[0]))
        results = ingest_reviewed_extractions(service)
        assert len(service.learning.claims_for_source(sources[1], owner_member_id='reader')) == 1
        assert service.learning.claims_for_source(sources[0], owner_member_id='reader') == []
        assert any(item['task_id'] == tasks[1] and item['status'] == 'candidate' for item in results)
        assert not service.store.db.in_transaction
    finally:
        service.close()
