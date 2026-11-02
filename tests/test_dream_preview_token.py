"""两段式预览：先看清影响再落盘；预览之后源记忆变了必须拒绝写入。"""

import pytest

from butian.runtime.mcp_guidance import ToolError
from butian.runtime.service import ButianService
from butian.mcp_server import call_tool


AUTH = {"actor_member_id": "a", "token": "secret"}


def _seed(service, member_id, count=3):
    memory = service.memory(member_id)
    ids = []
    try:
        for index in range(count):
            memory_id = f"{member_id}-{len(ids)}-{index}"
            memory.put(memory_id, "experience", f"{member_id} observation {index}", status="candidate")
            ids.append(memory_id)
    finally:
        memory.close()
    return ids


def _count(service, member_id, table):
    memory = service.memory(member_id)
    try:
        return memory.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        memory.close()


def test_preview_writes_nothing_and_returns_a_token(tmp_path, monkeypatch):
    monkeypatch.setenv("BUTIAN_MAINTENANCE_ENABLED", "0")
    service = ButianService(tmp_path)
    try:
        service.add_member("a", "A")
        service.store.set_member_token("a", "secret")
        ids = _seed(service, "a")
        preview = call_tool(service, "butian_dream_distill",
                            {**AUTH, "member_id": "a", "memory_ids": ids, "summary": "合并三条观察",
                             "preview_only": True})
        assert preview["status"] == "preview"
        assert preview["operation"] == "distill"
        assert preview["affected"] == 3
        assert preview["preview_token"].startswith("pt_")
        assert preview["writes"] == [{"table": "memory_distillations", "rows": 1}]
        assert preview["existing"] is None
        assert {item["memory_id"] for item in preview["source_states"]} == set(ids)
        # 预览是只读的：一条记录都不许写。
        assert _count(service, "a", "memory_distillations") == 0
    finally:
        service.close()


def test_commit_with_matching_token_writes_once(tmp_path, monkeypatch):
    monkeypatch.setenv("BUTIAN_MAINTENANCE_ENABLED", "0")
    service = ButianService(tmp_path)
    try:
        service.add_member("a", "A")
        service.store.set_member_token("a", "secret")
        ids = _seed(service, "a")
        args = {**AUTH, "member_id": "a", "memory_ids": ids, "summary": "合并三条观察"}
        token = call_tool(service, "butian_dream_distill", dict(args, preview_only=True))["preview_token"]
        result = call_tool(service, "butian_dream_distill", dict(args, preview_token=token))
        assert result["status"] == "candidate"
        assert _count(service, "a", "memory_distillations") == 1
        # 同内容再预览会看到已有记录，写 0 行（不会重复落一条）。
        again = call_tool(service, "butian_dream_distill", dict(args, preview_only=True))
        assert again["writes"] == [{"table": "memory_distillations", "rows": 0}]
        assert again["existing"]["distillation_id"] == result["distillation_id"]
    finally:
        service.close()


def test_stale_token_is_refused_after_source_memory_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("BUTIAN_MAINTENANCE_ENABLED", "0")
    service = ButianService(tmp_path)
    try:
        service.add_member("a", "A")
        service.store.set_member_token("a", "secret")
        ids = _seed(service, "a")
        args = {**AUTH, "member_id": "a", "memory_ids": ids, "summary": "合并三条观察"}
        token = call_tool(service, "butian_dream_distill", dict(args, preview_only=True))["preview_token"]
        memory = service.memory("a")
        try:
            memory.put(ids[0], "experience", "正文被改过了", status="candidate")
        finally:
            memory.close()
        with pytest.raises(ToolError) as info:
            call_tool(service, "butian_dream_distill", dict(args, preview_token=token))
        details = info.value.details
        assert details["code"] == "preview_stale"
        assert details["missing_conditions"] == ["preview_token_matches_current_state"]
        assert details["next_actions"][0]["tool"] == "butian_dream_distill"
        assert details["next_actions"][0]["arguments"]["preview_only"] is True
        assert _count(service, "a", "memory_distillations") == 0
    finally:
        service.close()


def test_env_gate_requires_preview_but_stays_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("BUTIAN_MAINTENANCE_ENABLED", "0")
    service = ButianService(tmp_path)
    try:
        service.add_member("a", "A")
        service.store.set_member_token("a", "secret")
        ids = _seed(service, "a")
        args = {**AUTH, "member_id": "a", "memory_ids": ids, "summary": "合并三条观察"}
        # 默认关：不带令牌也能写（老行为一字不变）。
        assert call_tool(service, "butian_dream_distill", args)["status"] == "candidate"
        monkeypatch.setenv("BUTIAN_REQUIRE_DREAM_PREVIEW", "1")
        with pytest.raises(ToolError) as info:
            call_tool(service, "butian_dream_distill",
                      {**AUTH, "member_id": "a", "memory_ids": ids, "summary": "另一份摘要"})
        details = info.value.details
        assert details["code"] == "preview_required"
        assert details["missing_conditions"] == ["preview_token_from_preview_only_call"]
        assert details["next_actions"]
    finally:
        service.close()


def test_abstract_preview_and_stale_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("BUTIAN_MAINTENANCE_ENABLED", "0")
    service = ButianService(tmp_path)
    try:
        service.add_member("a", "A")
        service.store.set_member_token("a", "secret")
        ids = _seed(service, "a")
        args = {**AUTH, "member_id": "a", "memory_ids": ids, "pattern": "同一个成员反复观测同一现象"}
        preview = call_tool(service, "butian_dream_abstract", dict(args, preview_only=True))
        assert preview["operation"] == "abstract"
        assert preview["supports"] == ids
        assert _count(service, "a", "memory_abstractions") == 0
        written = call_tool(service, "butian_dream_abstract",
                            dict(args, preview_token=preview["preview_token"]))
        assert written["status"] == "candidate"
        assert _count(service, "a", "memory_abstractions") == 1
        with pytest.raises(ToolError) as info:
            call_tool(service, "butian_dream_abstract",
                      dict(args, preview_token=preview["preview_token"], pattern="换了一个模式"))
        assert info.value.details["code"] == "preview_stale"
    finally:
        service.close()
