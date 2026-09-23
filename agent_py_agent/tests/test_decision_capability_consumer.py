"""能力推荐经原设置/worker/账本，再沿原运行种子生成实际prompt和native schema。"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._tool_loop_service import _render_tool_loop_prompt
from agent_py_agent.agent.agent_core.runtime.loop_models import (
    RuntimeLoopParams,
    RuntimeToolLoopSeed,
)
from agent_py_agent.agent.agent_core.runtime.loop_support import _tool_loop_execute_params
from agent_py_agent.agent.backends.decision_protocol import DecisionBinding, DecisionRequest
from agent_py_agent.agent.backends.typesafe_decision import TypesafeDecisionBackend
from agent_py_agent.agent.backends.typesafe_decision_wire import (
    parse_typesafe_response,
    typesafe_payload,
)
from agent_py_agent.agent.capability import decision_recommendation as module
from agent_py_agent.agent.capability.decision_candidates import selection_questions
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_settings import host_at, patch
from agent_py_agent.tests.test_decision_skill_projection import setup_surface
from agent_py_agent.tests.test_tool_presentation_projection import native_schema
from agent_py_agent.tests.test_tool_presentation_projection import (
    prepared as tool_surface,  # noqa: F401
)


# LLM: 配置/目录/账本全部用原实现和临时文件；仅供应商回答由fixture替换，不调用收费服务。
# 函数用途: 将真实Skill快照和ToolRegistry接到原设置宿主，便于检验实际输入减量。
@pytest.fixture
def surface(tmp_path, tool_surface, skill_catalog_factory):  # noqa: F811
    registry, _, first, second, _legacy = tool_surface
    catalog, builder = setup_surface(tmp_path, skill_catalog_factory)
    host = host_at(tmp_path / "owner")
    profile, _ = decision(host)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    host.backend = SimpleNamespace(model_name="original-main")
    host.tools, host.prompts, host.capability_router = registry, builder, catalog.router
    host.root = tmp_path
    host.current_skill_snapshot = lambda: catalog.snapshot
    host.skill_snapshot_for_run_scope = lambda _root: catalog.snapshot
    registry.register(SkillSearchTool(host))
    snapshot = registry.runtime_snapshot(run_id="capability-run")
    params = RuntimeLoopParams("核对来源", "核对来源", [], [], None, "", request_id="capability-request",
        run_id="capability-run", task_id="capability-task", task_attributes={"agent_thread_id": thread.thread_id},
        tool_runtime_snapshot=snapshot, tool_protocol_snapshot=make_test_protocol_snapshot(run_id="capability-run"))
    patch(host, {"enabled": True, "profile_id": profile, "points.skill_tool.mode": "apply"})
    return SimpleNamespace(host=host, params=params, snapshot=snapshot, catalog=catalog, builder=builder,
                           first=first, second=second, contract=SimpleNamespace(required_actions=()))


# LLM: 只替换原生provider调用，逐题答案仍过正式wire校验；保留原设置、期限、worker和费用事实链。
# 函数用途: 按精确候选引用生成短名单，支持在请求过程中模拟配置/范围变化。
def provider(monkeypatch, *, choice=None, during=None, fail=None):
    calls = []
    def decide(backend, request, *, deadline):
        payload = typesafe_payload(request, backend.model_name)
        calls.append(request)
        if fail:
            raise fail
        desired = {"presentation_optional_a", "workspace:method-001"}
        answers = {}
        for key, question in payload["questions"].items():
            selected = choice or ("include" if question["instructions"]["candidate"]["ref"] in desired else "not_needed")
            answers[key] = {"type": "choice", "choice": selected, "confidence": 1.0,
                "probabilities": {candidate: float(candidate == selected) for candidate in question["criteria"]}}
        if during:
            during()
        return parse_typesafe_response(request, backend.model_name,
            {"model": "native-decision", "answers": answers, "usage": {"input_tokens": 41}})
    monkeypatch.setattr(TypesafeDecisionBackend, "decide", decide)
    return calls


# LLM: 原seed和params是真实类型；只固定易变工作区展示，不替换PromptBuilder或工具schema转换。
# 函数用途: 把消费者结果沿正式种子传递，获取模型实际可见的prompt与schema。
def model_input(surface, presentation):
    host, params, snapshot = surface.host, surface.params, presentation.tool_snapshot
    seed = RuntimeToolLoopSeed(params, [], host.tools.render_catalog_section(runtime_snapshot=snapshot),
        host.tools.render_recommended_tools_section(params.user_prompt, runtime_snapshot=snapshot),
        snapshot, params.tool_protocol_snapshot, surface.contract,
        presentation.selected_skill_ids, presentation.required_skill_ids)
    loop = _tool_loop_execute_params(host, seed)
    loop = replace(loop, workspace_context_snapshot="固定工作区")
    prompt = _render_tool_loop_prompt(host, loop)
    schema = native_schema(host.tools, loop.tool_runtime_snapshot, loaded_tool_names=loop.loaded_tool_names)
    return str(prompt), schema, loop


def test_apply_real_service_seed_reduces_prompt_and_native_schema_without_changing_authority(surface, monkeypatch):
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    from agent_py_agent.tests.test_tool_presentation_projection import search
    calls = provider(monkeypatch)
    baseline = model_input(surface, module.CapabilityPresentation(surface.snapshot))
    projected = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert projected.finding.endswith("applied"), projected.finding
    actual = model_input(surface, projected)
    assert len(actual[0].encode()) < len(baseline[0].encode())
    assert len(actual[1].encode()) < len(baseline[1].encode())
    assert "method-059" not in actual[0] and "workspace:method-001" in actual[0]
    assert surface.second.model_spec.name not in actual[1]
    assert projected.tool_snapshot.runtimes is surface.snapshot.runtimes
    assert projected.tool_snapshot.snapshot_hash == surface.snapshot.snapshot_hash
    _report, loaded = search(surface.host.tools, projected.tool_snapshot, surface.second.model_spec.name)
    assert surface.second.model_spec.name in native_schema(surface.host.tools, projected.tool_snapshot, loaded_tool_names=set(loaded))
    assert len(calls) == 1
    records = model_call_ledger(surface.host).records()
    assert len(records) == 1 and records[0].metadata["purpose"] == "decision"


@pytest.mark.parametrize("mode", ["off", "observe"])
def test_off_observe_preserve_actual_original_input(surface, monkeypatch, mode):
    patch(surface.host, {"points.skill_tool.mode": mode})
    calls = provider(monkeypatch)
    result = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert result.tool_snapshot is surface.snapshot and result.selected_skill_ids is None
    assert model_input(surface, result)[:2] == model_input(surface, module.CapabilityPresentation(surface.snapshot))[:2]
    assert len(calls) == (mode == "observe")


@pytest.mark.parametrize("choice", ["no_match", "abstain", "need_goal", "need_contract", "need_procedure", "need_environment"])
def test_non_selection_keeps_original_and_identifies_missing_data(surface, monkeypatch, choice):
    provider(monkeypatch, choice=choice)
    result = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert result.tool_snapshot is surface.snapshot and result.selected_skill_ids is None
    assert result.finding.endswith(choice)


def test_not_needed_can_reduce_optional_display_but_keeps_required_skills_and_tools(surface, monkeypatch):
    surface.params.task_attributes["skill_snapshot_refs"] = [{"stable_id": "workspace:method-003"}]
    surface.contract.required_actions = (SimpleNamespace(status="open", allowed_tools=(surface.second.model_spec.name,)),)
    provider(monkeypatch, choice="not_needed")
    result = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    prompt, schema, _ = model_input(surface, result)
    assert "workspace:method-003" in prompt and "method-059" not in prompt
    assert surface.second.model_spec.name in schema


@pytest.mark.parametrize("mutation", ["skills", "handler", "policy", "model", "window", "attrs"])
def test_changes_during_request_reject_old_suggestion(surface, monkeypatch, mutation):
    def change():
        if mutation == "skills":
            fresh = replace(surface.catalog.snapshot, entries=(), fingerprint="changed")
            surface.host.skill_snapshot_for_run_scope = lambda _: fresh
        elif mutation == "handler":
            surface.second.ready = False
        elif mutation == "policy":
            patch(surface.host, {"points.skill_tool.context_policy": "metadata"})
        elif mutation == "model":
            surface.host.backend = SimpleNamespace(model_name="changed")
        elif mutation == "window":
            surface.host.config.model_context_window_tokens = 5000
        else:
            surface.params.task_attributes["skill_snapshot_refs"] = [{"stable_id": "workspace:method-002"}]
    provider(monkeypatch, during=change)
    result = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert result.tool_snapshot is surface.snapshot and result.selected_skill_ids is None, result.finding


def test_metadata_policy_keeps_full_native_schema_but_reduces_skill_cards(surface, monkeypatch):
    patch(surface.host, {"points.skill_tool.context_policy": "metadata"})
    provider(monkeypatch)
    result = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert result.finding.endswith("applied")
    assert native_schema(surface.host.tools, result.tool_snapshot) == native_schema(surface.host.tools, surface.snapshot)
    assert result.selected_skill_ids == ("workspace:method-001",)


def test_native_error_does_not_change_original_input(surface, monkeypatch):
    provider(monkeypatch, fail=RuntimeError("fixture failure"))
    result = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert result.tool_snapshot is surface.snapshot and result.selected_skill_ids is None


def test_explicit_tool_allowlist_keeps_original_schemas(surface, monkeypatch):
    provider(monkeypatch)
    surface.params.allowed_tools = list(surface.snapshot.available_tool_names)
    result = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert result.tool_snapshot is surface.snapshot and result.finding.endswith("applied")


def test_scope_without_visible_skill_search_does_not_hide_skill_cards(surface, monkeypatch):
    provider(monkeypatch)
    surface.params.allowed_tools = [surface.first.model_spec.name, surface.second.model_spec.name]
    result = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert result.selected_skill_ids is None


def test_original_runtime_entry_calls_decision_once_and_passes_selection_to_renderer(surface, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime import loop_support
    calls = provider(monkeypatch)
    captured = []
    class FinishedProbe(Exception):
        pass
    def execute(service, params):
        captured.append((params, str(_render_tool_loop_prompt(service._agent, params))))
        assert str(_render_tool_loop_prompt(service._agent, params)) == captured[0][1]
        raise FinishedProbe
    monkeypatch.setattr(loop_support.ToolLoopService, "execute", execute)
    with pytest.raises(FinishedProbe):
        loop_support._execute_runtime_loop(surface.host, surface.params)
    assert len(calls) == 1 and len(captured) == 1
    assert captured[0][0].selected_skill_ids == ("workspace:method-001",)
    assert "method-059" not in captured[0][1]


def test_more_than_64_candidates_keep_all_candidates_with_native_valid_bounded_slots():
    rows = [{"kind": "tool", "ref": f"tool_{i}"} for i in range(96)]
    questions = selection_questions(rows)
    request = DecisionRequest(DecisionBinding("skill_tool", "owner", "operation", "policy", "candidate"), {"query": "从给定材料完成任务"}, questions)
    payload = typesafe_payload(request, "decision")
    assert len(payload["questions"]) == 96
    selected = {question["instructions"]["candidate"]["ref"] for question in questions.values()}
    assert selected == {row["ref"] for row in rows}
    assert len(json.dumps(payload).encode()) < 262144
