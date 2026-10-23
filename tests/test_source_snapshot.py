import hashlib
import pytest
from butian.runtime.service import ButianService

def test_source_snapshot_is_owned_hashed_and_restart_safe(tmp_path):
    service=ButianService(tmp_path)
    service.add_member('m','Member')
    raw='原始方法正文'.encode()
    source=service.learning.add_source('web','https://example.org/method','method',hashlib.sha256(raw).hexdigest(),owner_member_id='m')
    try:
        service.learning.save_source_snapshot(source,raw,owner_member_id='m')
        result=service.learning.read_source_snapshot(source,owner_member_id='m',offset=0,limit=100)
        assert result['content']=='原始方法正文'
        assert result['sha256']==hashlib.sha256(raw).hexdigest()
        with pytest.raises(ValueError,match='knowledge_source_not_found'):
            service.learning.read_source_snapshot(source,owner_member_id='other')
    finally: service.close()
    service=ButianService(tmp_path)
    try:
        assert service.learning.read_source_snapshot(source,owner_member_id='m')['content']=='原始方法正文'
        service.learning.verify_source_content(source,b'changed',owner_member_id='m')
        with pytest.raises(ValueError,match='knowledge_source_hash_mismatch'):
            service.learning.read_source_snapshot(source,owner_member_id='m')
    finally: service.close()


def test_mcp_snapshot_requires_owner_authentication(tmp_path):
    from butian.mcp_server import call_tool, TOOLS
    service=ButianService(tmp_path)
    try:
        for member in ('m','other'):
            service.add_member(member,member)
            service.store.set_member_token(member,'test-'+member)
        raw=b'original source text'
        source=service.learning.add_source('web','https://example.org/method','method',hashlib.sha256(raw).hexdigest(),owner_member_id='m')
        service.learning.save_source_snapshot(source,raw,owner_member_id='m')
        args={'actor_member_id':'m','member_id':'m','token':'test-m','source_id':source,'limit':8}
        first=call_tool(service,'butian_knowledge_source_read',args)
        assert first['content']=='original' and first['next_offset']==8
        second=call_tool(service,'butian_knowledge_source_read',dict(args,offset=8))
        assert second['content']==' source '
        with pytest.raises(ValueError):
            call_tool(service,'butian_knowledge_source_read',dict(args,actor_member_id='other',member_id='other',token='test-other'))
        schema=next(t['inputSchema'] for t in TOOLS if t['name']=='butian_knowledge_source_read')
        assert 'actor_member_id' in schema['required'] and 'token' not in schema['required']
    finally: service.close()


def test_capture_snapshot_failure_leaves_no_partial_source(tmp_path, monkeypatch):
    import sqlite3
    from butian.runtime.learning import LearningStore
    import butian.runtime.learning as module
    db=sqlite3.connect(tmp_path/'capture.db',isolation_level=None)
    learning=LearningStore(db)
    class Response:
        status=200
        headers={'Content-Type':'text/plain'}
        def __init__(self):self.body=b'Original captured source.'
        def geturl(self):return 'https://example.org/source'
        def read(self,size=-1):
            part,self.body=self.body[:size],self.body[size:]
            return part
        def close(self):pass
    monkeypatch.setattr(learning,'_public_host',lambda host:None)
    monkeypatch.setattr(module,'build_opener',lambda *args:type('Opener',(),{'open':lambda self,*args,**kwargs:Response()})())
    db.execute("CREATE TRIGGER reject_snapshot BEFORE INSERT ON knowledge_source_snapshots BEGIN SELECT RAISE(ABORT,'injected_snapshot_failure'); END")
    try:
        import pytest
        with pytest.raises(Exception,match='injected_snapshot_failure'):
            learning.capture_url('https://example.org/source')
        assert db.execute('SELECT COUNT(*) FROM knowledge_sources').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM knowledge_source_snapshots').fetchone()[0]==0
        assert db.execute('SELECT status FROM learning_runs').fetchone()[0]=='failed'
    finally:db.close()


def test_source_snapshot_respects_declared_charset(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('m', 'Member')
        text = '流量测量必须保留原始证据。'
        raw = text.encode('gb18030')
        source = service.learning.add_source('web', 'https://example.org/encoding', 'encoding', hashlib.sha256(raw).hexdigest(), owner_member_id='m', metadata={'content_type': 'text/plain; charset=gb18030'})
        service.learning.save_source_snapshot(source, raw, owner_member_id='m')
        page = service.learning.read_source_snapshot(source, owner_member_id='m')
        assert page['content'] == text
        assert page['sha256'] == hashlib.sha256(raw).hexdigest()
        assert page['encoding'] == 'gb18030'
    finally:
        service.close()


def test_source_snapshot_invalid_encoding_never_silently_replaces_bytes(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('m', 'Member')
        raw = b'claim\xffevidence'
        source = service.learning.add_source('web', 'https://example.org/bad-encoding', 'bad encoding', hashlib.sha256(raw).hexdigest(), owner_member_id='m')
        service.learning.save_source_snapshot(source, raw, owner_member_id='m')
        with pytest.raises(ValueError, match='source_snapshot_decode_failed'):
            service.learning.read_source_snapshot(source, owner_member_id='m')
    finally:
        service.close()
