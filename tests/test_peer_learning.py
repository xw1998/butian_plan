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
    verified = service.verify_peer_learning(learning_id, success=True, notes="在同版本环境复现成功")
    assert verified["status"] == "verified"
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
