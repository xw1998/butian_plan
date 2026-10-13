from butian.runtime.service import ButianService


def test_peer_answer_is_candidate_until_reproduced(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("a", "失败成员")
    service.add_member("b", "成功成员")
    learning_id = service.ask_peer("a", "b", "你是怎么通过测试的？", task_id="task-x", comparison={"asker_result": "failed", "expert_result": "passed"})
    result = service.answer_peer(learning_id, "先检查输入格式", method="先做最小复现")
    assert result["status"] == "candidate"
    memory = service.memory("a")
    assert memory.search("输入格式")[0]["status"] == "candidate"
    memory.close()
    evidence = tmp_path / "verify.txt"
    evidence.write_text("reproduced", encoding="utf-8")
    evidence_id = service.store.record_evidence(evidence)
    verified = service.verify_peer_learning(learning_id, success=True, evidence_ids=[evidence_id], notes="在同版本环境复现成功")
    assert verified["status"] == "verified"
    memory = service.memory("a")
    assert memory.search("输入格式")[0]["status"] == "candidate"
    memory.close()
    second_evidence = tmp_path / "verify-second.txt"
    second_evidence.write_text("independent reproduction", encoding="utf-8")
    second_evidence_id = service.store.record_evidence(second_evidence)
    second = service.verify_peer_learning(learning_id, success=True, evidence_ids=[second_evidence_id], notes="第二次独立复现")
    assert second["memory_status"] == "warm"
    memory = service.memory("a")
    assert memory.search("输入格式")[0]["status"] == "warm"
    memory.close()
    service.close()


def test_peer_learning_rejects_self_question(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("a", "成员")
    try:
        service.ask_peer("a", "a", "为什么？")
    except ValueError as exc:
        assert str(exc) == "peer_learning_requires_two_members"
    else:
        raise AssertionError("self learning question must be rejected")
    service.close()

def test_peer_counterevidence_disputes_warm_memory(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('a', 'A')
        service.add_member('b', 'B')
        learning_id = service.ask_peer('a', 'b', 'method?')
        result = service.answer_peer(learning_id, 'method', method='check input')
        ids = []
        for index in range(3):
            path = tmp_path / f'proof-{index}.txt'
            path.write_text(f'independent run {index}', encoding='utf-8')
            ids.append(service.store.record_evidence(path))
        service.verify_peer_learning(learning_id, success=True, evidence_ids=[ids[0]])
        service.verify_peer_learning(learning_id, success=True, evidence_ids=[ids[1]])
        rejected = service.verify_peer_learning(learning_id, success=False, evidence_ids=[ids[2]])
        assert rejected['memory_status'] == 'disputed'
        import pytest
        with pytest.raises(ValueError, match='independent_evidence_required'):
            service.verify_peer_learning(learning_id, success=True, evidence_ids=[ids[0]])
    finally:
        service.close()


def test_peer_same_content_different_ids_cannot_strengthen(tmp_path):
    import pytest
    service = ButianService(tmp_path)
    try:
        service.add_member('a', 'A')
        service.add_member('b', 'B')
        lid = service.ask_peer('a', 'b', 'method?')
        service.answer_peer(lid, 'check', method='check input')
        ids = []
        for index in range(2):
            path = tmp_path / f'copy-{index}.txt'
            path.write_text('one execution result', encoding='utf-8')
            ids.append(service.store.record_evidence(path))
        service.verify_peer_learning(lid, success=True, evidence_ids=[ids[0]])
        with pytest.raises(ValueError, match='independent_evidence_required'):
            service.verify_peer_learning(lid, success=True, evidence_ids=[ids[1]])
    finally:
        service.close()


def test_peer_counterevidence_version_survives_restart(tmp_path):
    import pytest
    service = ButianService(tmp_path)
    try:
        service.add_member('a', 'A')
        service.add_member('b', 'B')
        lid = service.ask_peer('a', 'b', 'method?')
        mid = service.answer_peer(lid, 'check input', method='check')['candidate_memory_id']
        proofs = []
        for index in range(3):
            p = tmp_path / f'version-proof-{index}.txt'
            p.write_text(f'run {index}', encoding='utf-8')
            proofs.append(service.store.record_evidence(p))
        for eid in proofs[:2]:
            service.verify_peer_learning(lid, success=True, evidence_ids=[eid])
        service.verify_peer_learning(lid, success=False, evidence_ids=[proofs[2]])
        with pytest.raises(ValueError, match='duplicate_peer_verification'):
            service.verify_peer_learning(lid, success=False, evidence_ids=[proofs[2]])
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        memory = service.memory('a')
        try:
            assert memory.get(mid)['status'] == 'disputed'
            assert memory.list_versions(mid)[-1]['change_reason'] == 'peer_counterevidence'
            assert memory.db.execute("SELECT COUNT(*) FROM memory_events WHERE memory_id=? AND event_type='verification_failure'", (mid,)).fetchone()[0] == 1
        finally:
            memory.close()
    finally:
        service.close()


def test_peer_verification_recovers_after_member_write_failure(tmp_path, monkeypatch):
    import pytest
    from butian.runtime.memory import MemoryStore
    service = ButianService(tmp_path)
    service.add_member('a', 'A')
    service.add_member('b', 'B')
    lid = service.ask_peer('a', 'b', 'method?')
    mid = service.answer_peer(lid, 'check', method='check')['candidate_memory_id']
    path = tmp_path / 'crash-proof.txt'
    path.write_text('real reproduction', encoding='utf-8')
    eid = service.store.record_evidence(path)
    original = MemoryStore.put
    def interrupted(self, *args, **kwargs):
        if kwargs.get('change_reason') == 'peer_verification_success':
            raise RuntimeError('member_write_interrupted')
        return original(self, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(MemoryStore, 'put', interrupted)
        with pytest.raises(RuntimeError, match='member_write_interrupted'):
            service.verify_peer_learning(lid, success=True, evidence_ids=[eid])
    service.close()
    service = ButianService(tmp_path)
    try:
        memory = service.memory('a')
        try:
            assert len(memory.list_versions(mid)) == 2
            assert memory.db.execute("SELECT COUNT(*) FROM memory_events WHERE memory_id=? AND event_type='verification_success'", (mid,)).fetchone()[0] == 1
            assert service.list_peer_learning()[0]['status'] == 'verified'
        finally:
            memory.close()
    finally:
        service.close()


def test_peer_member_commit_before_team_projection_is_idempotent(tmp_path, monkeypatch):
    import pytest
    from butian.runtime.memory import MemoryStore
    service = ButianService(tmp_path)
    service.add_member('a', 'A')
    service.add_member('b', 'B')
    lid = service.ask_peer('a', 'b', 'method?')
    mid = service.answer_peer(lid, 'check', method='check')['candidate_memory_id']
    path = tmp_path / 'projection-proof.txt'
    path.write_text('reproduced once', encoding='utf-8')
    eid = service.store.record_evidence(path)
    original = MemoryStore.put
    def committed_then_interrupted(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if kwargs.get('change_reason') == 'peer_verification_success':
            self.db.commit()
            raise RuntimeError('after_member_commit')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(MemoryStore, 'put', committed_then_interrupted)
        with pytest.raises(RuntimeError, match='after_member_commit'):
            service.verify_peer_learning(lid, success=True, evidence_ids=[eid])
    service.close()
    service = ButianService(tmp_path)
    try:
        service.recover_peer_verifications()
        memory = service.memory('a')
        try:
            assert len(memory.list_versions(mid)) == 2
            assert memory.db.execute("SELECT COUNT(*) FROM memory_events WHERE memory_id=? AND event_type='verification_success'", (mid,)).fetchone()[0] == 1
            assert service.list_peer_learning()[0]['status'] == 'verified'
        finally:
            memory.close()
    finally:
        service.close()


def test_peer_two_processes_same_content_register_once(tmp_path):
    import subprocess
    import sys
    import json
    service = ButianService(tmp_path)
    service.add_member('a', 'A')
    service.add_member('b', 'B')
    lid = service.ask_peer('a', 'b', 'method?')
    mid = service.answer_peer(lid, 'check', method='check')['candidate_memory_id']
    ids = []
    for index in range(2):
        path = tmp_path / f'parallel-{index}.txt'
        path.write_text('same real execution', encoding='utf-8')
        ids.append(service.store.record_evidence(path))
    service.close()
    code = '''import sys,json
from butian.runtime.service import ButianService
s=ButianService(sys.argv[1])
try:
    try:
        s.verify_peer_learning(sys.argv[2],success=True,evidence_ids=[sys.argv[3]])
        print(json.dumps({'status':'accepted'}))
    except ValueError as exc:
        print(json.dumps({'status':str(exc)}))
finally:
    s.close()
'''
    processes = [subprocess.Popen([sys.executable, '-c', code, str(tmp_path), lid, eid], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for eid in ids]
    outcomes = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stderr
            outcomes.append(json.loads(stdout)['status'])
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert sorted(outcomes) == ['accepted', 'independent_evidence_required']
    service = ButianService(tmp_path)
    try:
        assert service.store.db.execute('SELECT COUNT(*) FROM memory_verifications WHERE memory_id=?', (mid,)).fetchone()[0] == 1
        memory = service.memory('a')
        try:
            assert len(memory.list_versions(mid)) == 2
            assert memory.get(mid)['status'] == 'candidate'
        finally:
            memory.close()
    finally:
        service.close()

def test_legacy_random_peer_event_is_not_reapplied(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('a', 'A')
        service.add_member('b', 'B')
        lid = service.ask_peer('a', 'b', 'method?')
        mid = service.answer_peer(lid, 'check', method='input')['candidate_memory_id']
        p = tmp_path / 'legacy-proof.txt'
        p.write_text('legacy measured result', encoding='utf-8')
        eid = service.store.record_evidence(p)
        service.verify_peer_learning(lid, success=True, evidence_ids=[eid])
        memory = service.memory('a')
        try:
            memory.db.execute("UPDATE memory_events SET event_id='legacy_random_id' WHERE memory_id=? AND event_type='verification_success'", (mid,))
            versions = len(memory.list_versions(mid))
            weight = memory.get(mid)['weight']
        finally:
            memory.close()
    finally:
        service.close()
    service = ButianService(tmp_path)
    try:
        memory = service.memory('a')
        try:
            assert len(memory.list_versions(mid)) == versions
            assert memory.get(mid)['weight'] == weight
            assert memory.db.execute("SELECT COUNT(*) FROM memory_events WHERE event_id LIKE 'peer_%' AND memory_id=?", (mid,)).fetchone()[0] == 1
        finally:
            memory.close()
    finally:
        service.close()

def test_legacy_peer_event_without_completed_version_recovers_once(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('a', 'A')
        service.add_member('b', 'B')
        lid = service.ask_peer('a', 'b', 'method?')
        mid = service.answer_peer(lid, 'check input')['candidate_memory_id']
        p = tmp_path/'legacy-partial.txt'
        p.write_text('actual partial legacy execution', encoding='utf-8')
        eid = service.store.record_evidence(p)
        service.verify_peer_learning(lid, success=True, evidence_ids=[eid])
        memory = service.memory('a')
        try:
            versions = memory.list_versions(mid)
            candidate = versions[0]
            memory.db.execute("UPDATE memory_events SET event_id='legacy_partial' WHERE memory_id=? AND event_id LIKE 'peer_%'", (mid,))
            memory.db.execute("DELETE FROM memory_versions WHERE memory_id=? AND change_reason='peer_verification_success'", (mid,))
            memory.db.execute("UPDATE memories SET status='candidate',weight=0.2,confidence=0.2,source_event_ids=? WHERE memory_id=?", (candidate['evidence_refs'],mid))
        finally:
            memory.close()
    finally:
        service.close()
    for _ in range(2):
        service = ButianService(tmp_path)
        try:
            memory = service.memory('a')
            try:
                assert len(memory.list_versions(mid)) == 2
                assert memory.get(mid)['weight'] == 0.7
                assert memory.db.execute("SELECT COUNT(*) FROM memory_events WHERE memory_id=? AND event_type='verification_success'", (mid,)).fetchone()[0] == 1
            finally:
                memory.close()
        finally:
            service.close()
