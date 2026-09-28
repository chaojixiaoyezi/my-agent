"""生命周期唤醒片续接原回合的工具事实（2026-09-28，T3 真实 TUI 验收发现）。

背景：子代理阻塞/完成后，父代理的后台唤醒片按唤醒信封里的 conversation_request_id 续接原用户回合的工具账。
前台 Gateway 轮的工具记录写在 owner 根自己的索引，唤醒片只读任务 work 索引，所以前台那次成功的 create_subagents
不在续跑的去重集合里：第一次唤醒片里同样内容的派工照样成功，多出一个子代理。
锁定：
- 续跑同时读 owner 根自己的索引（owner 由任务 run_workspace.json 的结构化 owner_home 给出，且任务根必须在其内）
  和任务 work 索引，都按精确请求编号流式过滤，别的请求、别的 run 的索引不会进来；
- owner 索引的记录只标记为运行时状态：进一次性编排去重、已执行工具和工具轮数，不进本片工具账与模型可见交接；
- 片内溢出压缩替换携带内容时，这些记录保留；工作片的新增工具轮额度不因携带记录而缩水。
读错分支（Codex 审查 B 后补，2026-09-28）：
- 两个来源各自读取、各自捕获异常，owner 索引读不到时任务索引照常读到；
- 读不到时 task_attributes 留下结构化不完整事实（来源与错误类型），压缩重试沿用首次读取的结论；
- 不完整时一次性编排去重 fail-closed：没写 replacement_for_run_ids 的 create_subagents 一律拦下，其它工具不受影响。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.create_payload import (
    effective_replacement_ids_per_child,
)
from agent_py_agent.agent.agent_core.parameters import _one_shot_tool_call_is_duplicate
from agent_py_agent.agent.agent_core.runtime.loop_models import (
    RunParams,
    RuntimeLoopParams,
    RuntimeToolLoopSeed,
)
from agent_py_agent.agent.agent_core.runtime.loop_support import _tool_loop_execute_params
from agent_py_agent.agent.agent_core.tool_call_runtime import (
    _duplicate_one_shot_result,
    _one_shot_blocked_by_incomplete_carry,
)
from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS
from agent_py_agent.agent.conversation import background_execution as execution_module
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_ACTIVE_TURN_CARRY_INCOMPLETE_ATTR,
)
from agent_py_agent.agent.conversation.compact_carry import compact_overflow_carry
from agent_py_agent.agent.conversation.runtime import (
    BackgroundRunRequest,
    GoalRuntimeContext,
    _background_active_turn_tool_calls,
    _goal_runtime_context,
    _run_params,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive.compact_tool_output_refs import (
    CARRIED_RUNTIME_ONLY_FIELD,
    CarriedIndexSource,
    carried_tool_call_records_for_requests,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.run_workspace import run_workspace_owner_home

_FOREGROUND = "gwreq-foreground-1"
_CHILD_GOAL = "读取父项目资料并写出三条要点"


# 函数用途: 写一行工具索引记录；字段与生产索引同名，只填续跑读取需要的结构化身份，参数另用 _params 补。
def _row(tool: str, call_id: str, request_id: str) -> dict:
    attempt = f"{request_id}:{call_id}:attempt"
    return {
        "kind": "tool_call", "tool": tool, "call_id": call_id, "scoped_call_id": f"{request_id}:{call_id}",
        "request_id": request_id, "conversation_request_id": request_id, "run_id": request_id,
        "task_id": request_id, "attempt_id": attempt, "turn_id": f"{attempt}:turn", "ok": True, "status": "ok",
        "parameters": {}, "model_parameters": {},
    }


def _params(parameters: dict) -> dict:
    return {"parameters": dict(parameters), "model_parameters": dict(parameters)}


def _write_index(root: Path, rows: list[object]) -> None:
    index = root / "blobs" / "tool_outputs" / "index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    lines = [row if isinstance(row, str) else json.dumps(row, ensure_ascii=False) for row in rows]
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")


# 函数用途: 按 T3 round1 的真实布局搭一个 owner 家目录：前台记录在 owner 根索引，唤醒片记录在任务 work 索引，
#   另有别的请求、只在参数里提到本请求编号的记录、坏行，以及别的 run 的索引。
def _owner_layout(tmp_path: Path) -> tuple[Path, Path]:
    owner = tmp_path / "home" / "owners" / "local" / "main"
    task_root = owner / "runs" / "2026-09-28" / "run-origin"
    (task_root / "work").mkdir(parents=True)
    (task_root / "work" / "run_workspace.json").write_text(json.dumps({
        "schema_version": "run_workspace.v1", "owner_home": str(owner), "task_root": str(task_root),
        "request_id": _FOREGROUND, "task_id": _FOREGROUND,
    }), encoding="utf-8")
    _write_index(owner, [
        _row("create_subagents", "call-create", _FOREGROUND) | _params({"goal": _CHILD_GOAL, "description": "读父项目资料"}),
        _row("read_file", "call-other", "gwreq-other") | _params({"path": "a.txt"}),
        _row("read_file", "call-mention", "gwreq-other") | _params({"note": _FOREGROUND}),
        "{not json " + _FOREGROUND,
    ])
    _write_index(task_root / "work", [_row("read_file", "call-wake", _FOREGROUND) | _params({"path": "b.txt"})])
    _write_index(owner / "runs" / "2026-09-28" / "run-else" / "work", [_row("read_file", "call-elsewhere", _FOREGROUND)])
    return owner, task_root


def _sources(owner: Path, task_root: Path) -> list[CarriedIndexSource]:
    return [CarriedIndexSource(owner, "owner_index", runtime_only=True), CarriedIndexSource(task_root / "work", "task_index")]


def test_owner_and_task_indexes_are_read_by_exact_request_only(tmp_path) -> None:
    owner, task_root = _owner_layout(tmp_path)

    read = carried_tool_call_records_for_requests(_sources(owner, task_root), [_FOREGROUND])
    records = read.records

    # 别的请求、只在参数里提到本请求编号的记录、坏行、别的 run 的索引都不会进来。
    assert [(item["call_id"], item.get(CARRIED_RUNTIME_ONLY_FIELD)) for item in records] == [
        ("call-create", True), ("call-wake", None),
    ]
    assert read.unreadable_sources == ()
    assert carried_tool_call_records_for_requests(_sources(owner, task_root), []).records == []


def test_owner_home_comes_from_run_workspace_and_must_contain_the_task(tmp_path) -> None:
    owner, task_root = _owner_layout(tmp_path)
    identity_path = task_root / "work" / "run_workspace.json"
    assert run_workspace_owner_home(task_root) == owner.resolve()

    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    for broken in ({**identity, "owner_home": str(tmp_path / "elsewhere")},
                   {**identity, "schema_version": "run_workspace.v0"},
                   {**identity, "owner_home": ""}):
        identity_path.write_text(json.dumps(broken), encoding="utf-8")
        assert run_workspace_owner_home(task_root) is None
    identity_path.unlink()
    assert run_workspace_owner_home(task_root) is None


def test_wake_slice_carries_the_foreground_turn_from_the_owner_index(tmp_path) -> None:
    owner, task_root = _owner_layout(tmp_path)
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "user-1", "channel": "internal", "channel_conversation_id": "thread-wake",
        "channel_user_id": "user-1",
    })
    objective = "请派一个子代理去读父项目目录里的资料并写出要点。"
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": _FOREGROUND, "goal": objective,
                      "status": "active", "task_path": str(task_root)})
    store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": objective,
                           "metadata": {"conversation_request_id": _FOREGROUND}})
    request = BackgroundRunRequest(
        thread_id=thread.thread_id, task_id=_FOREGROUND, reason="subagent_runner_finished",
        wake_signal={"metadata": {"conversation_request_id": _FOREGROUND}},
    )
    params = _run_params(thread.thread_id, request, agent, goal_context=_goal_runtime_context(agent, store, request),
                         thread=thread)
    baseline = _run_params(thread.thread_id, request, agent, goal_context=GoalRuntimeContext(task_objective=objective),
                           thread=thread)

    assert [(item["call_id"], item.get(CARRIED_RUNTIME_ONLY_FIELD)) for item in params.carried_archive_tool_calls] == [
        ("call-create", True), ("call-wake", None),
    ]
    # 携带记录按条数抬高本片的绝对上限，新增工具轮额度仍是配置值，不会一开始就触顶。
    assert params.task_attributes["max_tool_rounds"] == baseline.task_attributes["max_tool_rounds"] + 2
    # 两处都读到时不写不完整事实。
    assert CONVERSATION_ACTIVE_TURN_CARRY_INCOMPLETE_ATTR not in params.task_attributes


# 函数用途: 用携带记录构造一次工具循环参数，只替身 agent，不跑模型。
def _loop(carried: list[dict]) -> object:
    seed = RuntimeToolLoopSeed(
        params=RuntimeLoopParams(
            user_prompt="继续", root_user_prompt="继续", memories=[], runtime_injections=[], routed_context=None,
            resume_context_section="", carried_archive_tool_calls=carried,
        ),
        memories=[], tool_catalog_section="", tool_recommendations_section="",
    )
    return _tool_loop_execute_params(SimpleNamespace(backend=None, config=SimpleNamespace()), seed)


def test_runtime_only_records_rebuild_dedupe_but_not_the_slice_archive(tmp_path) -> None:
    owner, task_root = _owner_layout(tmp_path)
    carried = carried_tool_call_records_for_requests(_sources(owner, task_root), [_FOREGROUND]).records

    loop = _loop(carried)

    # 前台那次成功的 create_subagents 进了一次性编排去重：唤醒片里同样内容的派工会被拦下；
    # 结构化写明接替旧 run 的重派意图键不同，照常放行，拦截结果也指出这条路径。
    live = {"tool": "create_subagents", "goal": _CHILD_GOAL, "description": "读父项目资料"}
    assert _one_shot_tool_call_is_duplicate(live, loop.one_shot_tool_calls)
    replacement = {**live, "replacement_for_run_ids": ["subagent-old"]}
    assert not _one_shot_tool_call_is_duplicate(replacement, loop.one_shot_tool_calls)
    rejected = _duplicate_one_shot_result(live)
    assert rejected.error_code == "TOOL_ONE_SHOT_ALREADY_EXECUTED" and "replacement_for_run_ids" in rejected.output
    assert loop.tool_rounds == 2 and loop.executed_tools == ["create_subagents", "read_file"]
    # 本片工具账与模型可见交接只含唤醒片自己的记录；前台记录已在会话历史里原生重放。
    assert [item["call_id"] for item in loop.archive_tool_calls] == ["call-wake"]
    assert not any("call-create" in entry or "create_subagents" in entry for entry in loop.tool_context)


def test_overflow_carry_keeps_runtime_only_records() -> None:
    runtime_only = {"call_id": "call-create", CARRIED_RUNTIME_ONLY_FIELD: True}
    carried = [runtime_only, {"call_id": "call-wake"}]
    result_archive = [{"call_id": "call-wake"}, {"call_id": "call-new"}]

    records, _inputs = compact_overflow_carry(
        carried_archive_tool_calls=carried, carried_active_turn_user_inputs=[],
        result_archive_tool_calls=result_archive, result_active_turn_user_inputs=None, released_input_ids=(),
    )
    assert [item["call_id"] for item in records] == ["call-create", "call-wake", "call-new"]

    unchanged, _inputs = compact_overflow_carry(
        carried_archive_tool_calls=carried, carried_active_turn_user_inputs=[],
        result_archive_tool_calls=[], result_active_turn_user_inputs=None, released_input_ids=(),
    )
    assert unchanged == carried


# ---- 读错分支：一处读不到不能跳过另一处，也不能把半份携带当成完整 ----

_UNREADABLE_OWNER = [{"source": "owner_index", "error_type": "IsADirectoryError"}]


# 函数用途: 把一个根的工具索引换成同名目录，读取时产生真实的 OSError（IsADirectoryError），不替身文件系统。
def _break_index(root: Path) -> None:
    index = root / "blobs" / "tool_outputs" / "index.jsonl"
    if index.exists():
        index.unlink()
    index.mkdir(parents=True)


# 函数用途: 按 B 的真实布局准备线程、任务链接与原用户消息，返回本片的后台运行参数。
def _wake_params(tmp_path: Path, task_root: Path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "user-1", "channel": "internal", "channel_conversation_id": "thread-wake-error",
        "channel_user_id": "user-1",
    })
    objective = "请派一个子代理去读父项目目录里的资料并写出要点。"
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": _FOREGROUND, "goal": objective,
                      "status": "active", "task_path": str(task_root)})
    store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": objective,
                           "metadata": {"conversation_request_id": _FOREGROUND}})
    request = BackgroundRunRequest(
        thread_id=thread.thread_id, task_id=_FOREGROUND, reason="subagent_runner_finished",
        wake_signal={"metadata": {"conversation_request_id": _FOREGROUND}},
    )
    return _run_params(thread.thread_id, request, agent, goal_context=_goal_runtime_context(agent, store, request),
                       thread=thread)


def test_an_unreadable_owner_index_does_not_hide_the_task_index(tmp_path) -> None:
    owner, task_root = _owner_layout(tmp_path)
    _break_index(owner)

    read = carried_tool_call_records_for_requests(_sources(owner, task_root), [_FOREGROUND])

    assert [item["call_id"] for item in read.records] == ["call-wake"]
    assert list(read.unreadable_sources) == _UNREADABLE_OWNER


def test_wake_slice_records_the_unreadable_source_as_a_structured_fact(tmp_path) -> None:
    owner, task_root = _owner_layout(tmp_path)
    _break_index(owner)

    params = _wake_params(tmp_path, task_root)

    assert [item["call_id"] for item in params.carried_archive_tool_calls] == ["call-wake"]
    assert params.task_attributes[CONVERSATION_ACTIVE_TURN_CARRY_INCOMPLETE_ATTR] == _UNREADABLE_OWNER


def test_an_unusable_workspace_identity_marks_the_owner_source_unknown(tmp_path) -> None:
    owner, task_root = _owner_layout(tmp_path)
    identity_path = task_root / "work" / "run_workspace.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity_path.write_text(json.dumps({**identity, "owner_home": str(tmp_path / "elsewhere")}), encoding="utf-8")

    params = _wake_params(tmp_path, task_root)

    assert [item["call_id"] for item in params.carried_archive_tool_calls] == ["call-wake"]
    assert params.task_attributes[CONVERSATION_ACTIVE_TURN_CARRY_INCOMPLETE_ATTR] == [
        {"source": "owner_index", "error_type": "run_workspace_identity_unusable"},
    ]


def test_a_legacy_wake_reports_an_unreadable_task_index(tmp_path) -> None:
    _owner, task_root = _owner_layout(tmp_path)
    _break_index(task_root / "work")
    request = BackgroundRunRequest(thread_id="thread-1", task_id=_FOREGROUND, reason="subagent_runner_finished")

    read = _background_active_turn_tool_calls(
        request, GoalRuntimeContext(task_objective="原任务", task_path=str(task_root)),
    )

    assert read.records == []
    assert list(read.unreadable_sources) == [{"source": "task_index", "error_type": "IsADirectoryError"}]


def test_incomplete_carry_fails_closed_only_for_one_shot_orchestration() -> None:
    incomplete = SimpleNamespace(task_attributes={CONVERSATION_ACTIVE_TURN_CARRY_INCOMPLETE_ATTR: _UNREADABLE_OWNER})
    create = {"tool": "create_subagents", "goal": _CHILD_GOAL}
    batch = {"tool": "create_subagents", "items": [
        {"goal": "甲", "replacement_for_run_ids": ["subagent-old"]}, {"goal": "乙"},
    ]}

    assert _one_shot_blocked_by_incomplete_carry(incomplete, create)
    assert _one_shot_blocked_by_incomplete_carry(incomplete, batch)
    assert not _one_shot_blocked_by_incomplete_carry(incomplete, {**create, "replacement_for_run_ids": ["subagent-old"]})
    assert not _one_shot_blocked_by_incomplete_carry(incomplete, {"tool": "read_file", "path": "a.txt"})
    assert not _one_shot_blocked_by_incomplete_carry(SimpleNamespace(task_attributes={}), create)
    assert "replacement_for_run_ids" in ERROR_CONTRACTS["TOOL_ONE_SHOT_HISTORY_INCOMPLETE"].recovery_hint


def test_compact_retry_keeps_the_first_reads_incomplete_carry(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({"channel": "tui", "channel_conversation_id": "carry-retry"})
    request = BackgroundRunRequest(thread_id=thread.thread_id)
    seen: list[dict] = []

    # 首次准备读到不完整；压缩后的重试重读成功，但携带记录仍是首次那份，结论必须沿用首次。
    def prepare_run(*_args, **kwargs):
        attributes = {"conversation_thread_id": thread.thread_id}
        if not seen:
            attributes[CONVERSATION_ACTIVE_TURN_CARRY_INCOMPLETE_ATTR] = [dict(item) for item in _UNREADABLE_OWNER]
        return RunParams(source="background_main_agent", request_id="carry-retry", task_attributes=attributes,
                         carried_archive_tool_calls=[{"call_id": "call-wake", "tool": "read_file", "ok": True}],
                         conversation_history_seed=kwargs.get("history_seed"))

    # 替身沿用被替换函数的调用形状：位置参数依次是 execution、prompt、params、history、context。
    def attempt(*args, **_kwargs):
        params = args[2]
        seen.append(dict(params.task_attributes or {}))
        if len(seen) == 1:
            return SimpleNamespace(runtime_status="context_overflow", archive_tool_calls=[],
                                   active_turn_user_inputs=None), params, None
        return SimpleNamespace(runtime_status="completed"), params, SimpleNamespace(committed=True, committed_thread=thread)

    def refresh(_execution, current, history):
        return current, SimpleNamespace(seed=history.seed, compact_context=history.compact_context,
                                        compact_source=SimpleNamespace(messages=["earlier"]),
                                        context_bundle=history.context_bundle)

    monkeypatch.setattr(execution_module, "_run_background_recovery_attempt", attempt)
    monkeypatch.setattr(execution_module, "_refresh_background_compact_source", refresh)
    execution_module.run_background_turn_with_compact(
        execution_module.BackgroundExecutionDependencies(agent=agent, store=store, prepare_run=prepare_run),
        thread, request, user_prompt="继续", continuation_injection=[], proactive_delivery_available=False,
        activity_sink=execution_module.BackgroundMainActivitySink(agent, thread_id=thread.thread_id, task_id=""),
    )

    assert [item.get(CONVERSATION_ACTIVE_TURN_CARRY_INCOMPLETE_ATTR) for item in seen] == [_UNREADABLE_OWNER] * 2


# 守卫与创建边界同口径（Codex 复核的两个反例）：item 的字段覆盖顶层，接替编号去掉空白后必须非空。
@pytest.mark.parametrize(("carry_incomplete", "payload", "expected_ids", "blocked"), [
    (True, {"goal": "返回一句确认", "replacement_for_run_ids": [" "]}, [[]], True),
    (True, {"replacement_for_run_ids": ["old"], "items": [{"goal": "返回一句确认", "replacement_for_run_ids": []}]},
     [[]], True),
    (True, {"items": [{"replacement_for_run_ids": ["old"]}]}, [[]], True),
    (False, {"goal": "返回一句确认"}, [[]], False),
    (True, {"replacement_for_run_ids": ["old"], "items": [{"goal": "返回一句确认"}]}, [["old"]], False),
    (True, {"items": [{"goal": "返回一句确认", "replacement_for_run_ids": ["old"]}]}, [["old"]], False),
])
def test_fail_closed_guard_reads_replacements_like_the_creation_boundary(
    carry_incomplete, payload, expected_ids, blocked,
) -> None:
    attributes = {CONVERSATION_ACTIVE_TURN_CARRY_INCOMPLETE_ATTR: _UNREADABLE_OWNER} if carry_incomplete else {}
    call = {"tool": "create_subagents", **payload}

    assert effective_replacement_ids_per_child(call) == expected_ids
    assert _one_shot_blocked_by_incomplete_carry(SimpleNamespace(task_attributes=attributes), call) is blocked
