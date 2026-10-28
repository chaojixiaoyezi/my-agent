"""真实工具归档生产者到 Curator 的来源资格回归；不手造审计 JSON。"""
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from agent_py_agent.agent.agent_core import _finalization_service as finalization
from agent_py_agent.agent.agent_core.runtime import live_archive
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


# LLM: 隔离归档根和无关任务副本写入；实时/终态身份、事件及JSONL均由原生产者生成。
# 函数用途: 生成来自真实宿主运行入口的工具审计。
def _archive(env, monkeypatch, source_kind, archive_mode):
    source = {} if source_kind == "unknown" else ls.user_dialogue_projection()
    source_thread = thread(env, source, "archive-trigger")
    metadata = {"gateway_request_id": "archive-request"}
    if source_kind == "dispatch":
        metadata["session_task_id"] = "dispatch-id"
    env.store.messages.append(dict(thread_id=source_thread.thread_id, role="user", content="执行工具", metadata=metadata))
    monkeypatch.setattr(live_archive, "runtime_archive_roots", lambda agent: (env.owner,))
    params = NS(request_id="absent" if source_kind == "missing-request" else "archive-request",
                run_id="archive-run", task_id="archive-task", save=True,
                task_attributes={"conversation_thread_id": source_thread.thread_id})
    agent = NS(session_id="not-a-thread", config=NS(memory_archive_level=0, agent_name="test"))
    record = dict(id="archive-call", tool="read_file", ok=True, status="succeeded",
                  operation_id="archive-operation", output="confirmed project fact",
                  output_path="artifact://archive-output", thread_id="untrusted-record-thread")
    if archive_mode == "live":
        live_archive.archive_tool_call_if_enabled(agent, params, record, tool_round=1, tool_index=0)
        assert "raw_archive_error" not in record
        paths = [Path(record["raw_archive_path"])]
    else:
        # 只隔离无关任务工作区副本；终态归档适配、事件构造和JSONL存储均保持真实。
        monkeypatch.setattr(finalization, "runtime_archive_roots", lambda *a, **kw: (env.owner,))
        monkeypatch.setattr(finalization, "write_run_task_workspace_if_needed", lambda *a: None)
        archived = finalization.FinalizationService(agent)._archive_run_if_needed(finalization.ArchiveRunParams(
            do_save=True, user_prompt="执行工具", final_response=NS(text="已读", backend="test"),
            archive_tool_calls=[record], run_request_id=params.request_id, run_id=params.run_id,
            task_id=params.task_id, source="chat", task_attributes=params.task_attributes, context_scope=None))
        paths = archived.write_paths
    rows = [json.loads(line) for path in paths for line in Path(path).read_text().splitlines()]
    tools = [row for row in rows if row.get("operation_id") == "archive-operation"]
    assert len(tools) == 1
    return tools[0], source_thread


# LLM: 外部模型只作为固定候选发生器；收集、来源核对、提交和晋升均使用实际组件。
# 函数用途: 为归档回归建立完整Curator链路。
def _curator(env, message, draft):
    deps = MemoryCuratorDependencies(
        backend=_StaticStructuredBackend(_output(message, [draft])), conversation_store=env.store,
        audit_dir=env.owner / "audit", state_store=MemoryCuratorStateStore(env.owner / "memory/curator/state.json"),
        daily_store=DailyMemoryStore(env.owner / "memory/daily"), candidate_service=env.service.candidates,
        tool_evidence_verifier=env.service.tool_verifier,
        run_log=CuratorRunLog(env.owner / "memory/curator/runs"),
        identity=MemoryCuratorIdentity(owner_id="local/test", model="archive-source-review"))
    return MemoryCuratorService(config=MemoryCuratorConfig(interval_seconds=60, turn_threshold=1,
                               timeout_seconds=2, max_retries=0), dependencies=deps)


# LLM: 参数打包但保持原全部来源×归档模式×引用笛卡尔积；不能以消息A授权工具B。
# 函数用途: 检查实际归档工具及artifact在Curator中保留正确来源资格。
@pytest.mark.parametrize("reference", ["tool", "artifact"])
@pytest.mark.parametrize("case", [(kind, mode) for kind in ["user", "dispatch", "unknown", "missing-request"] for mode in ["live", "final"]])
def test_real_archive_source_reaches_curator(env, monkeypatch, reference, case):
    source_kind, archive_mode = case
    good = thread(env, ls.user_dialogue_projection(), "message-evidence")
    message = env.store.messages.append(dict(thread_id=good.thread_id, role="user", content="项目使用UTC保存时间"))
    event, source_thread = _archive(env, monkeypatch, source_kind, archive_mode)
    _bind_success_ledger(env, event)
    draft = _candidate(message.message_id, message.content, "archive.source")
    key, ref = ("source_tool_refs", {"event_id": event["event_id"]}) if reference == "tool" else (
        "source_artifact_refs", {"artifact_ref": event["content_path"]})
    draft[key] = [ref]
    result = _curator(env, message, draft).run(reason="admin")
    assert result.status == "succeeded", result
    [candidate] = env.service.candidates.list()
    allowed = source_kind == "user"
    assert ls.projection_is_learnable(candidate.learning_source) is allowed, candidate.learning_source
    promoted = env.service.promote(candidate.candidate_id, automatic=True)
    assert promoted.promoted is allowed, promoted
    assert (len(env.service.long_term.all()) == 1) is allowed
    if not allowed:
        assert env.service.long_term.all() == []
    assert event.get("thread_id") == source_thread.thread_id


# LLM: 成功工具夹具补真实SQLite终态账本及正式verifier，不mock核验函数，不改变任何来源正反断言。
# 函数用途: 为原本只带audit的成功样本补齐晋升所需生产证据。
def _bind_success_ledger(env, event):
    from agent_py_agent.agent.local_storage import LocalStore, RuntimeGateLedgerRecord
    from agent_py_agent.agent.memory_store.promotion import LocalStoreToolEvidenceVerifier
    ledger = LocalStore(env.owner / "tool-evidence.sqlite3")
    ledger.record_runtime_gate_ledger(RuntimeGateLedgerRecord(
        run_id=event["run_id"], task_id=event["task_id"], operation_id=event["operation_id"],
        tool=event.get("tool_name") or "read_file", args_hash="fixture-hash",
        idempotency_key=event["operation_id"], status="succeeded"))
    env.service.tool_verifier = LocalStoreToolEvidenceVerifier(ledger, owner_id="local/test", owner_root=env.owner)
