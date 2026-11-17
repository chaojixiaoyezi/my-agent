"""R-17：普通对话回合里第一次登记学习反馈前还没有持久任务（读正文的 skill_search get 不晋升），
skill_feedback 的写动作先按首个工作工具的同一事务晋升本回合，再核对主会话执行权；子代理照旧拒绝。"""
from __future__ import annotations

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime_mixin import _bind_main_agent_authority
from agent_py_agent.agent.capability import skill_feedback_tool
from agent_py_agent.agent.gateway_parts.io import write_json_file
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
from agent_py_agent.tests.test_sm_w9_feedback_awareness import _make_real_agent, _publish_skill


# 函数用途: 按网关前台回合的真实形状建立一轮：线程、执行车道 claim、请求绑定写入器，但还没有任务链接。
def _plain_foreground_turn(agent, tmp_path, *, request_id="gwreq-msg-r17"):
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {"channel": "chat", "channel_conversation_id": "sess-r17", "owner_id": str(agent.home_paths.owner_id)})
    paths = gateway_paths_from_root(tmp_path / "gateway")
    request_path = paths.processing / f"{request_id}.json"
    attempt = f"transport-{request_id}"
    write_json_file(request_path, {"id": request_id, "status": "processing", "turn_phase": "open",
                                   "execution_attempt_id": attempt})
    writer = GatewayTaskBindingWriter(request_path, request_id, execution_attempt_id=attempt)
    writer.bind_conversation_claim(thread.thread_id)
    store.claims.acquire({"thread_id": thread.thread_id, "task_id": f"gateway:{request_id}",
                          "reason": "gateway_foreground_turn"})
    agent._current_run_params = _bind_main_agent_authority(agent, RunParams(
        run_id=request_id, task_id=request_id, request_id=request_id, source="gateway",
        context_scope="conversation", task_attributes={"conversation_thread_id": thread.thread_id},
        conversation_task_binding_callback=writer,
    ))
    return thread


def test_adopted_in_a_plain_conversation_turn_binds_the_turn_task_first(tmp_path):
    agent, _repo, store = _make_real_agent(tmp_path, enable_self_learning=True)
    record = _publish_skill(store, "r17-handoff")
    thread = _plain_foreground_turn(agent, tmp_path)
    agent._current_skill_snapshot = agent.skills_service.snapshot_for(force_reload=True)
    assert agent.conversation_store.tasks.load("gwreq-msg-r17") is None
    outcome = skill_feedback_tool.SkillFeedbackTool(agent).execute({"action": "adopted",
                                                                    "skill_id": "owner:r17-handoff"})
    assert outcome.ok, outcome.output
    link = agent.conversation_store.tasks.load("gwreq-msg-r17")
    assert link is not None and link.thread_id == thread.thread_id and link.status == "active"
    assert agent._current_run_params.task_attributes["conversation_task_id"] == "gwreq-msg-r17"
    observations = store.load_registry().skills["r17-handoff"].maintenance.get("observations", [])
    assert any(row.get("outcome") == "adopted" and row.get("sha256") == record.sha256 for row in observations)


def test_status_stays_read_only_and_does_not_bind_a_task(tmp_path):
    agent, _repo, store = _make_real_agent(tmp_path, enable_self_learning=True)
    _publish_skill(store, "r17-status")
    _plain_foreground_turn(agent, tmp_path)
    outcome = skill_feedback_tool.SkillFeedbackTool(agent).execute({"action": "status"})
    assert outcome.ok, outcome.output
    assert agent.conversation_store.tasks.load("gwreq-msg-r17") is None
