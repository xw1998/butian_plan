import concurrent.futures
import pytest
from butian.runtime.service import ButianService

def test_prior_hypothesis_requires_real_running_action_and_evidence_for_conclusion(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("user", "验证成员")
    prior = service.hypotheses.add_prior("skill", "基础学识", "该拓扑应降低噪声", environment={"board": "sample"}, reliability=.4)
    assessment = service.hypotheses.assess(["skill", "deduction"], environment_match=.8)
    hypothesis = service.hypotheses.propose("hardware_hypothesis", "样机上的该拓扑降低噪声", prior_ids=[prior], rationale="由基础电路知识推导", conditions={"board": "sample"}, predictions=["噪声下降"], falsifiers=["噪声不变"], assessment=assessment)
    action = service.hypotheses.authorize(hypothesis, "测量样机", cost=1, risk=.1, information_gain=.8)
    with pytest.raises(ValueError, match="not_authorized_or_not_queued"):
        service.hypotheses.start(action)
    service.hypotheses.grant_authorization(action, actor_member_id="user", reason="批准验证", authorized=True)
    service.hypotheses.start(action)
    action = service.hypotheses.authorize(hypothesis, "重复测量", cost=1, risk=.1, information_gain=.8)
    service.hypotheses.grant_authorization(action, actor_member_id="user", reason="批准重复测量", authorized=True)
    service.hypotheses.start(action)
    with pytest.raises(ValueError, match="evidence_required"):
        service.hypotheses.observe(action, "supported", [], independent_key="sample-1")
    service.hypotheses.observe(action, "supported", ["ev_real"], independent_key="sample-1")
    assert service.hypotheses.get(hypothesis)["status"] == "supported"
    service.close()

def test_prior_only_is_not_fact_and_conflicting_observations_are_inconclusive(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("user", "验证成员")
    assessment = service.hypotheses.assess(["analogy"], counterexamples=1)
    h = service.hypotheses.propose("causal_hypothesis", "A causes B", rationale="类比推导", assessment=assessment)
    a = service.hypotheses.authorize(h, "对照实验", cost=1, risk=0, information_gain=1)
    service.hypotheses.grant_authorization(a, actor_member_id="user", reason="批准对照", authorized=True)
    service.hypotheses.start(a)
    service.hypotheses.observe(a, "supported", ["ev-1"], independent_key="run-1")
    b = service.hypotheses.authorize(h, "反事实复验", cost=1, risk=0, information_gain=1)
    service.hypotheses.grant_authorization(b, actor_member_id="user", reason="批准复验", authorized=True)
    service.hypotheses.start(b)
    service.hypotheses.observe(b, "rejected", ["ev-2"], independent_key="run-2")
    result = service.hypotheses.get(h)
    assert result["status"] == "inconclusive" and result["prior_reliability"] < 1
    service.close()


def test_external_claim_requires_team_evidence_and_creates_candidate_memory(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("m1", "验证成员")
    evidence = tmp_path / "result.txt"
    evidence.write_text("measured: pass", encoding="utf-8")
    service.store.set_member_token("m1", "secret")
    source_id = service.learning.add_source("paper", "https://example.test/paper", "测试论文", "hash-paper")
    claim_id = service.learning.add_claim(source_id, "该方法在目标环境有效", conditions={"board": "sample"}, confidence=.6)
    promoted = service.promote_claim_to_prior(claim_id)
    assert promoted["status"] == "candidate"
    prior = service.hypotheses.db.execute("SELECT * FROM prior_knowledge WHERE prior_id=?", (promoted["prior_id"],)).fetchone()
    assessment = service.hypotheses.assess([prior["source_kind"]], environment_match=1.0)
    hypothesis = service.hypotheses.propose("practice_hypothesis", "该方法在当前样机有效", prior_ids=[prior["prior_id"]], rationale="外部主张仅作为先验", assessment=assessment)
    action = service.hypotheses.authorize(hypothesis, "样机复测", cost=1, risk=.1, information_gain=.9)
    service.hypotheses.grant_authorization(action, actor_member_id="m1", reason="明确批准复测", authorized=True)
    service.hypotheses.start(action)
    with pytest.raises(ValueError, match="evidence_not_found"):
        service.observe_hypothesis(action, "supported", ["ev_fake"], independent_key="run-1", actor_member_id="m1", member_id="m1")
    evidence_id = service.record_evidence(evidence, owner_member_id="m1", actor_member_id="m1", token="secret")
    result = service.observe_hypothesis(action, "supported", [evidence_id], independent_key="run-1", actor_member_id="m1", member_id="m1", environment={"board": "sample"})
    assert result["memory_status"] == "candidate"
    memory = service.memory("m1")
    try:
        assert memory.get(result["candidate_memory_id"])["status"] == "candidate"
    finally:
        memory.close()
    service.close()


def test_member_memory_outbox_recovers_when_team_observation_exists_but_member_write_is_pending(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("m1", "验证成员")
    prior = service.hypotheses.add_prior("skill", "知识", "该方法有效")
    assessment = service.hypotheses.assess(["skill"])
    hypothesis = service.hypotheses.propose("practice_hypothesis", "该方法在当前环境有效", prior_ids=[prior], rationale="待验证", assessment=assessment)
    action = service.hypotheses.authorize(hypothesis, "验证", cost=1, risk=0, information_gain=1)
    service.hypotheses.grant_authorization(action, actor_member_id="m1", reason="批准", authorized=True)
    service.hypotheses.start(action)
    observation_id = service.hypotheses.observe(action, "supported", ["ev-real"], independent_key="outbox-1")
    service.store.enqueue_member_memory(observation_id, "m1", "mem_hyp_" + observation_id, "practice_hypothesis", "该方法在当前环境有效", {}, [observation_id])
    assert service.store.list_member_memory_outbox(status="pending")
    first_claim = service.store.claim_member_memory_outbox()
    assert len(first_claim) == 1
    assert service.store.claim_member_memory_outbox() == []
    service.store.db.execute("UPDATE member_memory_outbox SET lease_until=0 WHERE outbox_id=?", (first_claim[0]["outbox_id"],))
    result = service.drain_member_memory_outbox()
    assert result[0]["status"] == "committed"
    memory = service.memory("m1")
    try:
        assert memory.get("mem_hyp_" + observation_id)["status"] == "candidate"
    finally:
        memory.close()
    service.close()


def test_member_memory_outbox_is_idempotent_after_crash_after_member_write(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("m1", "验证成员")
    outbox_id = service.store.enqueue_member_memory("obs-crash", "m1", "mem-crash", "practice_hypothesis", "可重试记忆", {}, ["obs-crash"])
    first = service.drain_member_memory_outbox(fail_after_write=True)
    assert first[0]["status"] == "processing"
    memory = service.memory("m1")
    try:
        assert len(memory.list_versions("mem-crash")) == 1
    finally:
        memory.close()
    claimed = service.store.claim_member_memory_outbox()
    assert claimed == []
    service.store.db.execute("UPDATE member_memory_outbox SET lease_until=0 WHERE outbox_id=?", (outbox_id,))
    second = service.drain_member_memory_outbox()
    assert second[0]["status"] == "committed"
    memory = service.memory("m1")
    try:
        assert len(memory.list_versions("mem-crash")) == 1
    finally:
        memory.close()
    assert service.store.list_member_memory_outbox()[0]["outbox_id"] == outbox_id
    service.close()


def test_observation_and_outbox_are_atomic_in_team_database(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("user", "验证成员")
    prior = service.hypotheses.add_prior("deduction", "推理", "待验证")
    assessment = service.hypotheses.assess(["deduction"])
    hypothesis = service.hypotheses.propose("practice_hypothesis", "待验证假设", prior_ids=[prior], rationale="test", assessment=assessment)
    action = service.hypotheses.authorize(hypothesis, "执行", cost=1, risk=0, information_gain=1)
    service.hypotheses.grant_authorization(action, actor_member_id="user", reason="批准", authorized=True)
    service.hypotheses.start(action)
    try:
        service.hypotheses.observe(action, "supported", ["ev-test"], independent_key="atomic-1", outbox={"member_id": "m", "memory_id": "mem-x", "kind": "practice_hypothesis"})
    except KeyError:
        pass
    else:
        raise AssertionError("invalid outbox data should abort the team transaction")
    assert service.store.db.execute("SELECT COUNT(*) FROM hypothesis_observations").fetchone()[0] == 0
    assert service.store.db.execute("SELECT status FROM hypothesis_actions WHERE action_id=?", (action,)).fetchone()[0] == "running"
    assert service.store.db.execute("SELECT COUNT(*) FROM member_memory_outbox").fetchone()[0] == 0
    service.close()


def test_hypothesis_resources_are_member_scoped_across_full_action_chain(tmp_path):
    service = ButianService(tmp_path)
    service.add_member("m1", "成员一")
    service.add_member("m2", "成员二")
    prior = service.hypotheses.add_prior("skill", "m1知识", "仅属于m1", owner_member_id="m1")
    assessment = service.hypotheses.assess(["skill"])
    hypothesis = service.hypotheses.propose("practice_hypothesis", "m1假设", prior_ids=[prior], rationale="隔离回归", assessment=assessment, owner_member_id="m1")
    with pytest.raises(ValueError, match="not_found"):
        service.get_hypothesis(hypothesis, owner_member_id="m2")
    with pytest.raises(ValueError, match="not_found"):
        service.authorize_hypothesis(hypothesis, "越权动作", cost=1, risk=0, information_gain=1, owner_member_id="m2")
    action = service.authorize_hypothesis(hypothesis, "m1验证", cost=1, risk=0, information_gain=1, owner_member_id="m1")
    with pytest.raises(ValueError, match="not_found"):
        service.grant_hypothesis_authorization(action, actor_member_id="m2", reason="越权", authorized=True, owner_member_id="m2")
    service.grant_hypothesis_authorization(action, actor_member_id="m1", reason="批准", authorized=True, owner_member_id="m1")
    with pytest.raises(ValueError, match="not_found"):
        service.start_hypothesis_action(action, owner_member_id="m2")
    service.start_hypothesis_action(action, owner_member_id="m1")
    with pytest.raises(ValueError, match="not_found"):
        service.observe_hypothesis(action, "inconclusive", [], independent_key="m2", actor_member_id="m2", owner_member_id="m2")
    service.observe_hypothesis(action, "inconclusive", [], independent_key="m1", actor_member_id="m1", owner_member_id="m1")
    service.close()


def test_external_claim_prepare_is_concurrent_idempotent(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("m1", "验证成员")
        source_id = service.learning.add_source("paper", "https://example.test/paper", "测试论文", "hash-paper", owner_member_id="m1")
        claim_id = service.learning.add_claim(source_id, "并发主张", owner_member_id="m1")
        def prepare():
            return service.prepare_claim_practice(claim_id, owner_member_id="m1", description="并发复测")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: prepare(), range(4)))
        assert len({item["action_id"] for item in results}) == 1
        assert service.hypotheses.db.execute("SELECT COUNT(*) FROM claim_practice_preparations").fetchone()[0] == 1
        assert service.hypotheses.db.execute("SELECT COUNT(*) FROM hypothesis_actions").fetchone()[0] == 1
    finally:
        service.close()


def test_external_claim_prepares_idempotent_awaiting_action_without_execution(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("m1", "验证成员")
        source_id = service.learning.add_source("paper", "https://example.test/paper", "测试论文", "hash-paper", owner_member_id="m1")
        claim_id = service.learning.add_claim(source_id, "该方法在目标环境有效", owner_member_id="m1", conditions={"board": "sample"}, confidence=0.0)
        first = service.prepare_claim_practice(claim_id, owner_member_id="m1", description="在样机上复测", kind="practice_hypothesis")
        assert first["status"] == "awaiting_authorization"
        assert first["claim_id"] == claim_id
        assert service.hypotheses.get(first["hypothesis_id"], owner_member_id="m1")["status"] == "candidate"
        action = service.hypotheses.db.execute("SELECT status FROM hypothesis_actions WHERE action_id=?", (first["action_id"],)).fetchone()
        assert action["status"] == "awaiting_authorization"
        second = service.prepare_claim_practice(claim_id, owner_member_id="m1", description="在样机上复测", kind="practice_hypothesis")
        assert {key: second[key] for key in ("claim_id", "prior_id", "hypothesis_id", "action_id", "status")} == {key: first[key] for key in ("claim_id", "prior_id", "hypothesis_id", "action_id", "status")}
        assert second["idempotent"] is True
        assert service.hypotheses.db.execute("SELECT COUNT(*) FROM prior_knowledge").fetchone()[0] == 1
        assert service.hypotheses.db.execute("SELECT COUNT(*) FROM practice_hypotheses").fetchone()[0] == 1
        assert service.hypotheses.db.execute("SELECT COUNT(*) FROM hypothesis_actions").fetchone()[0] == 1
    finally:
        service.close()


def test_external_claim_prepare_failure_leaves_no_partial_resources(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member("m1", "验证成员")
        source_id = service.learning.add_source("paper", "https://example.test/failure", "失败论文", "hash-failure", owner_member_id="m1")
        claim_id = service.learning.add_claim(source_id, "必须完整回滚", owner_member_id="m1")
        with pytest.raises(ValueError, match="invalid_verification_metrics"):
            service.prepare_claim_practice(claim_id, owner_member_id="m1", description="坏参数", risk=2.0)
        assert service.hypotheses.db.execute("SELECT COUNT(*) FROM prior_knowledge WHERE owner_member_id='m1'").fetchone()[0] == 0
        assert service.hypotheses.db.execute("SELECT COUNT(*) FROM practice_hypotheses WHERE owner_member_id='m1'").fetchone()[0] == 0
        assert service.hypotheses.db.execute("SELECT COUNT(*) FROM hypothesis_actions WHERE owner_member_id='m1'").fetchone()[0] == 0
        assert service.hypotheses.db.execute("SELECT COUNT(*) FROM claim_practice_preparations WHERE owner_member_id='m1'").fetchone()[0] == 0
    finally:
        service.close()


def test_claim_preparation_failure_rolls_back_all_team_rows(tmp_path, monkeypatch):
    import pytest
    from butian.runtime.service import ButianService
    service = ButianService(tmp_path)
    try:
        service.add_member('m', 'M')
        sid = service.learning.add_source('manual', 'local://proof', 'Claim source', 'a' * 64, owner_member_id='m')
        cid = service.learning.add_claim(sid, 'check input', owner_member_id='m')
        original = service.hypotheses.authorize
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('prepare_interrupted')
        monkeypatch.setattr(service.hypotheses, 'authorize', interrupted)
        with pytest.raises(RuntimeError, match='prepare_interrupted'):
            service.prepare_claim_practice(cid, owner_member_id='m', description='execute input check')
        for table in ('prior_knowledge', 'practice_hypotheses', 'hypothesis_actions', 'claim_practice_preparations'):
            assert service.store.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    finally:
        service.close()

@pytest.mark.parametrize('field', ['cost', 'risk', 'information_gain'])
@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_verification_metrics_must_be_finite(tmp_path, field, value):
    service = ButianService(tmp_path)
    try:
        assessment = service.hypotheses.assess(['skill'])
        hypothesis = service.hypotheses.propose('practice_hypothesis', 'finite metrics', rationale='validation', assessment=assessment)
        metrics = dict(cost=1.0, risk=0.1, information_gain=1.0)
        metrics[field] = value
        with pytest.raises(ValueError, match='invalid_verification_metrics'):
            service.hypotheses.authorize(hypothesis, 'verify', **metrics)
        assert service.store.db.execute('SELECT COUNT(*) FROM hypothesis_actions').fetchone()[0] == 0
    finally:
        service.close()

def test_authorization_failure_rolls_back_ledger(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('m', 'M')
        assessment = service.hypotheses.assess(['skill'])
        h = service.hypotheses.propose('practice_hypothesis', 'atomic authorization', rationale='check', assessment=assessment)
        a = service.hypotheses.authorize(h, 'verify', cost=1, risk=0, information_gain=1)
        service.store.db.execute("CREATE TRIGGER fail_authorization_update BEFORE UPDATE ON hypothesis_actions BEGIN SELECT RAISE(ABORT, 'authorization_interrupted'); END")
        with pytest.raises(Exception, match='authorization_interrupted'):
            service.hypotheses.grant_authorization(a, actor_member_id='m', reason='approve', authorized=True)
        assert service.store.db.execute('SELECT COUNT(*) FROM hypothesis_authorizations').fetchone()[0] == 0
        assert service.store.db.execute('SELECT status FROM hypothesis_actions WHERE action_id=?', (a,)).fetchone()[0] == 'awaiting_authorization'
    finally:
        service.close()

def test_hypothesis_support_and_counterevidence_update_stable_memory(tmp_path):
    service = ButianService(tmp_path)
    try:
        service.add_member('m', 'M')
        h = service.hypotheses.propose('practice_hypothesis', 'method works under measured conditions', rationale='verify', assessment=service.hypotheses.assess(['skill']))
        memory_ids = []
        for i, outcome in enumerate(('supported', 'supported', 'rejected')):
            a = service.hypotheses.authorize(h, 'experiment', cost=1, risk=0, information_gain=1)
            service.hypotheses.grant_authorization(a, actor_member_id='m', reason='approved experiment', authorized=True)
            service.hypotheses.start(a)
            proof = tmp_path / f'proof-{i}.txt'
            proof.write_text(f'experiment {i}: {outcome}', encoding='utf-8')
            eid = service.store.record_evidence(proof, owner_member_id='m', actor_member_id='m')
            result = service.observe_hypothesis(a, outcome, [eid], independent_key=f'run-{i}', actor_member_id='m', member_id='m')
            memory_ids.append(result['candidate_memory_id'])
            assert result['memory_status'] == ('candidate', 'warm', 'disputed')[i]
        assert len(set(memory_ids)) == 1
        memory = service.memory('m')
        try:
            assert memory.db.execute('SELECT COUNT(*) FROM practice_observations WHERE memory_id=?', (memory_ids[0],)).fetchone()[0] == 3
        finally:
            memory.close()
    finally:
        service.close()

def test_stable_hypothesis_member_failure_recovers_once(tmp_path, monkeypatch):
    from butian.runtime.memory import MemoryStore
    service = ButianService(tmp_path)
    service.add_member('m', 'M')
    h = service.hypotheses.propose('practice_hypothesis', 'recover measured method', rationale='verify', assessment=service.hypotheses.assess(['skill']))
    a = service.hypotheses.authorize(h, 'experiment', cost=1, risk=0, information_gain=1)
    service.hypotheses.grant_authorization(a, actor_member_id='m', reason='approved', authorized=True)
    service.hypotheses.start(a)
    p = tmp_path / 'recovery-proof.txt'
    p.write_text('actual recovery experiment', encoding='utf-8')
    eid = service.store.record_evidence(p, owner_member_id='m', actor_member_id='m')
    original = MemoryStore.record_practice_outcome
    def interrupted(*args, **kwargs):
        raise RuntimeError('member_interrupted')
    monkeypatch.setattr(MemoryStore, 'record_practice_outcome', interrupted)
    result = service.observe_hypothesis(a, 'supported', [eid], independent_key='recover', actor_member_id='m', member_id='m')
    assert result['memory_status'] == 'pending'
    service.store.db.execute("UPDATE member_memory_outbox SET lease_until=0 WHERE status='processing'")
    service.close()
    monkeypatch.setattr(MemoryStore, 'record_practice_outcome', original)
    service = ButianService(tmp_path)
    try:
        service.drain_member_memory_outbox()
        memory = service.memory('m')
        try:
            assert memory.get(result['candidate_memory_id'])['status'] == 'candidate'
            assert memory.db.execute('SELECT COUNT(*) FROM practice_observations').fetchone()[0] == 1
        finally:
            memory.close()
        assert service.store.list_member_memory_outbox()[0]['status'] == 'committed'
    finally:
        service.close()
