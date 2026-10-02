# LLM: J16 片 D 的离线验证：假 Jev 四档（off / observe / apply / apply+自动执行）、粗位置派生、自动执行的各项结构化生效条件
#   （开关、属主、唯一可执行动作、幂等、中断）、宿主调用走模型同一条执行/审批/记录链（actor=decision 进归档、runtime_events 与
#   决策账）、不进原生 IR 配对、拒绝/失败/取消都不重试不改选。决策服务边界与新鲜度权威用 test_decision_action_candidate 的替身，
#   归档与 runtime_events 走真实 archive_tool_call_record / persist_tool_runtime_ledger（假权威库 _Repo）。
# 模块用途: 钉住“宿主替主模型点一下”的全部边界，改 decision_action_execute / round_execution 的宿主动作链时先跑这里。
from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from functools import partial
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service as loop
from agent_py_agent.agent.agent_core.tool_context import decision_action_candidate as module
from agent_py_agent.agent.agent_core.tool_context import decision_action_execute as execute
from agent_py_agent.agent.agent_core.tool_loop import round_execution
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolRoundExecutionRequest
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.decision_protocol import decision_json
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.plugin_observation import (
    PluginToolObservation,
    PluginToolObservationRef,
    observation_action_payload_from_envelope,
    observation_actions,
)
from agent_py_agent.agent.tooling.action_policy import ActionDecision
from agent_py_agent.agent.tooling.executor import ToolExecution
from agent_py_agent.agent.tooling.models import ToolAvailability, ToolRuntime, ToolRuntimeSnapshot
from agent_py_agent.agent.tooling.observation_binding import ObservationBinding
from agent_py_agent.agent.tooling.runtime_contracts import (
    ToolFailureFacts,
    ToolResult,
    ToolSuccessFacts,
)
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_result,
    make_test_model_spec,
    make_test_protocol_snapshot,
    make_test_runtime_policy,
)
from agent_py_agent.tests.test_decision_action_candidate import (
    _C1,
    _C2,
    _CLICK,
    _FILL,
    _HINT_C1,
    _OBS,
    _READ,
    install,
    observation,
    prepared,
)
from agent_py_agent.tests.test_plugin_observation import _Repo

_FIXTURES = (prepared,)
_CLICK2 = "plugin__browser_lite_0a1b2c3d__press_6c7d8e9f"
_FRAME = {"space": "screen", "origin": [0, 0], "size": [800, 600], "scale": [2, 2]}
_OPERATION = "action_candidate:auto:" + _OBS
# 属主范围：(home 的 provider, kind, owner_id, 本轮快照 owner_type)
_LOCAL_MAIN = ("local", "main", "main", "main_agent")


# 函数用途: 造一个带 observation_ref（或 observation）声明的绑定，和插件线的真实类型一致。
def binding(tool, *, param="candidate_id", observation=False):
    if observation:
        return ObservationBinding("plugin:browser-lite", "activation-secret-7", tool, observation=PluginToolObservation("page", 64))
    return ObservationBinding("plugin:browser-lite", "activation-secret-7", tool, observation_ref=PluginToolObservationRef("page", param))


# 函数用途: 造一条工具运行时：schema 的 required 决定它能不能只凭候选编号调用，handler 的 observation_binding 决定它是不是动作工具。
def runtime(name, *, bound=None, required=("candidate_id",)):
    properties = {key: {"type": "string"} for key in dict.fromkeys((*required, "candidate_id"))}
    schema = {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}
    handler = SimpleNamespace(observation_binding=bound) if bound is not None else object()
    return ToolRuntime(model_spec=make_test_model_spec(name, input_schema=schema), runtime_policy=make_test_runtime_policy(),
                       handler=handler, availability=ToolAvailability.ready())


# 函数用途: 默认快照：read 普通工具、click 只凭候选编号、fill 还要文字。
def default_runtimes():
    return (runtime(_READ, required=()), runtime(_CLICK, bound=binding(_CLICK)),
            runtime(_FILL, bound=binding(_FILL), required=("candidate_id", "text")))


def snapshot(runtimes, *, owner_type="main_agent"):
    return ToolRuntimeSnapshot(run_id="run-1", runtimes=tuple(runtimes), available_tool_names=frozenset(r.model_spec.name for r in runtimes),
                               unavailable_tools=(), allowed_tools=None, owner_type=owner_type)


# LLM: 把 prepared 的宿主武装成“本机管理员主代理 + 开关可控 + 假权威库”；观察信封改从 result 的 handler_details 进真实归档，
#   让 _record_tool_call 的真实 archive_tool_call_record / persist_tool_runtime_ledger 都能跑。
# 函数用途: 准备一轮可自动执行的观察调用，返回 (host, record, repo)；scope 见 _LOCAL_MAIN，决策账写在 host.root 下。
def arm(prepared, *, enabled=True, scope=_LOCAL_MAIN, runtimes=None):
    host, record, _archive = prepared
    repo = _Repo()
    host.subagents = SimpleNamespace(runtime_db=repo)
    host.local_store = SimpleNamespace()
    host.home_paths = SimpleNamespace(owner_provider=scope[0], owner_kind=scope[1], owner_id=scope[2],
                                      owner_decision_outcomes_jsonl=host.root / "decision-outcomes.jsonl")
    host._capability_config_runtime_snapshot = SimpleNamespace(config=CapabilityConfig(action_candidate_auto_execute_enabled=enabled))
    object.__setattr__(record.params, "tool_runtime_snapshot", snapshot(runtimes or default_runtimes(), owner_type=scope[3]))
    record.params.archive_tool_calls.clear()
    result = canonical_history_result(record.call, '{"items": ["page-output"]}', ok=True,
                                      handler_details={"observation": observation()})
    return host, replace(record, result=result), repo


# 函数用途: 关掉记录链里与本片无关的副作用（护栏、收口标记、实时归档、进度、长内容），归档与权威事件照真。
def quiet_recording(monkeypatch):
    monkeypatch.setattr(loop, "record_tool_guard_observation", lambda *_args: "")
    monkeypatch.setattr(loop, "_mark_repeated_failure_halt", lambda *_args: None)
    monkeypatch.setattr(loop, "_mark_unknown_outcome_halt", lambda *_args: None)
    monkeypatch.setattr(loop, "archive_tool_call_if_enabled", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(loop, "update_runtime_fact_progress_if_enabled", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(loop, "append_long_content_recovery_context", lambda *_args: None)
    monkeypatch.setattr(loop, "record_runtime_subagent_tool_progress", lambda *_args: "")


# 函数用途: 假执行器：记下每次调用，按 outcomes 依次返回成功 / 失败 / 待审批；默认成功并带动作事实（真实绑定会这么写信封）。
class Executor:
    def __init__(self, *outcomes):
        self.calls, self.outcomes = [], list(outcomes)

    def __call__(self, params):
        self.calls.append(params)
        kind = self.outcomes.pop(0) if self.outcomes else "ok"
        call = params.call
        fact = {"observation_action": {"observation_id": _OBS, "candidate_id": call.arguments.get("candidate_id", ""), "tool": call.tool_name}}
        if kind == "ask":
            result = ToolResult.failed(call, "approval required", error_code="APPROVAL_REQUIRED", failure_stage="authorization",
                                       facts=ToolFailureFacts(status="approval_required", handler_executed=False, effect_outcome="not_started"))
            return ToolExecution(call, ActionDecision("ask", ("APPROVAL_REQUIRED",), approval_request={"tool_name": call.tool_name},
                                                      resolved_effect="dangerous"), result, ("received", "approval_pending"))
        if kind == "fail":
            result = ToolResult.failed(call, '{"error": "stale"}', error_code="TOOL_INVALID_ARGUMENTS", failure_stage="validation",
                                       facts=ToolFailureFacts(handler_executed=False, effect_outcome="not_started",
                                                              metadata={"handler_details": {**fact, "observation_rejected": "OBSERVATION_STALE"}}))
            return ToolExecution(call, ActionDecision("deny", ("TOOL_INVALID_ARGUMENTS",)), result, ("received", "failed"))
        result = ToolResult.succeeded(call, '{"clicked": true}', facts=ToolSuccessFacts(effect_outcome="confirmed",
                                                                                      metadata={"handler_details": fact}))
        return ToolExecution(call, ActionDecision("allow", resolved_effect="dangerous"), result, ("received", "succeeded"))


# LLM: 走产品的 round_execution._record_execution：观察调用记录 → 决策点规划 → 宿主动作执行/审批/记录，record_one 是真实
#   _record_tool_call；只替换执行器。
# 函数用途: 记录一次观察调用并让宿主动作链跑完，返回轮次请求。
def run_round(host, record, executor):
    request = ToolRoundExecutionRequest(agent=host, params=record.params, tool_rounds=record.tool_rounds,
                                        response=ModelResponse(text="", backend="test"), calls=[record.call],
                                        execute_one=executor, record_one=partial(loop._record_tool_call, host))
    execution = ToolExecution(record.call, ActionDecision("allow"), record.result, ("received", "succeeded"))
    round_execution._record_execution(request, record.idx, time.monotonic(), execution)
    return request


def ledger_rows(host):
    path = host.home_paths.owner_decision_outcomes_jsonl
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def auto_rows(host):
    return [row for row in ledger_rows(host) if row.get("record_kind") == execute.AUTO_RECORD_KIND]


def host_blocks(record):
    return [item for item in record.params.tool_context if item.startswith("[host-action-record")]


def tool_results(record):
    return [item for item in record.params.tool_ir_history if isinstance(item, ToolResult)]


# ── 粗位置（D1）──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("frame,region,expected", [
    (_FRAME, [200, 300, 160, 300], {"x": 0.2, "y": 0.4}),          # 中心 (280, 450) / (1600, 1200) = (0.175, 0.375)
    (_FRAME, [1500, 1100, 400, 400], {"x": 1.0, "y": 1.0}),         # 越界先夹到 1
    (_FRAME, [0, 0, 1, 1], {"x": 0.0, "y": 0.0}),                   # 下界夹到 0
    (_FRAME, [526, 0, 2, 2], {"x": 0.3, "y": 0.0}),                 # 0.329 四舍五入到一位
    (None, [160, 300, 160, 300], None),                             # 没有 frame 没有位置
    (_FRAME, None, None),                                           # 没有 region 没有位置
    ({"size": [800, 600], "scale": [0, 2]}, [1, 1, 1, 1], None),    # 缩放非正不给位置
    ({"size": [800, 600]}, [1, 1, 1, 1], None),                     # 缺 scale 不给位置
    (_FRAME, [1, 2, 3], None),                                      # region 不是 4 元
    (_FRAME, ["a", 2, 3, 4], None),                                 # 非数字
], ids=["center", "clamp_high", "clamp_low", "round", "no_frame", "no_region", "zero_scale", "missing_scale", "short_region", "nan"])
def test_coarse_position_normalizes_clamps_and_rounds(frame, region, expected):
    assert execute.coarse_position(frame, region) == expected


def test_material_sends_position_only_for_candidates_with_region_and_frame(prepared, monkeypatch):
    host, record, archive = prepared
    archive["tool_result_envelope"]["observation"]["frame"] = dict(_FRAME)
    archive["tool_result_envelope"]["observation"]["candidates"][0]["region"] = [200, 300, 160, 300]
    calls = install(monkeypatch, choice="not_needed")
    module.action_candidate_hint(host, record, archive)
    candidates = calls[0][2]["state"]["candidates"]
    assert decision_json({"position": {"x": 0.2, "y": 0.4}}).decode("utf-8")[1:-1] in candidates
    assert candidates.count("position") == 1, "没有 region 的候选不带位置"
    for private in ("200", "300", "160", "800", "origin", "screen"):
        assert private not in candidates, "绝对坐标、尺寸与坐标空间都不外发"


def test_material_without_frame_sends_no_position(prepared, monkeypatch):
    host, record, archive = prepared
    archive["tool_result_envelope"]["observation"]["candidates"][0]["region"] = [200, 300, 160, 300]
    calls = install(monkeypatch, choice="not_needed")
    module.action_candidate_hint(host, record, archive)
    assert "position" not in calls[0][2]["state"]["candidates"]


# ── 假 Jev 四档 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode,auto", [("off", False), ("observe", False), ("apply", False), ("apply", True)],
                         ids=["off", "observe", "apply", "apply+auto"])
def test_four_modes_only_apply_with_the_switch_executes(prepared, monkeypatch, mode, auto):
    host, record, repo = arm(prepared, enabled=auto)
    quiet_recording(monkeypatch)
    calls = install(monkeypatch, mode=mode, choice="c1")
    executor = Executor()
    run_round(host, record, executor)
    blocks = host_blocks(record)
    executed = [params.call for params in executor.calls]
    summary = {"mode": mode + ("+auto" if auto else ""), "requests": len(calls), "hint": record.params.tool_context[0].count("[action-candidate]"),
               "executed": [(call.tool_name, dict(call.arguments)) for call in executed], "host_records": len(blocks),
               "auto_rows": [row["result_category"] for row in auto_rows(host)]}
    if os.environ.get("MY_AGENT_J16D_REPORT"):
        print("J16D_MODE " + json.dumps(summary, ensure_ascii=False))
    assert summary["requests"] == (0 if mode == "off" else 1)
    assert summary["hint"] == (1 if mode == "apply" else 0)
    if not auto:
        assert executed == [] and blocks == [] and auto_rows(host) == []
        if mode == "apply":
            assert record.params.tool_context[0].endswith(_HINT_C1), "开关关着只给原提示"
        return
    assert [(call.tool_name, call.arguments, call.operation_id) for call in executed] == [(_CLICK, {"candidate_id": _C1}, _OPERATION)]
    assert "宿主将按建议以 actor=decision 自动执行候选 " + _C1 in record.params.tool_context[0]
    assert len(blocks) == 1 and blocks[0].startswith("[host-action-record round=2 index=2 actor=decision]\n")
    assert "[host-action-output-record round=2 index=2 actor=decision]" in blocks[0]
    assert record.params.host_actions == []
    assert [row["auto_execution"]["ok"] for row in auto_rows(host)] == [True]


# ── 两本账都记 actor=decision 与决策结果编号 ────────────────────────────────

def test_both_ledgers_carry_actor_and_decision_ref(prepared, monkeypatch):
    host, record, repo = arm(prepared)
    quiet_recording(monkeypatch)
    install(monkeypatch, choice="c1")
    run_round(host, record, Executor())
    archives = record.params.archive_tool_calls
    assert [(item["tool"], item["actor"]) for item in archives] == [(_READ, "model"), (_CLICK, "decision")]
    host_archive = archives[-1]
    decision_ref = host_archive["decision_ref"]
    assert decision_ref.startswith(module._POINT + ":") and "#" in decision_ref and archives[0]["decision_ref"] == ""
    assert host_archive["operation_id"] == _OPERATION and host_archive["id"] == "host-action-" + _OBS
    assert host_archive["tool_result_envelope"]["observation_action"] == {"observation_id": _OBS, "candidate_id": _C1, "tool": _CLICK}
    payloads = [event["payload"] for event in repo.events]
    assert [(p["tool"], p["actor"], p["decision_ref"]) for p in payloads] == [(_READ, "model", ""), (_CLICK, "decision", decision_ref)]
    assert payloads[-1]["observation_action"] == {"observation_id": _OBS, "candidate_id": _C1, "tool": _CLICK, "task_id": "task-1"}
    assert observation_actions(repo, run_id="run-1", task_id="task-1") == [
        {"observation_id": _OBS, "candidate_id": _C1, "tool": _CLICK, "task_id": "task-1", "actor": "decision"}]
    rows = auto_rows(host)
    assert len(rows) == 1 and rows[0]["decision_ref"] == decision_ref and rows[0]["actor"] == "decision"
    assert rows[0]["auto_execution"] == {"observation_id": _OBS, "candidate_id": _C1, "executed": True, "operation_id": _OPERATION,
                                         "tool": _CLICK, "ok": True, "error_code": "", "reported_error_code": "",
                                         "effect_outcome": "confirmed", "handler_executed": True, "status": "succeeded"}
    assert rows[0]["result_category"] == "auto_execution:executed" and rows[0]["point"] == module._POINT
    assert "requested_model" not in rows[0]


@pytest.mark.parametrize("protocol", ["native", "text"])
def test_host_record_never_enters_native_ir_but_reaches_the_text_chain(prepared, monkeypatch, protocol):
    host, record, _repo = arm(prepared)
    object.__setattr__(record.params, "tool_protocol_snapshot", make_test_protocol_snapshot(run_id="run-1", source_protocol=protocol))
    quiet_recording(monkeypatch)
    install(monkeypatch, choice="c1")
    run_round(host, record, Executor())
    assert [item.call_id for item in tool_results(record)] == (["read-1"] if protocol == "native" else [])
    assert len(host_blocks(record)) == 1, "宿主记录只经 [host-action-record] 块进模型上下文"


# ── 幂等与“模型已动作” ───────────────────────────────────────────────────────

def test_second_round_on_the_same_observation_does_not_execute_again(prepared, monkeypatch):
    host, record, repo = arm(prepared)
    quiet_recording(monkeypatch)
    install(monkeypatch, choice="c1")
    executor = Executor()
    run_round(host, record, executor)
    run_round(host, record, executor)
    assert len(executor.calls) == 1
    assert [row["result_category"] for row in auto_rows(host)] == ["auto_execution:executed", "auto_execution:skipped:already_acted"]


def test_model_action_on_the_observation_blocks_host_execution(prepared, monkeypatch):
    host, record, repo = arm(prepared)
    quiet_recording(monkeypatch)
    repo.append_event(event_type="tool_completed", attempt_id="attempt-1", agent_run_id="agentrun-run-1", payload={
        "operation_id": "tool_operation:model-click", "tool": _CLICK, "ok": False, "actor": "model",
        "observation_action": {"observation_id": _OBS, "candidate_id": _C2, "tool": _CLICK, "task_id": "task-1"}})
    install(monkeypatch, choice="c1")
    executor = Executor()
    run_round(host, record, executor)
    assert executor.calls == [] and host_blocks(record) == []
    assert [row["auto_execution"]["reason"] for row in auto_rows(host)] == [execute.SKIP_ALREADY_ACTED]
    assert record.params.tool_context[0].endswith(_HINT_C1), "没执行时提示退回原来的软提示"


def test_model_action_in_another_task_does_not_count(prepared, monkeypatch):
    host, record, repo = arm(prepared)
    quiet_recording(monkeypatch)
    repo.append_event(event_type="tool_completed", attempt_id="attempt-1", agent_run_id="agentrun-run-1", payload={
        "operation_id": "op", "tool": _CLICK, "ok": True, "actor": "model",
        "observation_action": {"observation_id": _OBS, "candidate_id": _C1, "tool": _CLICK, "task_id": "task-9"}})
    install(monkeypatch, choice="c1")
    executor = Executor()
    run_round(host, record, executor)
    assert len(executor.calls) == 1


# ── 生效条件：唯一可执行动作、属主、开关 ──────────────────────────────────────

def test_candidate_whose_only_action_needs_text_is_not_executed(prepared, monkeypatch):
    host, record, _repo = arm(prepared)
    quiet_recording(monkeypatch)
    install(monkeypatch, choice="c2")  # c2 的动作是 fill：required 含 text
    executor = Executor()
    run_round(host, record, executor)
    assert executor.calls == [] and [row["auto_execution"]["reason"] for row in auto_rows(host)] == [execute.SKIP_NO_AUTO_ACTION]


def test_two_qualifying_actions_are_ambiguous_and_nothing_runs(prepared, monkeypatch):
    runtimes = (*default_runtimes(), runtime(_CLICK2, bound=binding(_CLICK2)))
    host, record, _repo = arm(prepared, runtimes=runtimes)
    record = replace(record, result=canonical_history_result(record.call, "{}", ok=True, handler_details={
        "observation": observation(candidates=[
            {"candidate_id": _C1, "key": "key-e5", "role": "button", "label": "提交订单", "actions": [_CLICK, _CLICK2]},
            {"candidate_id": _C2, "key": "key-e9", "role": "link", "label": "帮助", "actions": [_CLICK]}])}))
    quiet_recording(monkeypatch)
    install(monkeypatch, choice="c1")
    executor = Executor()
    run_round(host, record, executor)
    assert executor.calls == [] and [row["auto_execution"]["reason"] for row in auto_rows(host)] == [execute.SKIP_AMBIGUOUS_ACTION]


@pytest.mark.parametrize("variant", ["plain_handler", "observation_binding", "ref_other_param"])
def test_action_tool_without_a_candidate_only_binding_is_not_executed(prepared, monkeypatch, variant):
    click = {"plain_handler": runtime(_CLICK), "observation_binding": runtime(_CLICK, bound=binding(_CLICK, observation=True)),
             "ref_other_param": runtime(_CLICK, bound=binding(_CLICK, param="target_id"))}[variant]
    host, record, _repo = arm(prepared, runtimes=(runtime(_READ, required=()), click, default_runtimes()[2]))
    quiet_recording(monkeypatch)
    install(monkeypatch, choice="c1")
    executor = Executor()
    run_round(host, record, executor)
    assert executor.calls == [] and [row["auto_execution"]["reason"] for row in auto_rows(host)] == [execute.SKIP_NO_AUTO_ACTION]


@pytest.mark.parametrize("scope", [
    ("local", "main", "main", "user"), ("feishu", "user", "ou_1", "main_agent"), ("local", "main", "", "main_agent"),
    ("", "", "", "main_agent"),
], ids=["snapshot_user", "feishu_owner", "missing_owner_id", "empty_identity"])
def test_only_the_complete_local_main_owner_executes(prepared, monkeypatch, scope):
    host, record, _repo = arm(prepared, scope=scope)
    quiet_recording(monkeypatch)
    install(monkeypatch, choice="c1")
    executor = Executor()
    run_round(host, record, executor)
    assert executor.calls == [] and [row["auto_execution"]["reason"] for row in auto_rows(host)] == [execute.SKIP_OWNER_SCOPE]


@pytest.mark.parametrize("config", [
    SimpleNamespace(action_candidate_auto_execute_enabled=True),      # 替身对象上的“真值”不是配置
    CapabilityConfig(action_candidate_auto_execute_enabled="yes"),    # 真配置但不是 bool True：只认 True
    CapabilityConfig(action_candidate_auto_execute_enabled=1),
], ids=["fake_config", "truthy_string", "truthy_int"])
def test_switch_is_read_only_as_true_from_the_capability_config(prepared, monkeypatch, config):
    host, record, _repo = arm(prepared)
    host._capability_config_runtime_snapshot = SimpleNamespace(config=config)
    quiet_recording(monkeypatch)
    install(monkeypatch, choice="c1")
    executor = Executor()
    run_round(host, record, executor)
    assert executor.calls == [] and auto_rows(host) == [], "不是 True 就按关处理且不记账"
    assert CapabilityConfig().action_candidate_auto_execute_enabled is False


def test_switch_lives_in_capability_config_as_an_admin_boundary():
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry
    from agent_py_agent.agent.settings.user_config_capability import USER_SETTINGS_BOUNDARY_KEYS

    assert not hasattr(AgentConfig(), "action_candidate_auto_execute_enabled")
    spec = parameter_registry()["action_candidate_auto_execute_enabled"]
    assert spec.default is False and spec.writable is False and spec.source == "capability"
    assert "action_candidate_auto_execute_enabled" in USER_SETTINGS_BOUNDARY_KEYS


# ── 审批、失败、中断：只记录，不重试，不改选 ────────────────────────────────

def test_ask_mode_approval_carries_actor_decision_and_denial_is_recorded_once(prepared, monkeypatch):
    host, record, _repo = arm(prepared)
    requests = []

    class Consumer:
        def request_permission(self, payload, *, cancellation_token=None):
            requests.append(payload)
            return {"permission_id": payload["permission_id"], "decision": "denied"}

    object.__setattr__(record.params, "effective_on_chunk", Consumer())
    quiet_recording(monkeypatch)
    install(monkeypatch, choice="c1")
    executor = Executor("ask")
    run_round(host, record, executor)
    assert len(requests) == 1 and len(executor.calls) == 1, "拒绝后不再执行、不再询问"
    binding_facts = requests[0]["binding"]
    assert binding_facts["actor"] == "decision" and binding_facts["decision_ref"] == record.params.archive_tool_calls[-1]["decision_ref"]
    assert binding_facts["operation_id"] == _OPERATION and binding_facts["tool_name"] == _CLICK
    assert requests[0]["description"].startswith("[决策自动执行 actor=decision] " + _CLICK)
    host_archive = record.params.archive_tool_calls[-1]
    assert host_archive["actor"] == "decision" and host_archive["error_code"] == "APPROVAL_REJECTED" and host_archive["handler_executed"] is False
    rows = auto_rows(host)
    assert [(row["auto_execution"]["ok"], row["auto_execution"]["error_code"], row["auto_execution"]["handler_executed"]) for row in rows] == [
        (False, "APPROVAL_REJECTED", False)]
    assert record.params.host_actions == []


def test_model_approval_requests_do_not_carry_an_actor(prepared, monkeypatch):
    host, record, _repo = arm(prepared, enabled=False)
    requests = []

    class Consumer:
        def request_permission(self, payload, *, cancellation_token=None):
            requests.append(payload)
            return {"permission_id": payload["permission_id"], "decision": "denied"}

    object.__setattr__(record.params, "effective_on_chunk", Consumer())
    request = ToolRoundExecutionRequest(agent=host, params=record.params, tool_rounds=2, response=ModelResponse(text="", backend="test"),
                                        calls=[record.call], execute_one=Executor("ask"), record_one=lambda _record: None)
    execution = request.execute_one(SimpleNamespace(call=record.call))
    round_execution._resolve_tool_approval(request, 1, record.call, execution)
    assert "actor" not in requests[0]["binding"] and "decision_ref" not in requests[0]["binding"]
    assert not requests[0]["description"].startswith("[决策自动执行")


def test_rejected_recheck_is_recorded_without_retry_or_another_candidate(prepared, monkeypatch):
    host, record, repo = arm(prepared)
    quiet_recording(monkeypatch)
    install(monkeypatch, choice="c1")
    executor = Executor("fail")
    run_round(host, record, executor)
    assert [(params.call.tool_name, params.call.arguments) for params in executor.calls] == [(_CLICK, {"candidate_id": _C1})]
    rows = auto_rows(host)
    assert [(row["auto_execution"]["ok"], row["auto_execution"]["error_code"], row["auto_execution"]["effect_outcome"]) for row in rows] == [
        (False, "TOOL_INVALID_ARGUMENTS", "not_started")]
    assert record.params.host_actions == [] and len(host_blocks(record)) == 1
    assert observation_actions(repo, run_id="run-1", task_id="task-1")[-1]["candidate_id"] == _C1, "失败的发送也算碰过这个观察"
    run_round(host, record, executor)
    assert len(executor.calls) == 1, "失败后不重试，也不换候选"


def test_interrupted_before_the_host_action_runs_nothing(prepared, monkeypatch):
    host, record, _repo = arm(prepared)
    quiet_recording(monkeypatch)
    install(monkeypatch, choice="c1")
    executor = Executor()

    def record_then_stop(item):
        loop._record_tool_call(host, item)
        item.params.cancellation_token.cancel("stop")

    request = ToolRoundExecutionRequest(agent=host, params=record.params, tool_rounds=record.tool_rounds,
                                        response=ModelResponse(text="", backend="test"), calls=[record.call],
                                        execute_one=executor, record_one=record_then_stop)
    execution = ToolExecution(record.call, ActionDecision("allow"), record.result, ("received", "succeeded"))
    round_execution._record_execution(request, record.idx, time.monotonic(), execution)
    assert executor.calls == [] and record.params.host_actions == []
    assert [row["auto_execution"]["reason"] for row in auto_rows(host)] == [execute.SKIP_INTERRUPTED]


def test_host_actions_are_only_run_for_model_records(prepared, monkeypatch):
    host, record, _repo = arm(prepared)
    executor = Executor()
    request = ToolRoundExecutionRequest(agent=host, params=record.params, tool_rounds=2, response=ModelResponse(text="", backend="test"),
                                        calls=[record.call], execute_one=executor, record_one=lambda _record: None, actor="decision")
    selection = execute.AutoExecutionSelection(module._POINT, SimpleNamespace(operation_id="op"), SimpleNamespace(response=None),
                                               {"observation_id": _OBS}, {"candidate_id": _C1})
    record.params.host_actions.append(execute.HostAction(replace(record.call, tool_name=_CLICK), selection))
    round_execution._run_host_actions(request, 1)
    assert executor.calls == [] and len(record.params.host_actions) == 1, "actor=decision 的请求不再取计划（防递归）"


# ── 事实投影 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [None, "x", {}, {"observation_id": _OBS, "candidate_id": _C1}, {"observation_id": "", "candidate_id": _C1, "tool": _CLICK},
                                   {"observation_id": _OBS, "candidate_id": 3, "tool": _CLICK}])
def test_action_fact_projection_rejects_incomplete_shapes(value):
    assert observation_action_payload_from_envelope(value, task_id="task-1") is None


def test_action_fact_projection_keeps_only_host_fields():
    value = {"observation_id": _OBS, "candidate_id": _C1, "tool": _CLICK, "label": "提交", "x": 1}
    assert observation_action_payload_from_envelope(value, task_id="task-1") == {
        "observation_id": _OBS, "candidate_id": _C1, "tool": _CLICK, "task_id": "task-1"}


def test_observation_actions_skip_other_runs_and_malformed_payloads():
    repo = _Repo()
    repo.append_event(event_type="tool_completed", attempt_id="a", agent_run_id="agentrun-run-1", payload={"ok": True})
    repo.append_event(event_type="tool_completed", attempt_id="a", agent_run_id="agentrun-run-1", payload={"ok": False, "observation_action": "bad"})
    repo.append_event(event_type="tool_completed", attempt_id="a", agent_run_id="agentrun-run-1",
                      payload={"ok": False, "observation_action": {"observation_id": _OBS, "candidate_id": _C1, "tool": _CLICK, "task_id": "task-1"}})
    assert observation_actions(repo, run_id="run-1", task_id="task-1") == [
        {"observation_id": _OBS, "candidate_id": _C1, "tool": _CLICK, "task_id": "task-1", "actor": "model"}]
    assert observation_actions(repo, run_id="run-2", task_id="task-1") == [] and observation_actions(None, run_id="run-1", task_id="") == []
