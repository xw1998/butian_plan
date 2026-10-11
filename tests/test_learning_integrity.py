import hashlib
import sqlite3
import tempfile
from pathlib import Path

from butian.runtime.learning import LearningStore


def test_source_hash_verification_and_claim_criteria():
    with tempfile.TemporaryDirectory() as d:
        db = sqlite3.connect(Path(d) / 'learning.db')
        try:
            store = LearningStore(db)
            content = 'retrieved source body'
            source_id = store.add_source('web', 'https://example.test/a', 'A', hashlib.sha256(content.encode()).hexdigest())
            assert store.verify_source_content(source_id, content)['status'] == 'verified'
            assert store.verify_source_content(source_id, 'changed')['status'] == 'hash_mismatch'
            claim_id = store.add_claim(source_id, 'Claim A', confidence=.6, predictions=['P1'], falsifiers=['F1'])
            prior = store.claim_as_prior(claim_id)
            assert prior['predictions'] == ['P1'] and prior['falsifiers'] == ['F1']
            assert store.get_claim(claim_id)['status'] == 'candidate'
        finally:
            db.close()


def test_learning_migrates_existing_source_schema():
    with tempfile.TemporaryDirectory() as d:
        db = sqlite3.connect(Path(d) / 'legacy.db')
        try:
            db.executescript("""
            CREATE TABLE knowledge_sources(source_id TEXT PRIMARY KEY,source_type TEXT NOT NULL,locator TEXT NOT NULL,title TEXT NOT NULL,content_hash TEXT NOT NULL,retrieved_at REAL NOT NULL,metadata TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'candidate');
            CREATE TABLE source_claims(claim_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,claim TEXT NOT NULL,conditions TEXT NOT NULL,limitations TEXT NOT NULL,confidence REAL NOT NULL,status TEXT NOT NULL DEFAULT 'candidate',created_at REAL NOT NULL);
            """)
            store = LearningStore(db)
            sid = store.add_source('manual', 'local://x', 'legacy', 'abc')
            assert sid
        finally:
            db.close()
