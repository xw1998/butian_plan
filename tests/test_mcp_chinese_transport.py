"""中文 MCP 传输与工具面：GBK 宿主控制台下也要能读中文、能跑完整闭环。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from butian.runtime.service import ButianService
from butian.runtime.stdio_encoding import frame_bytes, repair_surrogates, repair_text

ROOT = Path(__file__).resolve().parents[1]


def test_gbk_host_console_still_speaks_utf8_and_finishes_a_chinese_task(tmp_path):
    """宿主用本地代码页(gbk)启动 MCP 时，中文仍要能提交，且任务能一路走到审核。

    以前 stdio 继承 cp936+surrogateescape：成员名与标题变乱码，回传的孤立代理
    字符再让 `.encode("utf-8")` 抛 "surrogates not allowed"，中文一提交就失败。
    """
    home = tmp_path / "gbk-home"
    service = ButianService(home)
    service.store.set_member_token("xu_wei", "gbk-token")
    service.close()
    evidence = home / "中文证据.txt"
    evidence.write_text("实测：标定 12.5", encoding="utf-8")

    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONUTF8", "PYTHONIOENCODING"}}
    env.update({"BUTIAN_HOME": str(home), "PYTHONIOENCODING": "gbk"})
    proc = subprocess.Popen([sys.executable, "-m", "butian.cli", "mcp"], cwd=str(ROOT), env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    auth = {"member_id": "xu_wei", "actor_member_id": "xu_wei", "token": "gbk-token"}
    counter = {"n": 0}

    def request(method, params=None):
        counter["n"] += 1
        payload = {"jsonrpc": "2.0", "id": counter["n"], "method": method}
        if params is not None:
            payload["params"] = params
        proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        proc.stdin.flush()
        raw = proc.stdout.readline()
        assert raw, proc.stderr.read().decode("utf-8", "replace")
        return json.loads(raw.decode("utf-8"))  # 必须真的是 UTF-8

    def call(name, **arguments):
        reply = request("tools/call", {"name": name, "arguments": dict(auth, **arguments)})
        result = reply["result"]
        assert result["isError"] is False, result
        return json.loads(result["content"][0]["text"])

    try:
        request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "gbk-host", "version": "1"}})
        tools = request("tools/list")["result"]["tools"]
        names = {tool["name"] for tool in tools}
        # 只读第一页也要能看到推进与收口的工具。
        assert {"butian_task_claim", "butian_task_transition", "butian_task_review",
                "butian_execution_submit", "butian_context_compose", "butian_evidence_record",
                "butian_task_handoff"} <= names

        call("butian_connect")
        goal = call("butian_goal_submit", goal="把中文实验记录整理成可复核结论",
                    acceptance_criteria=["结论可复核"])
        task_id = goal["task_id"]
        assert goal["created"] is True
        assert goal["problem"]["goal"] == "把中文实验记录整理成可复核结论"
        assert [action["tool"] for action in goal["next_actions"]][:1] == ["butian_task_transition"]

        # 重复提交不再产生第二条任务。
        again = call("butian_goal_submit", goal="把中文实验记录整理成可复核结论")
        assert again["task_id"] == task_id and again["created"] is False

        def current():
            return next(task for task in call("butian_task_list")["tasks"] if task["task_id"] == task_id)

        call("butian_task_transition", task_id=task_id, expected_revision=current()["revision"],
             target="ready", operation_key="gbk-ready")
        call("butian_task_claim", task_id=task_id)
        call("butian_task_transition", task_id=task_id, expected_revision=current()["revision"],
             target="running", operation_key="gbk-running")
        composed = call("butian_context_compose", instruction="整理中文记录", task_id=task_id)
        assert composed["context"]
        evidence_id = call("butian_evidence_record", path=str(evidence))["evidence_id"]
        call("butian_task_transition", task_id=task_id, expected_revision=current()["revision"],
             target="review", operation_key="gbk-review")
        # 这一句正是以前报 "surrogates not allowed" 的提交动作。
        call("butian_task_handoff", task_id=task_id, task_revision=current()["revision"],
             status="done", summary="按验收标准整理完成，含中文实测数据",
             evidence_ids=[evidence_id], next_action="审核内容与证据", confidence=0.8)
        assert current()["status"] == "review"
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        proc.stdout.close()
        proc.stderr.close()


def test_lone_surrogates_are_repaired_and_never_break_output():
    """宿主把我们的 UTF-8 中文按别的编码解过再回传时，能还原成原文而不是崩掉。"""
    original = "许伟：标定"
    # 宿主按 ascii/latin-1 解字节时，非 ASCII 字节全部落成代理字符——这种损伤可逆。
    mangled = original.encode("utf-8").decode("ascii", "surrogateescape")
    assert any(0xD800 <= ord(ch) <= 0xDFFF for ch in mangled)
    assert repair_text(mangled) == original
    assert repair_surrogates({"name": mangled, "list": [mangled]}) == {"name": original, "list": [original]}

    # 真修不回来的孤立代理字符也不能让整条响应消失：退回 ASCII 转义，仍是合法 JSON。
    frame = frame_bytes({"text": "\udcff"})
    assert json.loads(frame.decode("ascii"))["text"] == "\udcff"


def test_cli_emit_writes_utf8_bytes(tmp_path):
    """CLI 输出同样不跟本地代码页：管道里拿到的是 UTF-8 字节。"""
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONUTF8", "PYTHONIOENCODING"}}
    env.update({"BUTIAN_HOME": str(tmp_path / "cli-home"), "PYTHONIOENCODING": "gbk"})
    subprocess.run([sys.executable, "-m", "butian.cli", "init"], cwd=str(ROOT), env=env,
                   check=True, capture_output=True)
    done = subprocess.run([sys.executable, "-m", "butian.cli", "init"], cwd=str(ROOT), env=env,
                          check=True, capture_output=True)
    payload = json.loads(done.stdout.decode("utf-8"))
    assert payload["status"] == "ok"
