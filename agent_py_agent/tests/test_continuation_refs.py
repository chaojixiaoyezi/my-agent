# LLM: 续跑关联的合同单测：全部经 fake 子代理 + fake LLM 的 run_once 主链路与 TaskStore 公开入口，
#   不直接改产品档案结构；覆盖「交付 final 才写、幂等、多级顺序、解析失败/冲突只写诊断、投影读取」。
# 模块用途: 覆盖 continuation_refs 的解析、写入、幂等与管理员投影读取。
from __future__ import annotations

import json

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    ConversationStore,
    FakeDeliveryService,
)
from agent_py_agent.agent.conversation.continuation_refs import (
    CONTINUATION_DIAGNOSTIC_SCHEMA,
    CONTINUATION_REASON_SOURCE_CONFLICT,
    CONTINUATION_REASON_SOURCE_MISSING,
    CONTINUATION_REF_SCHEMA,
    read_continuation_projection,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


# LLM: 后台回合只要求一个能返回固定文本的后端；探针按测试后端声明原生能力，不触网。
# 类用途: 给续跑回合提供最小 fake LLM。
class _ReplyBackend:
    name = "continuation-test"

    # 函数用途: 生成一条固定回复文本。
    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        del prompt, on_chunk, kwargs
        return ModelResponse(text="子代理结果已整合，交付完成。", backend=self.name)

    # 函数用途: 声明测试后端的原生工具能力，避免走真实探测。
    def probe_tool_capability(self):
        from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso

        return ProviderToolCapability(
            provider=self.name,
            endpoint="local://continuation-test",
            model="",
            stream=False,
            native_supported=True,
            evidence="test_backend_declares_native_tools",
            observed_at=_utc_now_iso(),
        )


# LLM: 两个子代理默认都未终态，测试按场景改状态；runtime 初始化会把 agent.conversation_store 指到
#   同一个 store，投影读取与交付写入共用一份任务档案（与生产同源）。
# 函数用途: 搭「原请求 task-root + 两个子代理 + 后台 runtime」的最小环境。
def _continuation_env(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    agent.backend = _ReplyBackend()
    first = agent.subagents.create_run(
        goal="完成第一部分", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    second = agent.subagents.create_run(
        goal="完成第二部分", thought="", plan=["执行"], parent_id="task-root", root_id="task-root"
    )
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-continuation",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-root", "goal": "分两部分完成", "now": 11.0}
    )
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    return agent, store, thread, runtime, first, second


# 函数用途: 跑一次「子代理完成」后台回合；parent 固定为原请求 task-root（冲突场景单独内联构造）。
def _run_wake(runtime, thread_id: str, child_id: str, *, now: float):
    return runtime.run_once(
        {
            "thread_id": thread_id,
            "task_id": "task-root",
            "reason": "subagent_runner_finished",
            "wake_signal": {
                "root_task_id": "task-root",
                "source_agent_id": child_id,
                "parent_agent_id": "task-root",
                "metadata": {"task_id": child_id, "status": "DONE"},
            },
            "now": now,
        }
    )


def test_final_resume_delivery_records_continuation_ref(tmp_path) -> None:
    """请求已结束、子代理完成后交付 final：原请求档案出现一条字段齐全的关联。"""
    agent, store, thread, runtime, first, second = _continuation_env(tmp_path)
    agent.subagents.lifecycle.set_status(first.id, "DONE")
    agent.subagents.lifecycle.set_status(second.id, "DONE")
    report = _run_wake(runtime, thread.thread_id, first.id, now=20.0)

    assert report.delivery_reason == "root_subagents_terminal"
    link = store.tasks.load("task-root")
    assert len(link.continuation_refs) == 1
    assert link.continuation_diagnostics == ()
    ref = link.continuation_refs[0]
    assert ref["schema_version"] == CONTINUATION_REF_SCHEMA
    assert str(ref["turn_id"]).startswith("background-turn")
    assert ref["message_id"]
    assert ref["content_chars"] > 0
    assert ref["delivered_at"] > 0
    assert ref["background_delivery_reason"] == "root_subagents_terminal"
    assert ref["source_run_id"] == first.id
    # 指向的消息确实落盘且是 assistant 回复
    rows = store.messages.recent(thread.thread_id, limit=100)
    target = [row for row in rows if row.message_id == ref["message_id"]]
    assert target and target[0].role == "assistant"


def test_multi_level_resume_appends_refs_in_order(tmp_path) -> None:
    """多级续跑：每次带 final 的续跑追加一条，顺序按写入（时间）递增。"""
    agent, store, thread, runtime, first, second = _continuation_env(tmp_path)
    agent.subagents.lifecycle.set_status(first.id, "DONE")
    agent.subagents.lifecycle.set_status(second.id, "DONE")
    _run_wake(runtime, thread.thread_id, first.id, now=20.0)
    _run_wake(runtime, thread.thread_id, second.id, now=30.0)

    refs = store.tasks.load("task-root").continuation_refs
    assert len(refs) == 2
    assert refs[0]["source_run_id"] == first.id
    assert refs[1]["source_run_id"] == second.id
    assert refs[0]["message_id"] != refs[1]["message_id"]
    assert refs[0]["delivered_at"] <= refs[1]["delivered_at"]


def test_duplicate_delivery_append_is_idempotent(tmp_path) -> None:
    """同一交付（同 message_id）重复记账不重复追加。"""
    agent, store, thread, runtime, first, second = _continuation_env(tmp_path)
    agent.subagents.lifecycle.set_status(first.id, "DONE")
    agent.subagents.lifecycle.set_status(second.id, "DONE")
    _run_wake(runtime, thread.thread_id, first.id, now=20.0)
    ref = dict(store.tasks.load("task-root").continuation_refs[0])

    store.tasks.append_continuation_ref({"task_id": "task-root", "row": ref})
    store.tasks.append_continuation_ref({"task_id": "task-root", "row": ref})
    assert len(store.tasks.load("task-root").continuation_refs) == 1


def test_non_resume_wake_records_nothing(tmp_path) -> None:
    """不是子代理完成触发的回合不写关联、也不写诊断（reason 门是防线，信封即使完整也不落账）。"""
    agent, store, thread, runtime, first, second = _continuation_env(tmp_path)
    agent.subagents.lifecycle.set_status(first.id, "DONE")
    agent.subagents.lifecycle.set_status(second.id, "DONE")
    runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "scheduled_progress_report",
            "wake_signal": {
                "root_task_id": "task-root",
                "source_agent_id": first.id,
                "parent_agent_id": "task-root",
                "metadata": {"task_id": first.id, "status": "DONE"},
            },
            "now": 20.0,
        }
    )
    link = store.tasks.load("task-root")
    assert link.continuation_refs == ()
    assert link.continuation_diagnostics == ()


def test_unresolved_source_records_diagnostic_without_ref(tmp_path) -> None:
    """wake 信封缺父 run：不写关联，留固定原因诊断。"""
    agent, store, thread, runtime, first, second = _continuation_env(tmp_path)
    agent.subagents.lifecycle.set_status(first.id, "DONE")
    agent.subagents.lifecycle.set_status(second.id, "DONE")
    runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "subagent_runner_finished",
            "wake_signal": {
                "root_task_id": "task-root",
                "source_agent_id": first.id,
                "metadata": {"task_id": first.id, "status": "DONE"},
            },
            "now": 20.0,
        }
    )
    link = store.tasks.load("task-root")
    assert link.continuation_refs == ()
    assert len(link.continuation_diagnostics) == 1
    diag = link.continuation_diagnostics[0]
    assert diag["schema_version"] == CONTINUATION_DIAGNOSTIC_SCHEMA
    assert diag["reason"] == CONTINUATION_REASON_SOURCE_MISSING
    assert diag["turn_id"]


def test_conflicting_parent_records_diagnostic_without_ref(tmp_path) -> None:
    """信封父 run 与来源 run 的持久父级冲突：不静默选一侧，留带两侧事实的诊断。"""
    agent, store, thread, runtime, first, second = _continuation_env(tmp_path)
    agent.subagents.lifecycle.set_status(first.id, "DONE")
    agent.subagents.lifecycle.set_status(second.id, "DONE")
    runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-root",
            "reason": "subagent_runner_finished",
            "wake_signal": {
                "root_task_id": "task-root",
                "source_agent_id": first.id,
                "parent_agent_id": "other-parent",
                "metadata": {"task_id": first.id, "status": "DONE"},
            },
            "now": 20.0,
        }
    )
    link = store.tasks.load("task-root")
    assert link.continuation_refs == ()
    assert len(link.continuation_diagnostics) == 1
    diag = link.continuation_diagnostics[0]
    assert diag["reason"] == CONTINUATION_REASON_SOURCE_CONFLICT
    assert any("task_parent_id=task-root" in item for item in diag["warnings"])
    assert any("wake_parent_agent_id=other-parent" in item for item in diag["warnings"])


def test_partial_resume_delivery_records_nothing(tmp_path) -> None:
    """中间回合被抑制（还有子代理在跑）时不写任何关联或诊断。"""
    agent, store, thread, runtime, first, second = _continuation_env(tmp_path)
    agent.subagents.lifecycle.set_status(first.id, "DONE")
    report = _run_wake(runtime, thread.thread_id, first.id, now=20.0)

    assert report.delivery_reason == "partial_subagent_success"
    link = store.tasks.load("task-root")
    assert link.continuation_refs == ()
    assert link.continuation_diagnostics == ()


def test_admin_projection_reads_latest_ref(tmp_path) -> None:
    """管理员投影按最新一条给出结构化指向；无关联请求返回 None。"""
    agent, store, thread, runtime, first, second = _continuation_env(tmp_path)
    agent.subagents.lifecycle.set_status(first.id, "DONE")
    agent.subagents.lifecycle.set_status(second.id, "DONE")
    _run_wake(runtime, thread.thread_id, first.id, now=20.0)
    _run_wake(runtime, thread.thread_id, second.id, now=30.0)

    projection = read_continuation_projection(agent, "task-root")
    assert projection is not None
    assert projection["ref_count"] == 2
    latest = projection["latest"]
    assert latest["message_id"] == store.tasks.load("task-root").continuation_refs[-1]["message_id"]
    assert latest["background_delivery_reason"] == "root_subagents_terminal"
    assert read_continuation_projection(agent, "no-such-task") is None


def test_legacy_task_link_rewrite_keeps_empty_keys_omitted(tmp_path) -> None:
    """旧档案没有续跑键；普通状态更新重写后也不新增空键（保持旧序列化字节口径）。"""
    agent, store, thread, runtime, first, second = _continuation_env(tmp_path)
    path = store.storage.task_path("task-root")
    assert "continuation_refs" not in json.loads(path.read_text())
    store.tasks.update_status({"task_id": "task-root", "status": "active"})
    payload = json.loads(path.read_text())
    assert "continuation_refs" not in payload
    assert "continuation_diagnostics" not in payload


# LLM: /result 的归档终态返回体：管理员完整结果附 continuation 结构化指向，普通用户的公开投影不带（IM/TUI 不变）；
#   server 只需带 agent 属性（真实 Gateway server 同名字段），不发 HTTP 请求。
# 函数用途: 钉住管理员视图有续跑指向、公开视图没有。
def test_result_body_adds_continuation_only_for_admin_view(tmp_path) -> None:
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.http_handlers import (
        _archived_result_body,
        _ResultAccessContext,
    )

    agent, store, thread, runtime, first, second = _continuation_env(tmp_path)
    agent.subagents.lifecycle.set_status(first.id, "DONE")
    agent.subagents.lifecycle.set_status(second.id, "DONE")
    _run_wake(runtime, thread.thread_id, first.id, now=20.0)
    server = SimpleNamespace(agent=agent)
    terminal = {"id": "task-root", "request_id": "task-root", "status": "done", "ok": True, "response": "第一段"}

    admin = _archived_result_body(server, _ResultAccessContext("task-root", "local-agent", None), terminal)
    assert admin["continuation"]["ref_count"] == 1
    assert admin["continuation"]["latest"]["message_id"] == store.tasks.load("task-root").continuation_refs[-1]["message_id"]
    restricted = SimpleNamespace(can_access_all_users=False)
    public = _archived_result_body(server, _ResultAccessContext("task-root", "local-agent", restricted), terminal)
    assert "continuation" not in public
