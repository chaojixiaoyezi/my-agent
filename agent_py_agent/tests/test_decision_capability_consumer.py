"""能力推荐及同片纯值沿原运行种子渲染；复用不重发决策，不修改授权或序列化活对象。"""
import json
from dataclasses import FrozenInstanceError, asdict, fields, replace
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


# LLM: 只替换原生provider调用，逐题答案仍过正式wire校验；保留原设置、期限、worker和输入用量事实链。
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


@pytest.mark.parametrize("mutation", ["skills", "handler", "policy", "model", "window", "attrs", "generation_endpoint", "generation_headers"])
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
        elif mutation == "generation_endpoint":
            surface.host.backend.api_base = "https://changed.example.test/generation"
        elif mutation == "generation_headers":
            surface.host.backend.custom_headers = {"X-Route": "changed-test-secret"}
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
    def execute(agent, params):
        captured.append((params, str(_render_tool_loop_prompt(agent, params))))
        assert str(_render_tool_loop_prompt(agent, params)) == captured[0][1]
        raise FinishedProbe
    monkeypatch.setattr(loop_support, "execute_tool_loop", execute)
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


def test_applied_carrier_is_immutable_data_without_runtime_or_response(surface, monkeypatch):
    provider(monkeypatch)
    result = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    selection = result.selection
    assert isinstance(selection, module.CapabilityPresentationSelection)
    assert selection.selected_skill_ids == ("workspace:method-001",)
    assert selection.presentation_deferred_names == frozenset({surface.second.model_spec.name})
    assert "only-private-secret" not in repr(selection)
    assert all(type(value) in {str, tuple, frozenset, type(None)} for key, value in vars(selection).items() if key != "binding")
    assert all(type(value) in {str, tuple} for value in vars(selection.binding).values())
    assert not {"handler", "tool_snapshot", "response", "deadline"}.intersection(vars(selection))
    with pytest.raises(FrozenInstanceError):
        selection.selected_skill_ids = ()
    with pytest.raises(ValueError, match="不可变"):
        replace(selection, selected_skill_ids=[])
    with pytest.raises(ValueError, match="不可变"):
        replace(selection, presentation_shortlist_names={"list_tools"})


@pytest.mark.parametrize("choice", [None, "not_needed"])
def test_same_slice_carrier_preserves_exact_projection_without_another_decision(surface, monkeypatch, choice):
    calls = provider(monkeypatch, choice=choice)
    original = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    surface.params.capability_presentation = original.selection
    # 代际是历史边界；它本身不能令原能力/必要引用和宿主身份失效。
    surface.params.task_attributes["conversation_compact_generation"] = 3
    restored = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert restored.finding == "skill_tool_decision:apply:carried"
    assert restored.selection is original.selection
    assert restored.selected_skill_ids == (() if choice else ("workspace:method-001",))
    assert model_input(surface, restored)[:2] == model_input(surface, original)[:2]
    assert restored.tool_snapshot.runtimes is surface.snapshot.runtimes
    assert restored.tool_snapshot.allowed_tools is surface.snapshot.allowed_tools
    assert restored.tool_snapshot.snapshot_hash == surface.snapshot.snapshot_hash
    assert len(calls) == 1
    assert surface.first.calls == surface.second.calls == 0


@pytest.mark.parametrize("mutation", [
    "skills", "handler", "policy", "observe", "off", "model", "window", "required", "allowlist",
    "attempt", "request", "run", "task", "thread", "input", "connection",
])
def test_stale_carrier_keeps_original_without_network_retry(surface, monkeypatch, mutation):
    from agent_py_agent.agent.settings.model_profiles import (
        execute_model_profile_operation,
        model_profiles_path,
        read_model_profiles,
    )

    calls = provider(monkeypatch)
    original = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    surface.params.capability_presentation = original.selection
    if mutation == "skills":
        surface.host.skill_snapshot_for_run_scope = lambda _: replace(surface.catalog.snapshot, entries=(), fingerprint="changed")
    elif mutation == "handler":
        surface.second.ready = False
    elif mutation == "policy":
        patch(surface.host, {"points.skill_tool.context_policy": "metadata"})
    elif mutation in {"off", "observe"}:
        patch(surface.host, {"points.skill_tool.mode": mutation})
    elif mutation == "model":
        surface.host.backend = SimpleNamespace(model_name="changed")
    elif mutation == "window":
        surface.host.config.model_context_window_tokens = 5000
    elif mutation == "required":
        surface.contract.required_actions = (SimpleNamespace(status="open", allowed_tools=(surface.second.model_spec.name,)),)
    elif mutation == "allowlist":
        surface.params.allowed_tools = [surface.first.model_spec.name]
    elif mutation == "thread":
        surface.params.task_attributes["agent_thread_id"] = "other-thread"
    elif mutation == "input":
        surface.params.user_prompt = "另一个工作片的需求"
    elif mutation == "connection":
        data = read_model_profiles(model_profiles_path(surface.host.home_paths))
        profile = next(row for row in data["profiles"].values() if row.get("capability") == "decision")
        execute_model_profile_operation(surface.host, "save_provider", {
            "provider_id": profile["provider_id"], "editing": True, "provider": {"api_key": "new-test-secret"},
        })
    else:
        setattr(surface.params, mutation + "_id", "changed")
    result = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert result.selection is None and result.selected_skill_ids is None
    assert result.tool_snapshot is surface.snapshot
    assert len(calls) == 1


def test_carrier_keeps_real_loaded_schema_and_rejects_out_of_scope_names(surface, monkeypatch):
    calls = provider(monkeypatch)
    original = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    surface.params.capability_presentation = original.selection
    restored = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert surface.second.model_spec.name in native_schema(surface.host.tools, restored.tool_snapshot,
                                                        loaded_tool_names={surface.second.model_spec.name})
    surface.params.capability_presentation = replace(original.selection, presentation_deferred_names=frozenset({"outside-scope"}))
    rejected = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert rejected.tool_snapshot is surface.snapshot and rejected.selection is None
    assert len(calls) == 1


def test_carried_projection_propagates_user_stop(surface, monkeypatch):
    calls = provider(monkeypatch)
    surface.params.capability_presentation = module.recommend_capabilities(
        surface.host, surface.params, surface.snapshot, surface.contract,
    ).selection
    monkeypatch.setattr(module, "_tools_current", lambda _: (_ for _ in ()).throw(InterruptedError("stop")))
    with pytest.raises(InterruptedError):
        module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert len(calls) == 1


def test_evaluated_without_selection_never_reopens_decision_stage(surface, monkeypatch):
    calls = provider(monkeypatch)
    surface.params.capability_presentation_evaluated = True
    def forbidden(*_args, **_kwargs):
        raise AssertionError("同片已评估后不能重新打开决策阶段")
    monkeypatch.setattr(module, "begin_decision_stage", forbidden)
    result = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert result.tool_snapshot is surface.snapshot and result.selection is None and not calls
    monkeypatch.setattr(module, "_check_cancelled", lambda: (_ for _ in ()).throw(InterruptedError("stop")))
    with pytest.raises(InterruptedError):
        module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)


def test_host_turn_survives_db_attempt_rotation_but_not_host_turn_change(surface, monkeypatch):
    calls = provider(monkeypatch)
    surface.params.capability_presentation_turn_id = "gateway-execution-1"
    surface.params.attempt_id = "database-attempt-1"
    selected = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert selected.selection.attempt_id == "gateway-execution-1"
    surface.params.capability_presentation = selected.selection
    surface.params.capability_presentation_evaluated = True
    surface.params.attempt_id = "database-attempt-2"
    same_turn = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert same_turn.selection is selected.selection
    surface.params.capability_presentation_turn_id = "gateway-execution-2"
    rejected = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert rejected.selection is None and rejected.tool_snapshot is surface.snapshot and len(calls) == 1


def test_new_selection_still_checks_original_deadline_after_freezing_carrier(surface, monkeypatch):
    calls = provider(monkeypatch)
    original = module._presentation_revision
    revisions = []
    def delayed(*args):
        value = original(*args)
        revisions.append(value)
        if len(revisions) == 2:
            monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: float("inf")))
        return value
    monkeypatch.setattr(module, "_presentation_revision", delayed)
    result = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert result.tool_snapshot is surface.snapshot and result.selection is None
    assert result.finding.endswith("stale") and len(calls) == 1


@pytest.mark.parametrize("source,field", [
    ("backend", "api_base"), ("backend", "custom_headers"), ("backend", "api_key"), ("backend", "auth_ref"),
    ("config", "api_base"), ("config", "model_custom_headers"), ("config", "model_auth_ref"), ("config", "config_sources"),
])
def test_same_generation_model_connection_change_invalidates_carrier_without_exposing_secrets(surface, monkeypatch, source, field):
    backend, config = surface.host.backend, surface.host.config
    backend.api_base = config.api_base = "https://original.example.test/generation"
    backend.custom_headers = {"X-Route": "original-test-route-secret"}
    config.model_custom_headers = dict(backend.custom_headers)
    backend.api_key = config.api_key = "original-test-api-secret"
    backend.auth_ref = {"mode": "login", "profile_id": "original-test-auth-ref"}
    config.model_auth_ref = dict(backend.auth_ref)
    config.config_sources = {"model_name": {"profile_id": "original-profile"}}
    calls = provider(monkeypatch)
    initial = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert initial.selection is not None
    before = module._generation_connection_revision(surface.host)
    surface.params.capability_presentation = initial.selection
    target = getattr(surface.host, source)
    if field.endswith("headers"):
        getattr(target, field)["X-Route"] = "changed-test-route-secret"
    elif field.endswith("auth_ref"):
        getattr(target, field)["profile_id"] = "changed-test-auth-ref"
    elif field == "config_sources":
        config.config_sources["model_name"]["profile_id"] = "changed-profile"
    else:
        setattr(target, field, "https://changed.example.test/generation" if field == "api_base" else "changed-test-api-secret")
    after = module._generation_connection_revision(surface.host)
    restored = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert before != after and len(before) == len(after) == 64
    assert backend.model_name == "original-main"
    assert restored.tool_snapshot is surface.snapshot and restored.selection is None
    assert len(calls) == 1
    public = repr(initial.selection) + str(asdict(initial.selection)) + before + after
    for private in ("original-test-route-secret", "original-test-api-secret", "original-test-auth-ref", "original.example.test"):
        assert private not in public


def test_already_applied_selection_is_not_a_pending_response_with_an_old_deadline(surface, monkeypatch):
    calls = provider(monkeypatch)
    initial = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    surface.params.capability_presentation = initial.selection
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: float("inf")))
    restored = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract)
    assert restored.selection is initial.selection and restored.finding.endswith("carried")
    assert len(calls) == 1


# LLM: 仅替换最终模型工具循环；能力决策/快照/参数/真实PromptBuilder仍走产品路径，捕获输出不触碰生产文件。
# 函数用途: 让原运行接缝完整返回结果，以核对callback与序列化结果之间的边界。
def capture_runtime_loop(monkeypatch, captured):
    from agent_py_agent.agent.agent_core.runtime import loop_support
    from agent_py_agent.agent.backends.base import ModelResponse

    def execute(agent, params):
        from agent_py_agent.agent.agent_core._tool_loop_service import ToolLoopRunResult

        actual = replace(params, workspace_context_snapshot="固定工作区")
        prompt = _render_tool_loop_prompt(agent, actual)
        captured.append((actual, str(prompt)))
        return ToolLoopRunResult(prompt, ModelResponse(text="完成", backend="fixture"), 0, params)
    monkeypatch.setattr(loop_support, "execute_tool_loop", execute)
    return loop_support


def test_runtime_callback_receives_only_adopted_pure_value_and_clears_stale(surface, monkeypatch):
    from agent_py_agent.agent.agent_core.models import AgentRunResult
    from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeLoopResult

    calls = provider(monkeypatch)
    observed, captured = [], []
    loop_support = capture_runtime_loop(monkeypatch, captured)
    surface.params.capability_presentation_callback = observed.append
    result = loop_support._execute_runtime_loop(surface.host, surface.params)
    assert len(observed) == 1 and isinstance(observed[0], module.CapabilityPresentationSelection)
    surface.params.capability_presentation = observed[0]
    loop_support._execute_runtime_loop(surface.host, surface.params)
    assert observed[1] is observed[0] and captured[0][1] == captured[1][1]
    patch(surface.host, {"enabled": False})
    loop_support._execute_runtime_loop(surface.host, surface.params)
    assert observed[2] is None and captured[2][0].selected_skill_ids is None
    assert len(calls) == 1
    for result_type in (RuntimeLoopResult, AgentRunResult):
        assert not {"capability_presentation", "capability_presentation_callback"}.intersection(field.name for field in fields(result_type))
    assert "CapabilityPresentationSelection" not in str(asdict(result))


def test_failed_presentation_callback_stops_before_model_loop(surface, monkeypatch):
    provider(monkeypatch)
    captured = []
    loop_support = capture_runtime_loop(monkeypatch, captured)
    def broken(_value):
        raise RuntimeError("host callback failed")
    surface.params.capability_presentation_callback = broken
    with pytest.raises(RuntimeError, match="host callback failed"):
        loop_support._execute_runtime_loop(surface.host, surface.params)
    assert captured == []


def test_run_params_pass_carrier_and_callback_without_new_slice_inheritance(surface, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime.loop_models import (
        PreparedRuntimeContext,
        RunParams,
    )
    from agent_py_agent.agent.agent_core.runtime.loop_support import _runtime_loop_params

    provider(monkeypatch)
    selection = module.recommend_capabilities(surface.host, surface.params, surface.snapshot, surface.contract).selection
    observed = []
    host_params = RunParams(capability_presentation=selection, capability_presentation_callback=observed.append,
                           capability_presentation_evaluated=True, capability_presentation_turn_id="host-turn")
    prepared = PreparedRuntimeContext([], [], None, None, "", tool_runtime_snapshot=surface.snapshot,
                                      tool_protocol_snapshot=surface.params.tool_protocol_snapshot)
    actual = _runtime_loop_params("核对来源", prepared, host_params)
    assert actual.capability_presentation is selection
    assert actual.capability_presentation_callback is host_params.capability_presentation_callback
    assert actual.capability_presentation_evaluated is True
    assert actual.capability_presentation_turn_id == "host-turn"
    assert RunParams().capability_presentation is None
    assert RunParams().capability_presentation_callback is None
    assert RunParams().capability_presentation_evaluated is False
    assert RunParams().capability_presentation_turn_id == ""
