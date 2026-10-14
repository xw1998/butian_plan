"""Validate host reasoning against immutable source bytes, without promotion."""
import json
import uuid


def validate_extraction(service, payload, owner, output):
    source_id = payload.get('learning_source_id')
    if not source_id:
        return None
    pages = []
    offset = 0
    while True:
        page = service.learning.read_source_snapshot(source_id, owner_member_id=owner, offset=offset, limit=50000)
        expected = str(payload.get('learning_source_hash', '')).lower().removeprefix('sha256:')
        if page['sha256'] != expected:
            raise ValueError('knowledge_source_hash_mismatch')
        pages.append(page['content'])
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    original = ''.join(pages)
    try:
        result = json.loads(output)
    except (ValueError, TypeError) as exc:
        raise ValueError('invalid_extraction_json') from exc
    if not isinstance(result, dict) or not isinstance(result.get('claims'), list) or len(result['claims']) > 50:
        raise ValueError('invalid_extraction_claims')
    if not result['claims'] and (not isinstance(result.get('limitations'), str) or not result['limitations'].strip()):
        raise ValueError('empty_extraction_requires_limitations')
    seen = set()
    for item in result['claims']:
        if not isinstance(item, dict) or not isinstance(item.get('claim'), str) or not item['claim'].strip() or len(item['claim']) > 10000:
            raise ValueError('invalid_extraction_claim')
        quote = item.get('quote')
        if not isinstance(quote, str) or not quote.strip() or quote not in original:
            raise ValueError('extraction_quote_not_in_source')
        for key in ('conditions', 'limitations', 'predictions', 'falsifiers'):
            values = item.get(key)
            if not isinstance(values, list) or not values or len(values) > 50 or any(not isinstance(value, str) or not value.strip() or len(value) > 10000 for value in values):
                raise ValueError('invalid_extraction_' + key)
        if item['claim'] in seen:
            raise ValueError('duplicate_extraction_claim')
        seen.add(item['claim'])
    return result


def ingest_reviewed_extractions(service, *, limit=20):
    """Recover candidate publication after committed approval; never create warm facts."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 20:
        raise ValueError("invalid_learning_ingest_limit")
    store = service.store
    results = []
    with store._write_lock:
        store.db.execute('CREATE TABLE IF NOT EXISTS source_extraction_reviews (task_id TEXT PRIMARY KEY, result TEXT NOT NULL)')
        store.db.execute('CREATE TABLE IF NOT EXISTS source_extraction_attempts (task_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL)')
        owns_transaction = not store.db.in_transaction
        savepoint = 'extraction_ingest_' + uuid.uuid4().hex
        store.db.execute('BEGIN IMMEDIATE' if owns_transaction else 'SAVEPOINT ' + savepoint)
        try:
            # Acquire SQLite's writer reservation before reading publication candidates.
            store.db.execute('UPDATE source_extraction_attempts SET attempts=attempts WHERE 0')
            rows = store.db.execute("SELECT t.task_id,t.assignee_id,t.payload,x.source_id AS dispatch_source_id FROM source_extraction_tasks x JOIN tasks t ON t.task_id=x.task_id WHERE t.status='done' AND CASE WHEN json_valid(t.payload) THEN json_extract(t.payload,'$.review_verdict') END='approve' AND NOT EXISTS (SELECT 1 FROM source_extraction_reviews r WHERE r.task_id=t.task_id) ORDER BY COALESCE((SELECT attempts FROM source_extraction_attempts a WHERE a.task_id=t.task_id),0), CASE WHEN EXISTS (SELECT 1 FROM knowledge_sources k WHERE k.source_id=x.source_id AND k.status='verified') THEN 0 ELSE 1 END, x.created_at LIMIT ?", (limit,)).fetchall()
            for row in rows:
                store.db.execute('INSERT INTO source_extraction_attempts VALUES (?,1) ON CONFLICT(task_id) DO UPDATE SET attempts=attempts+1', (row['task_id'],))
                try:
                    payload = json.loads(row['payload'])
                    if not isinstance(payload, dict) or payload.get('learning_source_id') != row['dispatch_source_id']:
                        raise ValueError('extraction_source_binding_mismatch')
                    extracted = validate_extraction(service, payload, row['assignee_id'], payload.get('provider_output', ''))
                except ValueError as exc:
                    results.append({'task_id': row['task_id'], 'source_id': row['dispatch_source_id'], 'status': 'blocked', 'reason': str(exc)})
                    continue
                ids = []
                for claim in extracted['claims']:
                    ids.append(service.learning.add_claim(payload['learning_source_id'], claim['claim'], owner_member_id=row['assignee_id'], conditions={'conditions': claim['conditions'], 'source_quote': claim['quote'], 'source_hash': payload['learning_source_hash'], 'extraction_task_id': row['task_id']}, limitations=claim['limitations'], predictions=claim['predictions'], falsifiers=claim['falsifiers'], confidence=0.0))
                result = {'task_id': row['task_id'], 'source_id': payload['learning_source_id'], 'claim_ids': ids, 'status': 'candidate', 'limitations': extracted.get('limitations')}
                store.db.execute('INSERT INTO source_extraction_reviews VALUES (?,?)', (row['task_id'], json.dumps(result, ensure_ascii=False)))
                store.append_event(row['task_id'], 'source_candidates_reviewed', result, 'source-candidates:' + row['task_id'])
                results.append(result)
            store.db.execute('COMMIT' if owns_transaction else 'RELEASE SAVEPOINT ' + savepoint)
        except BaseException:
            if owns_transaction:
                store.db.rollback()
            else:
                store.db.execute('ROLLBACK TO SAVEPOINT ' + savepoint)
                store.db.execute('RELEASE SAVEPOINT ' + savepoint)
            raise
    return results
