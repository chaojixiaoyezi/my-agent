"""生命周期唤醒片续接原回合的工具事实（2026-09-28，T3 真实 TUI 验收发现）。

背景：子代理阻塞/完成后，父代理的后台唤醒片按唤醒信封里的 conversation_request_id 续接原用户回合的工具账。
前台 Gateway 轮的工具记录写在 owner 根自己的索引，唤醒片只读任务 work 索引，所以前台那次成功的 create_subagents
不在续跑的去重集合里：第一次唤醒片里同样内容的派工照样成功，多出一个子代理。
锁定：
- 续跑同时读 owner 根自己的索引（owner 由任务 run_workspace.json 的结构化 owner_home 给出，且任务根必须在其内）
  和任务 work 索引，都按精确请求编号流式过滤，别的请求、别的 run 的索引不会进来；
- owner 索引的记录只标记为运行时状态：进一次性编排去重、已执行工具和工具轮数，不进本片工具账与模型可见交接；
- 片内溢出压缩替换携带内容时，这些记录保留；工作片的新增工具轮额度不因携带记录而缩水。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.parameters import _one_shot_tool_call_is_duplicate
from agent_py_agent.agent.agent_core.runtime.loop_models import (
    RuntimeLoopParams,
    RuntimeToolLoopSeed,
)
from agent_py_agent.agent.agent_core.runtime.loop_support import _tool_loop_execute_params
from agent_py_agent.agent.agent_core.tool_call_runtime import _duplicate_one_shot_result
from agent_py_agent.agent.conversation.compact_carry import compact_overflow_carry
from agent_py_agent.agent.conversation.runtime import (
    BackgroundRunRequest,
    GoalRuntimeContext,
    _goal_runtime_context,
    _run_params,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive.compact_tool_output_refs import (
    CARRIED_RUNTIME_ONLY_FIELD,
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


def test_owner_and_task_indexes_are_read_by_exact_request_only(tmp_path) -> None:
    owner, task_root = _owner_layout(tmp_path)

    records = carried_tool_call_records_for_requests(
        [(owner, True), (task_root / "work", False)], [_FOREGROUND],
    )

    # 别的请求、只在参数里提到本请求编号的记录、坏行、别的 run 的索引都不会进来。
    assert [(item["call_id"], item.get(CARRIED_RUNTIME_ONLY_FIELD)) for item in records] == [
        ("call-create", True), ("call-wake", None),
    ]
    assert carried_tool_call_records_for_requests([(owner, True)], []) == []


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
    carried = carried_tool_call_records_for_requests([(owner, True), (task_root / "work", False)], [_FOREGROUND])

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
