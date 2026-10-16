"""真实Curator收集/验证/候选/晋升：每条工具和artifact不能借用户消息获取学习资格。"""
import json

import pytest

from agent_py_agent.agent.conversation import learning_source as ls
from agent_py_agent.agent.memory_store.curator import (
    MemoryCuratorDependencies,
    MemoryCuratorIdentity,
    MemoryCuratorService,
)
from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore
from agent_py_agent.tests.test_curator_observation_identity import _candidate, _output
from agent_py_agent.tests.test_memory_curator_v2 import _StaticStructuredBackend
from agent_py_agent.tests.test_review38_contracts import env as env
from agent_py_agent.tests.test_review38_contracts import thread


@pytest.mark.parametrize("reference", ["tool", "artifact", "both"])
@pytest.mark.parametrize("source_kind", ["dispatch", "unknown", "missing-request", "user", "user-child"])
# LLM: 建立依赖时拆平嵌套，真实消息/audit、输出校验和晋升及所有断言保持不变。
# 函数用途: 复现引用用户消息A夹带另一回合工具B时，整体覆盖错误扩大的缺口。
def test_curator_mixed_reference_learning_is_not_laundered(env, tmp_path, reference, source_kind):
    allowed = source_kind in {"user", "user-child"}
    good_thread = thread(env, ls.user_dialogue_projection(), "good")
    message = env.store.messages.append(dict(thread_id=good_thread.thread_id, role="user", content="项目使用 UTC 保存时间", metadata={"gateway_request_id": "good-request"}))
    source = ls.user_dialogue_projection()
    metadata = {"gateway_request_id": "audit-request"}
    if source_kind == "dispatch":
        metadata["session_task_id"] = "dispatch-id"
    if source_kind == "unknown":
        source = {}
    if source_kind == "user-child":
        source = ls.subagent_projection(trigger_kind="user_dialogue", run_ids=["child-run"], task_ids=["parent-task"])
    other = thread(env, source, "other")
    env.store.messages.append(dict(thread_id=other.thread_id, role="user", content="工具结果的触发回合", metadata=metadata))
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    audit = dict(event_id="audit-1", event_type="tool_call", created_at="2026-10-10T00:00:00+00:00", status="succeeded", tool_success=True,
                 operation_id="operation-1", tool_call_id="call-1", tool_name="read_file", thread_id=other.thread_id,
                 request_id="no-such-request" if source_kind == "missing-request" else "audit-request", task_id="task-1", run_id="run-1",
                 artifact_ref="artifact://current-output", content_hash="sha256:test")
    from agent_py_agent.tests.test_curator_archive_source_binding import _bind_success_ledger

    _bind_success_ledger(env, audit)
    (audit_dir / "2026-10-10.jsonl").write_text(json.dumps(audit) + "\n")
    draft = _candidate(message.message_id, "项目使用 UTC 保存时间", "review.mixed-source")
    if reference in {"tool", "both"}:
        draft["source_tool_refs"] = [{"event_id": "audit-1"}]
    if reference in {"artifact", "both"}:
        draft["source_artifact_refs"] = [{"artifact_ref": "artifact://current-output"}]
    backend = _StaticStructuredBackend(_output(message, [draft]))
    result = _make_curator(env, audit_dir, backend).run(reason="admin")
    assert result.status == "succeeded", result
    [candidate] = env.service.candidates.list()
    if reference in {"tool", "both"}:
        assert len(candidate.source_tool_refs) == 1
    if reference in {"artifact", "both"}:
        assert len(candidate.source_artifact_refs) == 1
    promoted = env.service.promote(candidate.candidate_id, automatic=True)
    assert not promoted.promoted, (promoted, candidate.learning_source)
    assert ls.projection_is_learnable(candidate.learning_source) is allowed
    assert env.service.long_term.all() == []
    if source_kind == "user-child":
        assert "child-run" in candidate.learning_source["source_run_ids"]


# LLM: 仅集中相同生产依赖的组装，所有来源矩阵与正式晋升断言留在调用方。
# 函数用途: 创建使用真实候选仓库的Curator测试入口。
def _make_curator(env, audit_dir, backend):
    identity = MemoryCuratorIdentity(owner_id="local/test", model="source-review")
    dependencies = MemoryCuratorDependencies(
        backend=backend, conversation_store=env.store, audit_dir=audit_dir,
        state_store=MemoryCuratorStateStore(env.owner / "memory/curator/state.json"),
        daily_store=DailyMemoryStore(env.owner / "memory/daily"),
        candidate_service=env.service.candidates, run_log=CuratorRunLog(env.owner / "memory/curator/runs"),
        identity=identity, tool_evidence_verifier=env.service.tool_verifier)
    config = MemoryCuratorConfig(interval_seconds=60, turn_threshold=1, timeout_seconds=2, max_retries=0)
    return MemoryCuratorService(config=config, dependencies=dependencies)
