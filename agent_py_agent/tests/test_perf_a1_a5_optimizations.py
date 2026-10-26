# LLM: 用临时索引和实际产品入口验证性能调用次数与安全边界，不调用真实模型或修改用户配置。
# 模块用途: 回归A1–A5优化的查找、回执、计时、内容失效和窗口边界。
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agent_py_agent.agent.memory_archive.artifact import reader
from agent_py_agent.tests.test_memory_artifact_read import _write_externalized_tool_output
from agent_py_agent.tests.test_tool_ref_scope_stream import write_index


# LLM: 纯逻辑ID不能为不相干的历史条目解析文件路径；命中记录最终读取仍须安全打开。
# 函数用途: 验证精确字段匹配在任何文件系统解析之前执行。
@pytest.mark.parametrize("ref,expected", [("run:hit", True), ("missing", False), ("art_hit", True)])
def test_a1_literal_match_never_resolves_unrelated_paths(ref, expected):
    row = {"call_id": "hit", "scoped_call_id": "run:hit", "artifact_id": "art_hit", "path": "/unused"}
    with patch.object(reader, "_resolve_index_record_path", side_effect=AssertionError("unexpected resolve")):
        assert reader._record_matches_ref(row, ref, None) is expected


# LLM: 统计真实JSON解码而非伪造命中；返回值污染不能改变缓存，索引追加后必须重新解析。
# 函数用途: 验证索引缓存、内容副本及mtime/size失效。
def test_a1_warm_lookup_reuses_parsing_and_detaches_rows(tmp_path):
    path = _write_externalized_tool_output(tmp_path, content="cache-body")
    request = reader.ReadToolOutputArtifactRequest(tmp_path, "run-artifact:call-artifact")
    first = reader._find_index_record(tmp_path, request.artifact_ref, request)
    assert first and first["path"] == str(path)
    first["parameters"] = {"nested": ["poison"]}
    first["path"] = "poison"
    with patch.object(reader, "iter_decoded_tool_index_objects", wraps=reader.iter_decoded_tool_index_objects) as decoded:
        second = reader._find_index_record(tmp_path, request.artifact_ref, request)
    assert second["path"] == str(path)
    assert "parameters" not in second or second["parameters"] != first["parameters"]
    assert decoded.call_count == 0
    index = path.parent / "index.jsonl"
    with index.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({**second, "call_id": "new", "scoped_call_id": "run-artifact:new"}) + "\n")
    assert reader._find_index_record(tmp_path, "run-artifact:new", request)["call_id"] == "new"


# LLM: 仅完整scoped身份主根命中可提前结束；别名/作用域与损坏索引另由既有合同覆盖。
# 函数用途: 验证当前主根精确命中不展开历史目录。
def test_a1_primary_scoped_hit_does_not_discover_historical_roots(tmp_path):
    _write_externalized_tool_output(tmp_path, content="primary-body" * 100)
    request = reader.ReadToolOutputArtifactRequest(tmp_path, "run-artifact:call-artifact")
    with patch.object(reader, "tool_output_index_paths_for_lookup", side_effect=AssertionError("historical discovery")):
        plan = reader.prepare_tool_output_artifact_read(request)
    assert plan.record["call_id"] == "call-artifact"


# LLM: 无关路径坏形状不能拖慢逻辑查询；当前命中路径损坏仍必须报错。
# 函数用途: 验证大量无关记录只解析真实命中路径。
def test_a1_logical_lookup_resolves_only_matching_row(tmp_path):
    rows = [{"call_id": f"noise-{i}", "path": f"/unused/{i}"} for i in range(1000)]
    rows.append({"call_id": "wanted", "path": ""})
    write_index(tmp_path, b"\n".join(json.dumps(row).encode() for row in rows))
    request = reader.ReadToolOutputArtifactRequest(tmp_path, "wanted")
    with patch.object(reader, "_resolve_index_record_path", wraps=reader._resolve_index_record_path) as resolve:
        assert reader._find_index_record(tmp_path, "wanted", request)["call_id"] == "wanted"
    assert resolve.call_count <= 1


# LLM: 使用临时真实参数中心文件；检查回执自然顺序和保存后的结构化事实，不修改本机设置。
# 函数用途: 验证配置写后无需再次view便能核对，并能在有界预览看到关键字段。
def test_a2_write_receipt_has_summary_and_readback_fact(tmp_path):
    from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
    from agent_py_agent.tests.test_parameter_changes import _main_agent, _user

    agent = _main_agent(_user(tmp_path))
    agent.config.model_context_window_tokens = 131072
    tool = UserConfigTool(agent)
    saved = tool.execute({"action": "set", "key": "max_tokens", "value": "32768"})
    assert saved.ok, saved.output
    report = json.loads(saved.output)
    assert report["action"] == "set" and report["changed_keys"] == ["max_tokens"]
    assert report["applied_values"]["max_tokens"] == 32768
    assert report["fact"]["effective"] == "32768"
    assert all(f'"{key}"' in saved.output[:300] for key in ("ok", "action", "changed_keys", "applied_values"))


# LLM: 单键读保留出处及可写性，大目录仅无key时返回；搜索brief不能重复长派生说明。
# 函数用途: 验证view/search不再让无关capability或applied_rule挤出结果。
def test_a2_single_key_view_and_search_are_compact(tmp_path):
    from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
    from agent_py_agent.tests.test_parameter_changes import _main_agent, _user

    tool = UserConfigTool(_main_agent(_user(tmp_path)))
    view = json.loads(tool.execute({"action": "view", "key": "max_tokens"}).output)
    assert "fact" in view and "parameter" in view and "capability" not in view
    assert "capability" in json.loads(tool.execute({"action": "view"}).output)
    search = json.loads(tool.execute({"action": "search", "query": "max_tokens"}).output)
    assert search["parameters"] and "applied_rule" not in search["parameters"][0]


# LLM: 默认只回显修改键，全量配置必须显式请求；两个flag只对主owner展示。
# 函数用途: 验证紧凑config回执与可选完整脱敏配置入口。
def test_a2_full_config_is_explicit_and_default_is_scoped(tmp_path):
    from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
    from agent_py_agent.tests.test_parameter_changes import _main_agent, _user

    tool = UserConfigTool(_main_agent(_user(tmp_path)))
    params = {"action": "set", "key": "request_timeout", "value": "200"}
    compact = json.loads(tool.execute(params).output)
    assert set(compact["config"]) == {"request_timeout"}
    full = json.loads(tool.execute({**params, "include_full_config": True}).output)
    assert len(full["config"]) > 10 and full["config"]["request_timeout"] == "200"
    for flag in ("include_full_config", "include_constants"):
        assert flag in tool.model_spec.input_schema["properties"]
        assert flag not in UserConfigTool().model_spec.input_schema["properties"]


# LLM: 只替换预算观察点；输出真实估算与登记仍走原函数，区分软线和硬容量。
# 函数用途: 验证过软线但远离硬窗口的小结果不强制外置。
@pytest.mark.parametrize("current,expected", [(140_000, False), (199_999, True)])
def test_a5_soft_trigger_is_not_hard_headroom(current, expected):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core import tool_call_archive_record as archive
    from agent_py_agent.agent.agent_core.model import context_pressure
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleNamespace(config=AgentConfig(tool_output_externalize_min_chars=32000, tool_output_preview_chars=1200))
    params = SimpleNamespace(live_archive_state={})
    budget = SimpleNamespace(context_window_tokens=200_000, compact_trigger_tokens=136_800,
                             current_tokens=current, remaining_to_compact_tokens=0)
    with patch.object(context_pressure, "model_visible_context_budget", return_value=budget):
        assert archive._headroom_forces_externalize(agent, params, "small result " * 500) is expected


# LLM: 同一批结果尚未提交IR时也必须累计，不能把每条都当独占余量。
# 函数用途: 验证两条各自可放下的结果合计触及硬窗口时第二条外置。
def test_a5_inline_reservations_accumulate_before_ir_commit():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core import tool_call_archive_record as archive
    from agent_py_agent.agent.agent_core.model import context_pressure
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleNamespace(config=AgentConfig(tool_output_externalize_min_chars=32000, tool_output_preview_chars=1200))
    params = SimpleNamespace(live_archive_state={})
    budget = SimpleNamespace(context_window_tokens=200_000, compact_trigger_tokens=136_800,
                             current_tokens=196_000, remaining_to_compact_tokens=0)
    with patch.object(context_pressure, "model_visible_context_budget", return_value=budget):
        assert not archive._headroom_forces_externalize(agent, params, "small result " * 500)
        assert archive._headroom_forces_externalize(agent, params, "small result " * 500)


# LLM: 注入相互不兼容的账本时钟与单调时钟，秒表不得把前者当成后者。
# 函数用途: 验证请求周期重建prompt使用time.monotonic。
def test_a3_rebuild_timer_uses_monotonic_clock(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core import _tool_loop_service as service
    from agent_py_agent.agent.agent_core.model import call_runtime

    captured = []
    agent = SimpleNamespace(_model_call_ledger=SimpleNamespace(context=SimpleNamespace(now=lambda: 9999.0)))
    monkeypatch.setattr(service, "build_tool_loop_prompt", lambda *_args: "prompt")
    monkeypatch.setattr(service, "generate_model_response", lambda request: captured.append(request) or "response")
    # LLM: 只替代模型请求周期，仍执行传入的真实builder及generation参数构造。
    # 函数用途: 在本进程同步捕获真实请求时间字段，不调用外部模型。
    def request(**callbacks):
        prompt = callbacks["build_prompt"]()
        return prompt, callbacks["generate_response"](prompt)
    monkeypatch.setattr(service, "request_model_response", request)
    monkeypatch.setattr(call_runtime, "model_call_ledger", lambda _agent: agent._model_call_ledger)
    with patch("time.monotonic", return_value=12.0):
        service._request_tool_loop_model_response(agent, SimpleNamespace(), 1)
    assert captured[0].context_assembly_started_at == 12.0


# LLM: 包装原始投影统计真实计算；采用相同typed布局，且在中段原位改写后必须失效。
# 函数用途: 验证预检、物化和最终计量共用一次prompt/IR投影。
def test_a4_projection_reused_across_materialization_and_content_changes():
    from agent_py_agent.agent.agent_core import tool_ir_history, tool_model_generation
    from agent_py_agent.agent.agent_core.model import context_pressure
    from agent_py_agent.agent.backends.tool_ir import AssistantTurn, ToolCall
    from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
    from agent_py_agent.tests.test_model_latency_q2 import _context_case

    agent, params = _context_case()
    params.tool_ir_history = [AssistantTurn(text="before"), AssistantTurn(text="middle"), AssistantTurn(text="after")]
    prompt = CacheStructuredPrompt("stable", volatile_sections=(("runtime.test", "dynamic facts"),))
    original = tool_ir_history.project_native_prompt_history
    with patch.object(tool_ir_history, "project_native_prompt_history", wraps=original) as projected:
        context_pressure.model_visible_context_snapshot(agent, params, prompt)
        request = tool_model_generation._materialize_native_prompt_facts(
            tool_model_generation.ModelGenerateParams(agent, params, prompt, 1))
        context_pressure.model_visible_context_snapshot(agent, params, request.prompt)
        assert projected.call_count == 1
        params.tool_ir_history[1].tool_calls.append(ToolCall(
            "mid", "read", {"path": "changed in place"}, "native", "sha256:test", "r", "t", "a"))
        context_pressure.model_visible_context_snapshot(agent, params, request.prompt)
        assert projected.call_count == 2


# LLM: 使用真实manager，只有下层列表被探针替换；空目录应不进入其canonical查询，出现子目录必须恢复查询。
# 函数用途: 验证零子代理快路径不会永久缓存空状态。
def test_a4_empty_subagent_workspace_skips_reports_but_not_new_children(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.runtime import guidance
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    manager = SubAgentManager(tmp_path / "children")
    agent = SimpleNamespace(subagents=manager)
    params = SimpleNamespace(task_id="root", task_attributes={}, runtime_injections=[])
    with patch.object(manager.persistence, "list_runs_for_root_report", side_effect=RuntimeError("canonical probe")) as report:
        assert guidance._render_runtime_direct_children(agent, params) == ""
        assert report.call_count == 0
        (manager.workspace / "new-child").mkdir()
        with pytest.raises(RuntimeError, match="canonical probe"):
            guidance._render_runtime_direct_children(agent, params)
        assert report.call_count == 1


# LLM: 确定性单调时钟模拟canonical状态读取与纯渲染；不把前置5秒算进2秒组装。
# 函数用途: 验证实际prompt入口分别计量housekeeping、直属状态刷新和纯组装。
def test_a3_prompt_stage_timings_separate_housekeeping(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent import model_request_selection
    from agent_py_agent.agent.agent_core import _tool_loop_service as service
    from agent_py_agent.agent.agent_core.model.context_estimate_cache import estimate_memo_for

    clock = [10.0]
    params = SimpleNamespace(consume_pending_turn_input=False)
    monkeypatch.setattr(service.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(service, "refresh_runtime_direct_children_snapshot", lambda *_: clock.__setitem__(0, clock[0] + 5))
    monkeypatch.setattr(service, "window_tool_context_params", lambda *_: None)
    monkeypatch.setattr(model_request_selection, "request_owns_compact", lambda *_: True)
    # LLM: 只模拟渲染计算时长，调用方计时和memo发表由真实产品函数执行。
    # 函数用途: 将纯组装时钟推进两秒并返回输入。
    def render(*_):
        clock[0] += 2
        return "prompt"
    monkeypatch.setattr(service, "_render_tool_loop_prompt", render)
    assert service.build_tool_loop_prompt(SimpleNamespace(), params) == "prompt"
    phase = estimate_memo_for(params).prompt_timings()
    assert phase["assembly_ms"] == 2000
    assert phase["pre_call_housekeeping_ms"] == phase["supervision_sweep_ms"] == 5000


# LLM: 真实执行参数是frozen，复用其已有ContextEstimateMemo；不能只在可加属性的SimpleNamespace上成立。
# 函数用途: 验证冻结参数下投影复用及同批内联余量累计仍生效。
def test_a4_a5_use_existing_memo_on_frozen_runtime_params():
    from dataclasses import MISSING, fields
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core import tool_call_archive_record as archive
    from agent_py_agent.agent.agent_core import tool_ir_history
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
    from agent_py_agent.agent.agent_core.model import context_pressure
    from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
    from agent_py_agent.tests.test_model_latency_q2 import _context_case

    agent, source = _context_case()
    required = {field.name: None for field in fields(ToolLoopExecuteParams)
                if field.default is MISSING and field.default_factory is MISSING}
    required.update({key: value for key, value in vars(source).items() if key in ToolLoopExecuteParams.__dataclass_fields__})
    params = ToolLoopExecuteParams(**required)
    prompt = CacheStructuredPrompt("stable", "facts")
    with patch.object(tool_ir_history, "project_native_prompt_history", wraps=tool_ir_history.project_native_prompt_history) as project:
        context_pressure.model_visible_context_snapshot(agent, params, prompt)
        context_pressure.model_visible_context_snapshot(agent, params, prompt)
    assert project.call_count == 1
    budget = SimpleNamespace(context_window_tokens=200_000, compact_trigger_tokens=136_800,
                             current_tokens=196_000, remaining_to_compact_tokens=0)
    with patch.object(context_pressure, "model_visible_context_budget", return_value=budget):
        assert not archive._headroom_forces_externalize(agent, params, "small result " * 500)
        assert archive._headroom_forces_externalize(agent, params, "small result " * 500)


# LLM: 在真实投影即将读取时追加原IR，再回退原IR；输入键不得关联到另一时刻的输出。
# 函数用途: 复现独立审阅发现的并发追加后旧输入命中幽灵记录。
def test_a4_projection_snapshot_does_not_resurrect_rolled_back_history():
    from agent_py_agent.agent.agent_core import tool_ir_history
    from agent_py_agent.agent.agent_core.model.context_pressure import (
        project_request_prompt_history,
    )
    from agent_py_agent.agent.backends.tool_ir import AssistantTurn
    from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
    from agent_py_agent.tests.test_model_latency_q2 import _context_case

    _, params = _context_case()
    params.tool_ir_history = [AssistantTurn(text="base")]
    base = list(params.tool_ir_history)
    prompt = CacheStructuredPrompt("stable", "facts")
    original = tool_ir_history.project_native_prompt_history
    injected = []
    # LLM: 精确模拟键生成后的并发变更，真实投影仍执行，只变更原参数、不改传入快照。
    # 函数用途: 在第一次投影前追加一条随后被回滚的记录。
    def project(snapshot, value, **kwargs):
        if not injected:
            params.tool_ir_history.append(AssistantTurn(text="ghost"))
            injected.append(True)
        return original(snapshot, value, **kwargs)
    with patch.object(tool_ir_history, "project_native_prompt_history", side_effect=project):
        project_request_prompt_history(params, prompt)
        params.tool_ir_history[:] = base
        _, history = project_request_prompt_history(params, prompt)
    assert all(getattr(item, "text", "") != "ghost" for item in history)


# LLM: 硬容量改变不是批次结束，同批旧预留仍未进入IR。
# 函数用途: 复现独立审阅发现的预算变化丢失预留。
def test_a5_same_batch_budget_change_keeps_pending_reservations():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core import tool_call_archive_record as archive
    from agent_py_agent.agent.agent_core.model import context_pressure
    from agent_py_agent.agent.settings import AgentConfig

    text = "small result " * 500
    tokens = archive.estimate_tokens(text)
    agent = SimpleNamespace(config=AgentConfig(tool_output_externalize_min_chars=32000, tool_output_preview_chars=1200))
    params = SimpleNamespace(live_archive_state={"_current_model_turn_id": "batch-1"})
    budget = SimpleNamespace(context_window_tokens=3*tokens, compact_trigger_tokens=0,
                             current_tokens=tokens, remaining_to_compact_tokens=0)
    with patch.object(context_pressure, "model_visible_context_budget", return_value=budget):
        assert not archive._headroom_forces_externalize(agent, params, text)
        budget.context_window_tokens = 5*tokens//2
        assert archive._headroom_forces_externalize(agent, params, text)


# LLM: replace共用memo，但模型turn身份已变化；相同数值预算不代表旧结果仍未提交。
# 函数用途: 验证新批次不会继承旧批次的临时预留。
def test_a5_new_model_turn_resets_same_numeric_reservations():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core import tool_call_archive_record as archive
    from agent_py_agent.agent.agent_core.model import context_pressure
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleNamespace(config=AgentConfig(tool_output_externalize_min_chars=32000, tool_output_preview_chars=1200))
    params = SimpleNamespace(live_archive_state={"_current_model_turn_id": "batch-1"})
    budget = SimpleNamespace(context_window_tokens=200000, compact_trigger_tokens=136800,
                             current_tokens=196000, remaining_to_compact_tokens=0)
    with patch.object(context_pressure, "model_visible_context_budget", return_value=budget):
        assert not archive._headroom_forces_externalize(agent, params, "small result " * 500)
        params.live_archive_state["_current_model_turn_id"] = "batch-2"
        assert not archive._headroom_forces_externalize(agent, params, "small result " * 500)


# LLM: 硬余量与软Compact余量保持独立字段，防止诊断混淆。
# 函数用途: 验证硬溢出回执保存实际硬余量而不是软线零值。
def test_a5_overflow_receipt_records_actual_hard_remaining():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core import tool_call_archive_record as archive
    from agent_py_agent.agent.agent_core.model import context_pressure
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleNamespace(config=AgentConfig(tool_output_externalize_min_chars=32000, tool_output_preview_chars=1200))
    params = SimpleNamespace(live_archive_state={})
    budget = SimpleNamespace(context_window_tokens=200000, compact_trigger_tokens=136800,
                             current_tokens=199999, remaining_to_compact_tokens=0)
    with patch.object(context_pressure, "model_visible_context_budget", return_value=budget):
        assert archive._headroom_forces_externalize(agent, params, "small result " * 500)
    overflow = params.live_archive_state["tool_context_window_overflow"]
    assert overflow.get("remaining_hard_headroom_tokens") == 1
    assert overflow["remaining_to_compact_tokens"] == 0


# LLM: typed结果已进入同一canonical IR才结算旧预留；同call重试只替换一次预留。
# 函数用途: 验证已提交结果不重复扣硬余量、边界重试不重复外置。
def test_a5_committed_ir_and_call_retry_settle_reservations():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core import tool_call_archive_record as archive
    from agent_py_agent.agent.agent_core.model import context_pressure
    from agent_py_agent.agent.backends.tool_ir import AssistantTurn
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.tooling.runtime_contracts import (
        ToolCall,
        ToolContentBlock,
        ToolResult,
    )

    text = "small result " * 500
    agent = SimpleNamespace(config=AgentConfig(tool_output_externalize_min_chars=32000, tool_output_preview_chars=1200))
    params = SimpleNamespace(live_archive_state={}, tool_ir_history=[])
    first = ToolCall("c1", "read", {}, "native", "sha256:test", "run", "model-turn", "attempt")
    second = ToolCall("c2", "read", {}, "native", "sha256:test", "run", "model-turn", "attempt")
    budget = SimpleNamespace(context_window_tokens=200000, compact_trigger_tokens=136800,
                             current_tokens=193500, remaining_to_compact_tokens=0)
    with patch.object(context_pressure, "model_visible_context_budget", return_value=budget):
        assert not archive._headroom_forces_externalize(agent, params, text, call=first)
        params.tool_ir_history.append(AssistantTurn(tool_calls=[first]))
        params.tool_ir_history.append(ToolResult("c1", "read", "succeeded", (ToolContentBlock("text", text=text),)))
        budget.current_tokens += archive.estimate_tokens(text)
        assert not archive._headroom_forces_externalize(agent, params, text, call=second)
        # 新真实turn在剩余4000时反复处理同一调用，不能将其未提交预留扣两次。
        first = ToolCall("retry", "read", {}, "native", "sha256:test", "run", "new-turn", "attempt")
        budget.current_tokens = 196000
        assert not archive._headroom_forces_externalize(agent, params, text, call=first)
        assert not archive._headroom_forces_externalize(agent, params, text, call=first)


# LLM: 旧turn可重用同call_id，typed结果也须按配对AssistantTurn原turn核对，不能只按裸ID结算。
# 函数用途: 验证旧轮结果不会结算新轮尚未写入IR的预留。
def test_a5_old_turn_same_call_id_does_not_settle_current_pending():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core import tool_call_archive_record as archive
    from agent_py_agent.agent.agent_core.model import context_pressure
    from agent_py_agent.agent.backends.tool_ir import AssistantTurn
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall, ToolResult

    old = ToolCall("c1", "read", {}, "native", "sha256:test", "run", "old-turn", "attempt")
    first = ToolCall("c1", "read", {}, "native", "sha256:test", "run", "new-turn", "attempt")
    second = ToolCall("c2", "read", {}, "native", "sha256:test", "run", "new-turn", "attempt")
    params = SimpleNamespace(live_archive_state={}, tool_ir_history=[AssistantTurn(tool_calls=[old]), ToolResult("c1", "read", "succeeded")])
    agent = SimpleNamespace(config=AgentConfig(tool_output_externalize_min_chars=32000, tool_output_preview_chars=1200))
    budget = SimpleNamespace(context_window_tokens=200000, compact_trigger_tokens=136800,
                             current_tokens=196000, remaining_to_compact_tokens=0)
    with patch.object(context_pressure, "model_visible_context_budget", return_value=budget):
        assert not archive._headroom_forces_externalize(agent, params, "small result " * 500, call=first)
        assert archive._headroom_forces_externalize(agent, params, "small result " * 500, call=second)
