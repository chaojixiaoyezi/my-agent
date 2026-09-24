"""三宿主完整链：摘要期间不再驻留旧请求的完整原生历史（约 4.2M 字符），摘要覆盖与首业务请求保持完整。"""
from __future__ import annotations

import gc
import json
import tracemalloc

import pytest

from agent_py_agent.agent.backends import http
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation import background_execution, compact, compact_request_budget
from agent_py_agent.agent.conversation.compact_checkpoint import committed_compact_checkpoint_chain
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.model_request_selection import _HOST
from agent_py_agent.tests.test_background_compact_recovery import _background
from agent_py_agent.tests.test_gateway_model_adoption import actual_request
from agent_py_agent.tests.test_subagent_compact_recovery import _child

COUNT = 128
BODY_SIZE = 32_768
TASKS = {"child": "核对材料并给出结论", "gateway": "请整理当前资料并给出下一步建议", "background": "继续核对本轮资料"}


# LLM: 只追加到 tmp_path 的真实 canonical JSONL，不走宿主写入口；与 2a 基线同一形状（128 行、约 4.2M 字符）。
# 函数用途: 给线程写入一段必然触发压缩的长历史，返回全部消息 ID。
def _seed_history(store, thread_id: str) -> list[str]:
    path = store.storage.message_path(thread_id)
    ids = []
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            ids.extend(json.loads(line)["message_id"] for line in handle)
    with path.open("a", encoding="utf-8") as handle:
        for index in range(COUNT):
            message_id = f"host-history-{index:03d}"
            ids.append(message_id)
            handle.write(json.dumps({
                "message_id": message_id, "thread_id": thread_id,
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"原文{index}:" + "x" * BODY_SIZE + f":结束{index}",
                "metadata": {"conversation_request_id": f"prior-{index // 2}"},
            }, ensure_ascii=False) + "\n")
    return ids


# LLM: 只观测，不改变摘要结果：摘要辅助回复固定，业务 HTTP 只回一句完成并记录是否带当前任务。
# 函数用途: 在第一次进入摘要时记录驻留内存与旧历史是否已解绑，并替换网络端。
def _instrument(monkeypatch, host: str) -> dict:
    facts = {"entry_bytes": None, "released": None, "business": 0, "task_in_payload": False}
    original_summarize = compact._summarize

    def summarize(*args, **kwargs):
        if facts["entry_bytes"] is None:
            facts["entry_bytes"] = tracemalloc.get_traced_memory()[0]
            current = _HOST.get()
            recovery = getattr(current, "compact_recovery", current)
            facts["released"] = recovery.render_params.provider_history_messages == []
        return original_summarize(*args, **kwargs)

    def auxiliary(_request):
        return ModelResponse(text="已完整核对原始材料，保留任务要求与来源。", backend="fake")

    def send(request):
        facts["business"] += 1
        facts["task_in_payload"] = TASKS[host] in json.dumps(request.payload, ensure_ascii=False)
        if "system" in request.payload:
            return {"content": [{"type": "text", "text": "材料核对完成。"}], "stop_reason": "end_turn"}
        return {"choices": [{"message": {"content": "材料核对完成。"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(compact, "_summarize", summarize)
    monkeypatch.setattr(compact_request_budget, "generate_auxiliary_model_response", auxiliary)
    monkeypatch.setattr(http, "post_json", send)
    return facts


# LLM: 三宿主原入口，与 2a 基线相同；返回 (agent, thread_id, 运行一次并判定成功的函数)。
# 函数用途: 准备 child 宿主。
def _child_host(tmp_path):
    agent, task = _child(tmp_path, backend="anthropic_compatible", tools=False)
    return agent, task.agent_thread_id, lambda: agent.run_subagent(task.id, dry_run=False, probe=False).ok


# 函数用途: 准备 Gateway 宿主。
def _gateway_host(tmp_path):
    fixture = actual_request(tmp_path, mode="disabled", original_window=40_000)
    return (fixture.agent, fixture.thread_id,
            lambda: request_execution._run_gateway_ask(fixture.context).response == "材料核对完成。")


# 函数用途: 准备后台宿主。
def _background_host(tmp_path):
    agent, _store, thread, request, execution, sink = _background(
        tmp_path, backend="anthropic_compatible", detached=False)

    def run() -> bool:
        result = background_execution.run_background_turn_with_compact(
            execution, thread, request, user_prompt=TASKS["background"], continuation_injection=[],
            proactive_delivery_available=False, activity_sink=sink)
        return result.runtime_status == "ok" and result.model_response == "材料核对完成。"

    return agent, thread.thread_id, run


_HOSTS = {"child": _child_host, "gateway": _gateway_host, "background": _background_host}


# LLM: 与 2a 基线同一窗口配置（40k、50% 触发），只在 tracemalloc 期间运行一次真实链路。
# 函数用途: 按宿主运行完整 prepare→Compact→首业务请求，返回线程 ID、种子 ID、是否成功与观测事实。
def _run_host(tmp_path, monkeypatch, host: str):
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "my-agent-home"))
    agent, thread_id, run = _HOSTS[host](tmp_path)
    agent.config.model_context_window_tokens = 40_000
    agent.backend.context_window_tokens = 40_000
    agent.config.memory_compact_auto_trigger_percent = 50
    agent.config.max_tokens = 1024
    ids = _seed_history(agent.conversation_store, thread_id)
    facts = _instrument(monkeypatch, host)
    gc.collect()
    tracemalloc.start()
    try:
        ok = run()
    finally:
        tracemalloc.stop()
    return agent, thread_id, ids, ok, facts


@pytest.mark.slow
@pytest.mark.parametrize("host", ["gateway", "child", "background"])
def test_summary_phase_does_not_hold_old_request_history(tmp_path, monkeypatch, host):
    agent, thread_id, ids, ok, facts = _run_host(tmp_path, monkeypatch, host)
    body_chars = COUNT * BODY_SIZE
    # 解绑前三宿主摘要入口驻留 8.9–10.2MB（旧历史约 8.4MB）；解绑后低于一份历史正文的四分之三。
    assert facts["released"] is True, "进入摘要时原参数的旧历史已解绑"
    assert facts["entry_bytes"] is not None and facts["entry_bytes"] < body_chars * 3 // 4
    thread = agent.conversation_store.threads.require(thread_id)
    chain = committed_compact_checkpoint_chain(agent, thread)
    assert ok and thread.compact_generation == 1
    assert len(chain) == 1 and chain[0]["source_message_ids"] == ids, "摘要仍逐条覆盖全部历史"
    assert facts["business"] == 1 and facts["task_in_payload"]
