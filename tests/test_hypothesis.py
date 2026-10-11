import pytest
from butian.runtime.service import ButianService

def test_prior_hypothesis_requires_real_running_action_and_evidence_for_conclusion(tmp_path):
    service = ButianService(tmp_path)
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
