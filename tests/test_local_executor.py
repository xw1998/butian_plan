"""Local execution runs in this process: no connection file, no pairing key, no loopback port.

The durable answer to "did it run, with what output" stays; what disappears is the
transport that only exists for an executor on another machine.
"""
from __future__ import annotations

import pytest

from butian.runtime.service import ButianService

MANIFEST = {
    "input_contract": {"type": "object"},
    "output_contract": {"type": "text"},
    "permissions": ["execute"],
    "failure_conditions": ["invalid_input"],
    "entrypoint": ["python", "tool.py"],
}

def make_service(tmp_path, **kwargs):
    service = ButianService(tmp_path, **kwargs)
    service.add_member("m1", "本地作者")
    return service

def run_tool(service, source="print('hello from local')"):
    tool_id = service.propose_tool("m1", "本地工具", "在本机进程内执行",
                                   reuse_lower=3, manual_cost_upper=10, call_cost_lower=1,
                                   build_cost_upper=5, maintenance_cost_upper=2, risk_cost_upper=1)["tool_id"]
    service.add_tool_version("m1", tool_id, source, MANIFEST)
    session = service.store.create_session("solo", "m1", None, {})
    task = service.store.create_task(session, "本地执行任务", assignee_id="m1")
    result = service.execute_tool("m1", tool_id, 1, {}, task_id=task, verification_only=True)
    return tool_id, result

def test_local_executor_runs_a_tool_in_process_without_pairing_artifacts(tmp_path):
    service = make_service(tmp_path, local_executor=True)
    try:
        tool_id, result = run_tool(service)
        assert result["status"] == "success", result
        assert result["stdout"].strip() == "hello from local"
        assert result["termination_reason"] is None
        # 本机不需要连接文件、配对密钥，也没有回环端点要维护。
        assert not (tmp_path / "connection.json").exists()
        assert not (tmp_path / "pairing.bin").exists()
        assert not (tmp_path / "executor_clients").exists()
        # 账本仍然在：审计能查到这次执行与它的终态。
        audit = service.list_tool_executions("m1", tool_id=tool_id)
        assert audit[0]["status"] == "success"
    finally:
        service.close()

def test_local_executor_is_lazy_until_something_actually_runs(tmp_path):
    service = make_service(tmp_path, local_executor=True)
    try:
        # 自检要真起子进程；没有执行请求的进程不该付这笔启动成本。
        assert service.execution_runner is None
        assert not (tmp_path / "executor_self_test").exists()
        tool_id, result = run_tool(service)
        assert service.execution_runner is not None
        assert service.tools.execution_runner is service.execution_runner
    finally:
        service.close()

def test_local_executor_stays_off_when_not_requested(tmp_path):
    service = make_service(tmp_path)
    try:
        assert service.execution_runner is None
        assert service.tools.execution_runner_factory is None
        with pytest.raises(ValueError, match="execution_backend_required"):
            run_tool(service)
    finally:
        service.close()

def test_failed_self_test_refuses_to_execute_instead_of_degrading(tmp_path, monkeypatch):
    import butian.runtime.local_executor as module
    monkeypatch.setattr(module, "verify_process_control", lambda owner, root: {"verified": False})
    service = make_service(tmp_path, local_executor=True)
    try:
        with pytest.raises(ValueError, match="local_executor_self_test_failed"):
            run_tool(service)
        assert service.execution_runner is None
    finally:
        service.close()
