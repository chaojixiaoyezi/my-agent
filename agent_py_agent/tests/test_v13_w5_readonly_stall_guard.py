"""只读空转守卫必须经过正式调用记录入口，不能只验证独立计数器。"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service as loop
from agent_py_agent.agent.tooling.runtime_contracts import (
    ToolFailureFacts,
    ToolResult,
    ToolSuccessFacts,
)
from agent_py_agent.tests._tool_runtime_harness import canonical_history_call
from agent_py_agent.tests.test_tool_output_externalizer import _tool_loop_params


def _runtime(tmp_path, *, attrs=None, scope="task_local", hint=30, halt=80):
    config = tmp_path / "capability.yaml"
    config.write_text(
        f"subagent_readonly_stall_hint_calls: {hint}\nsubagent_readonly_stall_halt_calls: {halt}\n",
        encoding="utf-8",
    )
    agent = SimpleNamespace(root=tmp_path, capability_config_path=config)
    params = replace(
        _tool_loop_params(request_id="req-stall", run_id="child-stall", task_id="task-stall"),
        context_scope=scope,
        task_attributes={"required_files": ["product.py"]} if attrs is None else attrs,
    )
    return agent, params


def _record(agent, params, index, *, tool=None, effect="read_only", ok=True, progress=None, output=None):
    tool = tool or ("read_file" if index % 2 else "search_text")
    call = canonical_history_call(
        tool, {"path": f"source-{index}.py"}, call_id=f"call-{index}", source_protocol="native",
        run_id=params.run_id, turn_id=f"turn-{index // 4}", attempt_id="attempt-stall",
    )
    metadata = {"action_decision": {"resolved_effect": effect}, "handler_details": progress or {}}
    if ok:
        result = ToolResult.succeeded(
            call, f"observed {index}" if output is None else output, facts=ToolSuccessFacts(metadata=metadata),
        )
    else:
        result = ToolResult.failed(
            call, "写入被拒", error_code="WRITE_FORBIDDEN", failure_stage="authorization",
            facts=ToolFailureFacts(handler_executed=False, metadata=metadata),
        )
    record = loop.ToolCallRecordParams(params=params, tool_rounds=index // 4 + 1, idx=index, call=call, result=result)
    loop._record_tool_call(agent, record)
    return record


def _reads(agent, params, start, stop):
    for index in range(start, stop):
        _record(agent, params, index)


def _hints(params):
    return [entry for entry in params.tool_context if entry.startswith("[subagent-readonly-stall-hint]")]


def test_real_record_entry_halts_after_80_varied_reads_and_hints_once(tmp_path):
    agent, params = _runtime(tmp_path)
    _reads(agent, params, 1, 30)
    assert not _hints(params)
    _record(agent, params, 30)
    assert len(_hints(params)) == 1
    hint = json.loads(_hints(params)[0].split("\n", 1)[1])
    assert hint["code"] == "SUBAGENT_READONLY_STALL"
    assert hint["readonly_calls"] == 30
    assert hint["run_id"] == params.run_id
    _reads(agent, params, 31, 80)
    assert params.repeated_failure_halt is None
    _record(agent, params, 80)
    assert params.repeated_failure_halt == ("search_text", "code:SUBAGENT_READONLY_STALL", 80)
    assert params.repeated_failure_halt_exhausted is True
    assert len(_hints(params)) == 1
    assert len(params.archive_tool_calls) == 80


@pytest.mark.parametrize("ok", [True, False])
@pytest.mark.parametrize("tool", ["write_file", "edit_file", "apply_patch", "run_command"])
def test_write_attempt_at_79_resets_even_when_denied(tmp_path, tool, ok):
    agent, params = _runtime(tmp_path)
    _reads(agent, params, 1, 80)
    _record(agent, params, 80, tool=tool, effect="mutating", ok=ok)
    _reads(agent, params, 81, 160)
    assert params.repeated_failure_halt is None
    assert len(_hints(params)) == 2
    _record(agent, params, 160)
    assert params.repeated_failure_halt[1:] == ("code:SUBAGENT_READONLY_STALL", 80)


@pytest.mark.parametrize("attrs,scope", [
    ({}, "task_local"),
    ({"goal": "写 product.py", "allowed_write_roots": ["/tmp"], "allowed_tools": ["write_file"]}, "task_local"),
    ({"output_files": ["output.json"], "system_default_output_ref": True}, "task_local"),
    ({"required_files": ["product.py"]}, "default"),
])
def test_no_explicit_product_obligation_or_not_child_never_halts(tmp_path, attrs, scope):
    agent, params = _runtime(tmp_path, attrs=attrs, scope=scope)
    _reads(agent, params, 1, 90)
    assert params.repeated_failure_halt is None
    assert not _hints(params)


@pytest.mark.parametrize("key", ["required_files", "required_file_refs", "output_files", "output_refs", "artifact_refs"])
def test_canonical_explicit_output_fields_enable_guard(tmp_path, key):
    agent, params = _runtime(tmp_path, attrs={key: ["product.py"]}, hint=1, halt=2)
    _reads(agent, params, 1, 3)
    assert params.repeated_failure_halt[1:] == ("code:SUBAGENT_READONLY_STALL", 2)


@pytest.mark.parametrize("hint,halt", [(0, 0), (0, 3), (2, 0)])
def test_thresholds_independently_disable_and_use_capability_config(tmp_path, hint, halt):
    agent, params = _runtime(tmp_path, hint=hint, halt=halt)
    _reads(agent, params, 1, 4)
    assert len(_hints(params)) == (1 if hint else 0)
    assert (params.repeated_failure_halt is not None) is bool(halt)


# LLM: 阶段文件需要先有观察基线，心跳和完成文案均不提供更新证据。
# 函数用途: 强化旧用例为真实新建文件，并保留心跳反例。
def test_structured_stage_progress_resets_but_heartbeat_does_not(tmp_path, monkeypatch):
    product = tmp_path / "checkpoint.json"
    agent, params = _runtime(tmp_path, attrs={"required_files": [str(product)]}, hint=2, halt=4)
    _reads(agent, params, 1, 4)
    product.write_text('{"stage": 1}', encoding="utf-8")
    monkeypatch.setattr(loop, "record_runtime_subagent_tool_progress", lambda *args: {
        "schema_version": "subagent_tool_progress.v1", "latest_written_path": str(product),
    })
    _record(agent, params, 4)
    monkeypatch.setattr(loop, "record_runtime_subagent_tool_progress", lambda *args: {
        "run_id": params.run_id, "last_progress_at": 1234, "summary": "完成了文件",
    })
    _reads(agent, params, 5, 8)
    assert params.repeated_failure_halt is None
    _record(agent, params, 8)
    assert params.repeated_failure_halt[1:] == ("code:SUBAGENT_READONLY_STALL", 4)


def test_counter_survives_params_replace_but_not_new_run(tmp_path):
    agent, params = _runtime(tmp_path, hint=2, halt=4)
    _reads(agent, params, 1, 3)
    params = replace(params, tool_context=[])
    _reads(agent, params, 3, 5)
    assert params.repeated_failure_halt[1:] == ("code:SUBAGENT_READONLY_STALL", 4)
    new_params = replace(params, run_id="new-child", repeated_failure_halt=None, repeated_failure_halt_exhausted=False)
    _record(agent, new_params, 5)
    assert new_params.repeated_failure_halt is None


def test_stall_closeout_is_blocked_with_exact_reason(tmp_path, monkeypatch):
    agent, params = _runtime(tmp_path, hint=0, halt=1)
    _record(agent, params, 1)
    from agent_py_agent.agent.backends import ModelResponse
    reasons = []

    def closeout(*args, **kwargs):
        reasons.append(kwargs["runtime_reason"])
        return "prompt", ModelResponse(text="交接", backend="test")

    monkeypatch.setattr(loop, "_final_response_after_halt", closeout)
    _, response = loop._final_response_after_repeated_failure(agent, params, 1)
    assert reasons == ["SUBAGENT_READONLY_STALL"]
    assert response.runtime_status == "blocked"
    assert response.turn_end_reason == "blocked"


# LLM: 真实 progress 投影必须结合合同文件在观察窗口内的新建事实。
# 函数用途: 经正式记录入口核验阶段版本更新及磁盘投影。
def test_real_stage_progress_projection_resets_through_record_entry(tmp_path):
    from agent_py_agent.agent.subagents.model_task import SubAgentTask

    product = tmp_path / "checkpoint.txt"
    agent, params = _runtime(tmp_path, attrs={"required_files": [str(product)]}, hint=2, halt=4)
    task = SubAgentTask(id=params.run_id, goal="", thought="", plan=[],
                        agent_run_workspace_dir=str(tmp_path / "child"))
    saved = []
    agent.subagents = SimpleNamespace(load=lambda run_id: task, save=lambda value: saved.append(value))
    _reads(agent, params, 1, 4)
    product.write_text("已产出的阶段数据", encoding="utf-8")
    _record(agent, params, 4, output=json.dumps({"product_path": str(product)}))
    projection = tmp_path / "child" / "progress" / "latest_tool_progress.json"
    assert json.loads(projection.read_text())["latest_written_path"] == str(product)
    assert saved
    _reads(agent, params, 5, 8)
    assert params.repeated_failure_halt is None
    _record(agent, params, 8)
    assert params.repeated_failure_halt[1:] == ("code:SUBAGENT_READONLY_STALL", 4)


# LLM: 使用真实 session_progress 投影，避免只在独立函数中模拟路径可信性。
# 函数用途: 给正式 record 入口提供隔离的子任务存取器。
def _attach_progress_task(agent, params, tmp_path):
    from agent_py_agent.agent.subagents.model_task import SubAgentTask

    task = SubAgentTask(id=params.run_id, goal="", thought="", plan=[],
                        agent_run_workspace_dir=str(tmp_path / "child"))
    agent.subagents = SimpleNamespace(load=lambda run_id: task, save=lambda value: None)


@pytest.mark.parametrize("path_state", ["missing", "existing", "directory"])
@pytest.mark.parametrize("explicit", [False, True])
# LLM: JSON product_path 只是候选，首次遇到既有文件或不存在路径不是版本更新。
# 函数用途: 复现伪造路径清零漏洞，要求连续报告仍在第四次只读触发熔断。
def test_reported_path_without_version_update_cannot_reset(tmp_path, path_state, explicit):
    product = tmp_path / "checkpoint.txt"
    if path_state == "existing":
        product.write_text("旧数据", encoding="utf-8")
    elif path_state == "directory":
        product.mkdir()
    attrs = {"required_files": [str(product)]} if explicit else None
    agent, params = _runtime(tmp_path, attrs=attrs, hint=2, halt=4)
    _attach_progress_task(agent, params, tmp_path)
    _record(agent, params, 1)
    for index in range(2, 5):
        _record(agent, params, index, output=json.dumps({"product_path": str(product)}))
    assert params.live_archive_state["subagent_readonly_stall"]["readonly_calls"] == 4
    assert params.repeated_failure_halt[1:] == ("code:SUBAGENT_READONLY_STALL", 4)
    assert len(_hints(params)) == 1


@pytest.mark.parametrize("preexisting", [False, True])
# LLM: 合同路径在首轮有基线；真正新建或改写只能为该版本清零一次。
# 函数用途: 证明真实版本变化能重置，而重复报告同一版本不能继续清零。
def test_contract_file_version_update_resets_once(tmp_path, preexisting):
    product = tmp_path / "checkpoint.txt"
    if preexisting:
        product.write_text("旧数据", encoding="utf-8")
    agent, params = _runtime(tmp_path, attrs={"required_files": [str(product)]}, hint=2, halt=4)
    _attach_progress_task(agent, params, tmp_path)
    _reads(agent, params, 1, 4)
    product.write_text("新的阶段数据，比旧内容更长", encoding="utf-8")
    report = json.dumps({"product_path": str(product)})
    _record(agent, params, 4, output=report)
    assert params.live_archive_state["subagent_readonly_stall"]["readonly_calls"] == 0
    for index in range(5, 9):
        _record(agent, params, index, output=report)
    assert params.repeated_failure_halt[1:] == ("code:SUBAGENT_READONLY_STALL", 4)
    assert len(_hints(params)) == 2


# LLM: 非合同阶段路径只能从首次观察建立基线，不靠文件存在本身推断刚写入。
# 函数用途: 证明已观察的普通阶段文件后续更新可重置计数。
def test_observed_stage_path_requires_later_version_change(tmp_path):
    product = tmp_path / "stage.txt"
    product.write_text("旧数据", encoding="utf-8")
    agent, params = _runtime(tmp_path, hint=2, halt=4)
    _attach_progress_task(agent, params, tmp_path)
    _record(agent, params, 1)
    report = json.dumps({"product_path": str(product)})
    _record(agent, params, 2, output=report)
    assert params.live_archive_state["subagent_readonly_stall"]["readonly_calls"] == 2
    product.write_text("新阶段的真实内容", encoding="utf-8")
    _record(agent, params, 3, output=report)
    assert params.live_archive_state["subagent_readonly_stall"]["readonly_calls"] == 0
    for index in range(4, 8):
        _record(agent, params, index, output=report)
    assert params.repeated_failure_halt[1:] == ("code:SUBAGENT_READONLY_STALL", 4)


@pytest.mark.parametrize("projection_name", ["latest_tool_progress.json", "tool_progress.jsonl"])
# LLM: 保留真实session_progress写投影；观察器自己的活动不是业务产物。
# 函数用途: 连续只读自报latest或ledger时仍应熔断，不能把宿主写入清零。
def test_host_progress_projection_is_not_product_progress(tmp_path, projection_name):
    product = tmp_path / "product.txt"
    product.write_text("合同产物一直未变化", encoding="utf-8")
    agent, params = _runtime(tmp_path, attrs={"required_files": [str(product)]}, hint=2, halt=4)
    _attach_progress_task(agent, params, tmp_path)
    projection = tmp_path / "child" / "progress" / projection_name
    for index in range(1, 5):
        _record(agent, params, index, output=json.dumps({"product_path": str(projection)}))
    assert projection.is_file()
    assert product.read_text(encoding="utf-8") == "合同产物一直未变化"
    assert params.live_archive_state["subagent_readonly_stall"]["readonly_calls"] == 4
    assert params.repeated_failure_halt[1:] == ("code:SUBAGENT_READONLY_STALL", 4)


@pytest.mark.parametrize("key", ["latest_written_path", "closeout_written_path"])
@pytest.mark.parametrize("delivery", [False, True])
# LLM: 相对合同目标只锚定可信 cwd，两个候选字段都必须核验真实版本。
# 函数用途: 覆盖交付合同及任务文件合同的相对路径更新，不重放旧版本清零。
def test_relative_contract_version_uses_execution_cwd(tmp_path, monkeypatch, key, delivery):
    product = tmp_path / "stage.txt"
    product.write_text("旧版本", encoding="utf-8")
    attrs = {"workspace_root": str(tmp_path)}
    if not delivery:
        attrs["required_files"] = ["stage.txt"]
    agent, params = _runtime(tmp_path, attrs=attrs, hint=2, halt=4)
    agent.root = tmp_path / "different-root"
    if delivery:
        params = replace(params, delivery_contract={"artifacts": [{"preferred_path": "stage.txt"}]})
    monkeypatch.setattr(loop, "record_runtime_subagent_tool_progress", lambda *args: {
        "schema_version": "subagent_tool_progress.v1", key: "stage.txt",
    })
    _reads(agent, params, 1, 4)
    assert params.live_archive_state["subagent_readonly_stall"]["readonly_calls"] == 3
    product.write_text("新版本有更多实际内容", encoding="utf-8")
    _record(agent, params, 4)
    assert params.live_archive_state["subagent_readonly_stall"]["readonly_calls"] == 0
    _reads(agent, params, 5, 9)
    assert params.repeated_failure_halt[1:] == ("code:SUBAGENT_READONLY_STALL", 4)


# LLM: 失败调用不提供新进展；之后成功读取相同版本也不能补算为更新。
# 函数用途: 确认失败投影既不重置也不留下可重放的版本变化。
def test_failed_projection_cannot_replay_version_update(tmp_path, monkeypatch):
    product = tmp_path / "stage.txt"
    agent, params = _runtime(tmp_path, attrs={"required_files": [str(product)]}, hint=2, halt=4)
    monkeypatch.setattr(loop, "record_runtime_subagent_tool_progress", lambda *args: {
        "schema_version": "subagent_tool_progress.v1", "latest_written_path": str(product),
    })
    _record(agent, params, 1)
    product.write_text("真实新建，但失败投影不是成功进展", encoding="utf-8")
    _record(agent, params, 2, ok=False)
    _reads(agent, params, 3, 5)
    assert params.repeated_failure_halt[1:] == ("code:SUBAGENT_READONLY_STALL", 4)


# LLM: 文件合同再长也不能让只读热路径触发无界 stat 或隐式目录遍历。
# 函数用途: 在正式记录入口核对初始观察及后续候选的容量上限。
def test_file_version_observations_are_bounded(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import readonly_stall_guard as guard

    observed = []
    monkeypatch.setattr(guard, "_file_version", lambda path: observed.append(path) or None)
    attrs = {"required_files": [str(tmp_path / f"product-{index}.txt") for index in range(200)]}
    agent, params = _runtime(tmp_path, attrs=attrs, hint=2, halt=4)
    _record(agent, params, 1)
    assert len(observed) == 128
    monkeypatch.setattr(loop, "record_runtime_subagent_tool_progress", lambda *args: {
        "schema_version": "subagent_tool_progress.v1", "latest_written_path": str(tmp_path / "extra.txt"),
    })
    _reads(agent, params, 2, 5)
    assert len(observed) == 128
    assert len(params.live_archive_state["subagent_readonly_stall"]["path_versions"]) == 128
    assert params.repeated_failure_halt[1:] == ("code:SUBAGENT_READONLY_STALL", 4)


def test_delivery_artifact_contract_enables_guard_without_text_inference(tmp_path):
    agent, params = _runtime(tmp_path, attrs={}, hint=1, halt=2)
    params = replace(params, delivery_contract={"artifacts": [{"path": "result.txt"}]})
    _reads(agent, params, 1, 3)
    assert params.repeated_failure_halt[1:] == ("code:SUBAGENT_READONLY_STALL", 2)


def test_new_capability_keys_are_registered_documented_and_keyword_only():
    from agent_py_agent.agent.capability.config import CapabilityConfig, load_capability_config
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry

    config_path = Path(__file__).parents[1] / "config" / "capability_config.yaml"
    config = load_capability_config(config_path)
    registry = parameter_registry()
    for key, default in (("subagent_readonly_stall_hint_calls", 30), ("subagent_readonly_stall_halt_calls", 80)):
        assert getattr(config, key) == default
        assert getattr(CapabilityConfig(), key) == default
        assert CapabilityConfig.__dataclass_fields__[key].kw_only
        assert "0" in registry[key].description and "关闭" in registry[key].description


@pytest.mark.parametrize("value", [-1, True, "30"])
def test_invalid_stall_config_is_rejected_not_coerced_open(tmp_path, value):
    from agent_py_agent.agent.capability.config import load_capability_config

    config = tmp_path / "invalid.yaml"
    config.write_text(f"subagent_readonly_stall_hint_calls: {json.dumps(value)}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="非负整数"):
        load_capability_config(config)


def test_later_write_attempt_clears_only_this_stall(tmp_path):
    agent, params = _runtime(tmp_path, hint=0, halt=1)
    _record(agent, params, 1)
    _record(agent, params, 2, tool="write_file", effect="mutating", ok=False)
    assert params.repeated_failure_halt is None
    assert not params.repeated_failure_halt_exhausted
    other_halt = ("other", "code:OTHER_HALT", 7)
    params = replace(params, repeated_failure_halt=other_halt, repeated_failure_halt_exhausted=True)
    _record(agent, params, 3, tool="write_file", effect="mutating", ok=False)
    assert params.repeated_failure_halt == other_halt
