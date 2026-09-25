# LLM: 单元测试只替换决策服务边界与新鲜度权威 observation_is_current；观察信封沿 plugin_observation 真实归档形状，
#   另有一组集成测试用真实 plugin_observation 与插件线的事件夹具；_record_tool_call 为原实现，核对原结果/归档不变及 text/native 同段提示。
# 模块用途: 离线验证动作候选点的资格、脱敏输入、非选择回退、新鲜度与来源复核、取消传播、宿主展示接缝和设置入口。
from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service as loop
from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_context import decision_action_candidate as module
from agent_py_agent.agent.agent_core.tool_context.reducer import render_tool_result_for_live_prompt
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.backends.decision_protocol import (
    DecisionAnswer,
    DecisionBinding,
    DecisionInputError,
    DecisionResponse,
)
from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.plugin_observation import OBSERVATION_SCHEMA
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.decision_settings import (
    execute_decision_settings_operation as settings,
)
from agent_py_agent.agent.settings.decision_settings_schema import decision_field_scopes
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult
from agent_py_agent.cli.chat_parts import tui_decision_menu
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
    make_test_model_spec,
    make_test_protocol_snapshot,
    runtime_snapshot_for_model_specs,
)
from agent_py_agent.tests.test_decision_external_material_order import install as install_reading
from agent_py_agent.tests.test_decision_external_material_order import isolate_recording
from agent_py_agent.tests.test_decision_settings import host_at, patch
from agent_py_agent.tests.test_tui_decision_menu import (
    Gateway,
    choose,
    open_scope,
    press,
    running,
    visible,
)

_Q = module._QUESTION
_READ = "plugin__browser_lite_0a1b2c3d__read_4e5f6a7b"
_CLICK = "plugin__browser_lite_0a1b2c3d__click_8c9d0e1f"
_FILL = "plugin__browser_lite_0a1b2c3d__fill_2a3b4c5d"
_OBS = "obs-" + "a" * 24
_C1, _C2, _C3 = ("cand-" + digit * 16 for digit in "123")
_HINT_C1 = (f"[action-candidate]\n可选操作建议：下一步可先核对候选 {_C1}（button）；"
            "是否操作、如何操作仍由你按原工具与审批决定。")


# LLM: 字段与 plugin_observation 写进归档的 `tool_result_envelope.observation` 一致；key/目标引用/代次/动作名是本地事实，不应外发。
# 函数用途: 构造一份含三个候选的宿主观察记录，可覆盖任意顶层字段。
def observation(**changes):
    value = {"schema": OBSERVATION_SCHEMA, "observation_id": _OBS, "plugin_id": "browser-lite",
             "activation_id": "activation-secret-7", "tool": _READ, "target_kind": "page",
             "target_ref": "tab-plain-secret", "target_ref_hash": "target-ref-secret", "generation": "gen-8842",
             "content_hash": "c" * 24,
             "candidates": [
                 {"candidate_id": _C1, "key": "key-e5", "role": "button", "label": "提交订单", "actions": [_CLICK]},
                 {"candidate_id": _C2, "key": "key-e9", "role": "input:text", "label": "收货人", "actions": [_FILL]},
                 {"candidate_id": _C3, "key": "key-e12", "role": "link", "label": "帮助中心", "actions": [_CLICK]},
             ]}
    value.update(changes)
    return value


# LLM: 替身沿 plugin_observation.observation_is_current 的真实签名 (repo, *, run_id, task_id, observation_id) -> bool；
#   answers 依次返回，用尽后沿用最后一个，calls 记录每次询问。
# 函数用途: 替换本模块使用的新鲜度权威，返回调用记录。
def install_currency(monkeypatch, *answers):
    calls = []
    values = list(answers or (True,))

    def observation_is_current(repo, *, run_id, task_id, observation_id):
        calls.append((repo, run_id, task_id, observation_id))
        return values.pop(0) if len(values) > 1 else values[0]

    monkeypatch.setattr(module, "observation_is_current", observation_is_current)
    return calls


# LLM: 使用真实不可变 call/result 与原 runtime 参数；当前观察调用与其归档共用同一信封，同原归档投影一致。
# 函数用途: 构造“主模型刚调用插件 read 并拿到三个候选”的一轮，宿主带 owner 权威库替身。
@pytest.fixture
def prepared(tmp_path, monkeypatch):
    install_currency(monkeypatch)
    specs = tuple(make_test_model_spec(name) for name in (_READ, _CLICK, _FILL))
    params = ToolLoopExecuteParams(
        user_prompt="帮我在测试页下单。token=prompt-secret-value", memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section="", tool_recommendations_section="", tool_context=[], effective_on_chunk=None,
        allowed_tools=[_READ, _CLICK, _FILL], write_boundary=None, task_attributes={"agent_thread_id": "thread-1"},
        request_id="request-1", run_id="run-1", task_id="task-1", save=False,
        one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[],
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id="run-1"),
        tool_runtime_snapshot=runtime_snapshot_for_model_specs(specs, run_id="run-1"),
    )
    call = canonical_history_call(_READ, {}, call_id="read-1", run_id="run-1", turn_id="turn-2", attempt_id="attempt-1")
    result = canonical_history_result(call, '{"items": ["page-output-secret"]}', ok=True)
    archive = {"tool": _READ, "id": "read-1", "run_id": "run-1", "task_id": "task-1", "scoped_call_id": "run-1:read-1",
               "output_hash": "a" * 64, "tool_result_envelope": {"observation": observation()}}
    params.archive_tool_calls.append(archive)
    host = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(), config=SimpleNamespace(auto_save_memory=False),
                           subagents=SimpleNamespace(runtime_db="owner-runtime-db"))
    return host, ToolCallRecordParams(params=params, tool_rounds=2, idx=1, call=call, result=result), archive


# LLM: fake 沿原阶段/响应合同，只替换决策服务边界；不绕过消费者的版本、取消或非选择校验。
# 函数用途: 记录实际决策输入并返回可控结果；可设 mode/status/choice/answers/mutate/error/stage_error/stage_run_id。
def install(monkeypatch, **fake):
    calls = []
    mode = fake.get("mode", "apply")
    monkeypatch.setattr(module, "POINT_RUNTIME_SCOPES", {module._POINT: "thread"})

    def begin(_agent, run_params, **kwargs):
        assert kwargs["operation_id"].startswith(module._POINT + ":")
        return SimpleNamespace(error_code=fake.get("stage_error", ""), deadline=time.monotonic() + 5,
                               run_id=fake.get("stage_run_id", run_params.run_id),
                               enabled_points=() if mode == "off" else (module._POINT,))

    def decide(_agent, run_params, stage, **kwargs):
        calls.append((run_params, stage, kwargs))
        if fake.get("error") is not None:
            raise fake["error"]
        answers = fake["answers"] if "answers" in fake else (DecisionAnswer(_Q, "choice", fake.get("choice", "c1")),)
        binding = DecisionBinding(module._POINT, "owner", "operation", "policy", kwargs["candidates_revision"])
        status = fake.get("status", "success")
        outcome = SimpleNamespace(may_apply=mode == "apply" and status == "success", mode=mode, status=status,
                                  deadline=stage.deadline, response=DecisionResponse(
                                      binding, "digest", "decision-model", "decision-model", answers, b"{}"))
        fake.get("mutate", lambda *_args: None)(outcome, stage)
        return outcome

    monkeypatch.setattr(module, "begin_decision_stage", begin)
    monkeypatch.setattr(module, "decide", decide)
    monkeypatch.setattr(module, "decision_outcome_is_current", lambda *_args: True)
    return calls


# 函数用途: 生成“改观察记录的一个顶层字段”的变换。
def set_observation(key, value):
    def change(record, archive):
        archive["tool_result_envelope"]["observation"][key] = value
        return record, archive
    return change


# 函数用途: 生成“改第一个候选的一个字段”的变换。
def set_candidate(key, value):
    def change(record, archive):
        archive["tool_result_envelope"]["observation"]["candidates"][0][key] = value
        return record, archive
    return change


# 函数用途: 生成“改一个归档字段”的变换。
def set_archive(key, value):
    def change(record, archive):
        archive[key] = value
        return record, archive
    return change


# 函数用途: 生成“改一个运行参数字段”的变换，冻结 dataclass 沿原宿主的 object.__setattr__ 写法。
def set_params(name, value):
    def change(record, archive):
        object.__setattr__(record.params, name, value)
        return record, archive
    return change


# 函数用途: 生成“候选数量改为 count 个、编号互不相同”的变换。
def candidate_count(count):
    def change(record, archive):
        archive["tool_result_envelope"]["observation"]["candidates"] = [
            {"candidate_id": f"cand-{index:016x}", "key": f"k{index}", "role": "button", "label": f"按钮{index}",
             "actions": [_CLICK]} for index in range(count)]
        return record, archive
    return change


# 函数用途: 生成“本轮快照只剩给定工具”的变换，用于动作工具不可用的场景。
def only_tools(*names):
    def change(record, archive):
        specs = tuple(make_test_model_spec(name) for name in names)
        object.__setattr__(record.params, "tool_runtime_snapshot", runtime_snapshot_for_model_specs(specs, run_id="run-1"))
        return record, archive
    return change


_INELIGIBLE = {
    "single_candidate": candidate_count(1),
    "sixty_five_candidates": candidate_count(65),
    "no_candidates": candidate_count(0),
    "candidates_not_list": set_observation("candidates", {"c1": {}}),
    "bad_observation_id": set_observation("observation_id", "obs-short"),
    "wrong_schema": set_observation("schema", "plugin_observation.v0"),
    "bad_target_kind": set_observation("target_kind", "Page Kind"),
    "missing_target_ref": set_observation("target_ref_hash", ""),
    "missing_generation": set_observation("generation", None),
    "bad_candidate_id": set_candidate("candidate_id", "cand-XYZ"),
    "duplicate_candidate_id": set_candidate("candidate_id", _C2),
    "bad_role": set_candidate("role", "push button"),
    "long_label": set_candidate("label", "长" * 121),
    "empty_key": set_candidate("key", ""),
    "empty_actions": set_candidate("actions", []),
    "nine_actions": set_candidate("actions", [_CLICK] * 9),
    "blank_action": set_candidate("actions", [""]),
    "no_observation": set_archive("tool_result_envelope", {}),
    "archive_id": set_archive("id", "other-call"),
    "archive_run": set_archive("run_id", "other-run"),
    "archive_task": set_archive("task_id", "other-task"),
    "archive_scope": set_archive("scoped_call_id", ""),
    "archive_tool": set_archive("tool", _CLICK),
    "failed_call": lambda record, archive: (replace(record, result=replace(record.result, status="failed")), archive),
    "replayed": lambda record, archive: (replace(record, result=replace(record.result, handler_executed=False)), archive),
    "repeated_halt": set_params("repeated_failure_halt", (_READ, "code:TOOL_EXECUTION_FAILED", 3)),
    "unknown_halt": set_params("unknown_outcome_halt", (_READ, "", "unknown", True)),
    "long_request": set_params("user_prompt", "x" * 1025),
    "empty_request": set_params("user_prompt", ""),
    "request_query_url": set_params("user_prompt", "打开 https://shop.test/order?custom_credential=private 下单"),
    "label_query_url": set_candidate("label", "见 //shop.test/pay?session=private"),
    "no_available_action": only_tools(_READ),
}


def test_unregistered_point_is_strict_noop(prepared, monkeypatch):
    monkeypatch.setattr(module, "POINT_RUNTIME_SCOPES", {})
    monkeypatch.setattr(module, "_eligible", lambda *_args: pytest.fail("unregistered point cannot read sources"))
    assert module.action_candidate_hint(*prepared) == ""


@pytest.mark.parametrize("stage", [{"mode": "off"}, {"stage_error": "configuration_unavailable"},
                                   {"stage_run_id": "other-run"}], ids=["off", "stage_error", "foreign_run"])
def test_closed_or_foreign_stage_prepares_no_material(prepared, monkeypatch, stage):
    calls = install(monkeypatch, **stage)
    monkeypatch.setattr(module, "_material", lambda *_args: pytest.fail("closed point cannot prepare material"))
    assert module.action_candidate_hint(*prepared) == "" and not calls


def test_apply_appends_one_hint_for_the_chosen_candidate(prepared, monkeypatch):
    before = (deepcopy(prepared[1].result.to_dict()), deepcopy(prepared[2]))
    calls = install(monkeypatch)
    assert module.action_candidate_hint(*prepared) == _HINT_C1 and len(calls) == 1
    assert (prepared[1].result.to_dict(), prepared[2]) == before


def test_payload_sends_only_request_kind_roles_and_labels(prepared, monkeypatch):
    calls = install(monkeypatch, choice="not_needed")
    module.action_candidate_hint(*prepared)
    kwargs = calls[0][2]
    payload = json.dumps({"state": kwargs["state"], "questions": kwargs["questions"]}, ensure_ascii=False)
    for private in ("key-e5", "target-ref-secret", "tab-plain-secret", "gen-8842", "activation-secret-7", "cand-", "plugin__", "obs-",
                    "prompt-secret-value", "page-output-secret", "host-command:"):
        assert private not in payload
    for label in ("提交订单", "收货人", "帮助中心"):
        assert payload.count(label) == 1, "label 只出现在同一个外部数据块里"
    assert kwargs["state"]["target_kind"] == "page"
    assert kwargs["state"]["candidates"].startswith("<untrusted_tool_result")
    criteria = kwargs["questions"][_Q]["criteria"]
    assert {key: value for key, value in criteria.items() if key.startswith("c")} == {
        "c1": {"role": "button"}, "c2": {"role": "input:text"}, "c3": {"role": "link"}}
    assert set(criteria) - {"c1", "c2", "c3"} == set(module._NON_SELECTIONS)
    assert kwargs["source_refs"] == ("run-1:read-1",)


@pytest.mark.parametrize("mode,status", [("observe", "success"), ("apply", "deadline"), ("apply", "cooldown"),
                                         ("apply", "error"), ("apply", "stale")])
def test_observe_and_failed_outcomes_keep_original(prepared, monkeypatch, mode, status):
    before = (deepcopy(prepared[1].result.to_dict()), deepcopy(prepared[2]))
    calls = install(monkeypatch, mode=mode, status=status)
    assert module.action_candidate_hint(*prepared) == "" and len(calls) == 1
    assert (prepared[1].result.to_dict(), prepared[2]) == before


@pytest.mark.parametrize("answers", [
    (DecisionAnswer(_Q, "choice", "not_needed"),), (DecisionAnswer(_Q, "choice", "no_match"),),
    (DecisionAnswer(_Q, "choice", "abstain"),), (DecisionAnswer(_Q, "choice", "need_data"),),
    (DecisionAnswer(_Q, "choice", "c9"),), (DecisionAnswer("other_question", "choice", "c1"),),
    (DecisionAnswer(_Q, "choice", "c1"), DecisionAnswer(_Q, "choice", "c2")), (),
], ids=["not_needed", "no_match", "abstain", "need_data", "unknown_alias", "other_question", "two_answers", "none"])
def test_non_selections_and_invalid_answers_keep_original(prepared, monkeypatch, answers):
    calls = install(monkeypatch, answers=answers)
    assert module.action_candidate_hint(*prepared) == "" and len(calls) == 1


def test_chosen_candidate_without_an_available_action_adds_nothing(prepared, monkeypatch):
    record, archive = only_tools(_READ, _FILL)(prepared[1], prepared[2])
    calls = install(monkeypatch, choice="c1")
    assert module.action_candidate_hint(prepared[0], record, archive) == "" and len(calls) == 1


@pytest.mark.parametrize("scenario", _INELIGIBLE, ids=list(_INELIGIBLE))
def test_ineligible_records_send_no_request(prepared, monkeypatch, scenario):
    calls = install(monkeypatch)
    record, archive = _INELIGIBLE[scenario](prepared[1], prepared[2])
    assert module.action_candidate_hint(prepared[0], record, archive) == "" and calls == []


def test_two_and_sixty_four_candidates_are_eligible(prepared, monkeypatch):
    for count in (2, 64):
        calls = install(monkeypatch, choice="not_needed")
        record, archive = candidate_count(count)(prepared[1], deepcopy(prepared[2]))
        module.action_candidate_hint(prepared[0], record, archive)
        assert len(calls) == 1


def test_stale_observation_sends_no_request_and_asks_the_authority(prepared, monkeypatch):
    currency = install_currency(monkeypatch, False)
    calls = install(monkeypatch)
    assert module.action_candidate_hint(*prepared) == "" and calls == []
    assert currency == [("owner-runtime-db", "run-1", "task-1", _OBS)]


def test_observation_that_goes_stale_during_the_wait_adds_nothing(prepared, monkeypatch):
    currency = install_currency(monkeypatch, True, False)
    calls = install(monkeypatch)
    assert module.action_candidate_hint(*prepared) == "" and len(calls) == 1
    assert len(currency) == 2, "采用前必须再问一次新鲜度权威"


def test_missing_owner_runtime_db_is_not_current(prepared, monkeypatch):
    currency = install_currency(monkeypatch)
    calls = install(monkeypatch)
    host = SimpleNamespace(**{**vars(prepared[0]), "subagents": SimpleNamespace(runtime_db=None)})
    assert module.action_candidate_hint(host, prepared[1], prepared[2]) == "" and calls == [] and currency == []


def test_failing_freshness_authority_fails_closed(prepared, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("runtime db unavailable")

    monkeypatch.setattr(module, "observation_is_current", broken)
    calls = install(monkeypatch)
    assert module.action_candidate_hint(*prepared) == "" and calls == []


def test_subagent_context_sends_no_request(prepared, monkeypatch):
    calls = install(monkeypatch)
    token = set_current_subagent_context(prepared[0], run_id="child-run", task_attributes={"agent_thread_id": "thread-1"})
    try:
        assert module.action_candidate_hint(*prepared) == "" and calls == []
    finally:
        restore_current_subagent_context(prepared[0], token)


def test_sources_changed_during_the_wait_drop_the_hint(prepared, monkeypatch):
    def relabel(*_args):
        prepared[2]["tool_result_envelope"]["observation"]["candidates"][0]["label"] = "取消订单"

    calls = install(monkeypatch, mutate=relabel)
    assert module.action_candidate_hint(*prepared) == "" and len(calls) == 1


def test_changed_tool_snapshot_during_the_wait_drops_the_hint(prepared, monkeypatch):
    install(monkeypatch, mutate=lambda *_args: only_tools(_READ, _CLICK)(prepared[1], prepared[2]))
    assert module.action_candidate_hint(*prepared) == ""


@pytest.mark.parametrize("mutate", [
    lambda outcome, _stage: setattr(outcome, "deadline", 0),
    lambda _outcome, stage: setattr(stage, "deadline", 0),
    lambda outcome, _stage: setattr(outcome, "response", replace(outcome.response, binding=replace(
        outcome.response.binding, candidates_revision="old"))),
], ids=["outcome_deadline", "stage_deadline", "candidates_revision"])
def test_deadline_and_binding_are_checked_before_adoption(prepared, monkeypatch, mutate):
    calls = install(monkeypatch, mutate=mutate)
    assert module.action_candidate_hint(*prepared) == "" and len(calls) == 1


def test_changed_policy_blocks_adoption(prepared, monkeypatch):
    calls = install(monkeypatch)
    monkeypatch.setattr(module, "decision_outcome_is_current", lambda *_args: False)
    assert module.action_candidate_hint(*prepared) == "" and len(calls) == 1


@pytest.mark.parametrize("error", [RuntimeError("provider failure"), ValueError("bad value"),
                                   DecisionInputError("too large"), TimeoutError("late")])
def test_optional_errors_keep_the_original_result(prepared, monkeypatch, error):
    before = (deepcopy(prepared[1].result.to_dict()), deepcopy(prepared[2]))
    calls = install(monkeypatch, error=error)
    assert module.action_candidate_hint(*prepared) == "" and len(calls) == 1
    assert (prepared[1].result.to_dict(), prepared[2]) == before


@pytest.mark.parametrize("status", ["success", "deadline", "error", "stale"])
def test_host_cancellation_propagates_before_adoption(prepared, monkeypatch, status):
    token = prepared[1].params.cancellation_token
    install(monkeypatch, status=status, mutate=lambda *_args: token.cancel("user-stop"))
    with pytest.raises(ToolCancelled):
        module.action_candidate_hint(*prepared)


def test_cancellation_is_not_hidden_by_an_optional_error(prepared, monkeypatch):
    token = prepared[1].params.cancellation_token

    def fail(*_args, **_kwargs):
        token.cancel("user-stop")
        raise TimeoutError("optional call also timed out")

    install(monkeypatch)
    monkeypatch.setattr(module, "decide", fail)
    with pytest.raises(ToolCancelled):
        module.action_candidate_hint(*prepared)


def test_interrupt_from_the_decision_boundary_propagates(prepared, monkeypatch):
    install(monkeypatch, error=InterruptedError("user stop"))
    with pytest.raises(InterruptedError):
        module.action_candidate_hint(*prepared)


@pytest.mark.parametrize("mode", ["off", "observe", "apply"])
def test_host_text_and_native_share_one_appended_hint(prepared, monkeypatch, mode):
    host, record, archive = prepared
    record.params.archive_tool_calls.pop()  # 原 _record_tool_call 会自行追加这一条归档。
    original_result, original_archive = deepcopy(record.result.to_dict()), deepcopy(archive)
    calls = install(monkeypatch, mode=mode)
    ledger = isolate_recording(monkeypatch, archive)
    baseline = render_tool_result_for_live_prompt(record.result, archive)
    loop._record_tool_call(host, record)
    history_result = next(item for item in record.params.tool_ir_history if isinstance(item, ToolResult))
    messages = AnthropicMessageAdapter().to_provider_messages(record.params.tool_ir_history)
    native = next(block["content"] for message in messages for block in message["content"] if block["type"] == "tool_result")
    text = record.params.tool_context[0].split("[tool-output-record round=2 index=1]\n", 1)[1]
    assert text == native == history_result.render_for_model_prompt()
    assert history_result.output == record.result.output
    assert text == baseline + ("\n" + _HINT_C1 if mode == "apply" else "")
    assert text.count("[action-candidate]") == (1 if mode == "apply" else 0)
    assert len(calls) == (0 if mode == "off" else 1)
    assert record.result.to_dict() == original_result and archive == original_archive
    assert ledger == [original_archive] and record.params.archive_tool_calls[-1] is archive


def test_observation_record_reaches_at_most_one_decision_point(prepared, monkeypatch):
    reading_calls, _ = install_reading(monkeypatch)
    calls = install(monkeypatch)
    assert loop._optional_result_hints(*prepared) == "\n" + _HINT_C1
    assert len(calls) == 1 and not reading_calls


def test_config_defaults_are_off_and_thread_scoped(tmp_path):
    config = load_config(Path(__file__).parents[1] / "config" / "agent_config.yaml")
    defaults = AgentConfig()
    for suffix in ("mode", "timeout_seconds", "profile_id"):
        name = f"decision_{module._POINT}_{suffix}"
        assert getattr(config, name) == getattr(defaults, name) == ("off" if suffix == "mode" else None)
        assert decision_field_scopes()[f"points.{module._POINT}.{suffix}"] == ["owner", "thread"]
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    view = settings(host, "read", {"scope": "thread"}, thread_id=thread.thread_id)
    point = view["effective"]["points"][module._POINT]
    assert point["mode"] == point["effective_mode"] == "off" and point["runtime_scope"] == "thread"
    patch(host, {"enabled": True, f"points.{module._POINT}.mode": "apply"}, thread_id=thread.thread_id, scope="thread")
    view = settings(host, "read", {"scope": "thread"}, thread_id=thread.thread_id)
    assert view["effective"]["points"][module._POINT]["effective_mode"] == "apply"
    assert settings(host, "read", {})["effective"]["points"][module._POINT]["mode"] == "off"


def test_original_tui_can_edit_action_candidate_thread_mode(tmp_path):
    async def scenario():
        gateway = Gateway(tmp_path)
        async with running(tmp_path, gateway) as ui:
            await open_scope(ui, thread=True)
            await choose(ui, 5)
            view = settings(gateway.host, "read", {"scope": "thread"}, thread_id=gateway.thread.thread_id)
            points = [key for key in tui_decision_menu._POINTS if f"points.{key}.mode" in tui_decision_menu._fields(view)]
            assert "动作候选" in visible(ui.app)
            await choose(ui, points.index(module._POINT))
            await choose(ui, 0)
            await press(ui, b"\x1b[B\x1b[B\r")
            result = settings(gateway.host, "read", {"scope": "thread"}, thread_id=gateway.thread.thread_id)
            assert result["overrides"]["thread"][f"points.{module._POINT}.mode"] == "apply"
            assert not any(name in {"decision_probe", "select", "set_default"} for name, _payload in gateway.calls)
    asyncio.run(scenario())


# LLM: 与真实工具循环同一写入口 persist_tool_runtime_ledger；归档形状照产品归档且没有 runtime_gate（只读观察工具不经审批），
#   宿主带真实 LocalStore，用来钉住“门台账不写、权威事件仍写”的真实链路；有副作用：写 tmp_path 下的本地库与假权威库。
# 函数用途: 把一次宿主铸造的观察按产品写法归档进权威事件流，返回归档与观察记录。
def persist_observation(host, payload, *, operation_id, call_id):
    from agent_py_agent.agent.agent_core.tool_runtime_ledger import persist_tool_runtime_ledger
    from agent_py_agent.agent.plugin_observation import parse_observation
    from agent_py_agent.tests.test_plugin_observation import _context

    record = parse_observation(payload, _context(operation_id=operation_id))
    archive = {"tool": record.tool_name, "id": call_id, "run_id": "run-1", "task_id": "task-1", "operation_id": operation_id,
               "attempt_id": "attempt-1", "ok": True, "error_code": "", "idempotency_key": "",
               "scoped_call_id": f"run-1:{call_id}", "output_hash": "a" * 64,
               "tool_result_envelope": {"observation": record.to_envelope()}}
    persist_tool_runtime_ledger(host, archive)
    return archive, record


# LLM: 集成夹具：观察经插件线的 parse_observation 铸造，并经产品写入口 persist_tool_runtime_ledger 记进假权威库；
#   不替换 observation_is_current，只替换决策服务边界，用来钉住与真实新鲜度合同（run_id 映射、按事件序判当前）的接缝。
# 函数用途: 构造一次真实铸造的 browser-lite 读观察调用，返回宿主、调用记录、归档、假库与首个观察记录。
def real_observation_turn(tmp_path):
    from agent_py_agent.agent.local_storage import LocalStore
    from agent_py_agent.tests.test_plugin_observation import ACTIONS, _payload, _Repo

    repo = _Repo()
    host = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(), config=SimpleNamespace(auto_save_memory=False),
                           local_store=LocalStore(tmp_path / "local.db", enable_fts=False),
                           subagents=SimpleNamespace(runtime_db=repo))
    archive, record = persist_observation(host, _payload(), operation_id="op-1", call_id="read-1")
    tools = (record.tool_name, *ACTIONS.values())
    params = ToolLoopExecuteParams(
        user_prompt="帮我在测试页下单。", memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section="", tool_recommendations_section="", tool_context=[], effective_on_chunk=None,
        allowed_tools=list(tools), write_boundary=None, task_attributes={"agent_thread_id": "thread-1"},
        request_id="request-1", run_id="run-1", task_id="task-1", save=False,
        one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[],
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id="run-1"),
        tool_runtime_snapshot=runtime_snapshot_for_model_specs(tuple(make_test_model_spec(name) for name in tools), run_id="run-1"),
    )
    call = canonical_history_call(record.tool_name, {}, call_id="read-1", run_id="run-1", turn_id="turn-2", attempt_id="attempt-1")
    result = canonical_history_result(call, '{"items": ["page-output"]}', ok=True)
    params.archive_tool_calls.append(archive)
    return host, ToolCallRecordParams(params=params, tool_rounds=2, idx=1, call=call, result=result), archive, repo, record


def test_real_freshness_authority_accepts_the_current_observation(tmp_path, monkeypatch):
    host, record, archive, repo, observed = real_observation_turn(tmp_path)
    assert host.local_store.list_runtime_gate_ledger(run_id="run-1") == [], "没有 runtime_gate：门台账不写"
    assert [row["event_type"] for row in repo.events] == ["tool_completed"], "权威完成事件照写，观察才有新鲜度事实源"
    calls = install(monkeypatch, choice="c1")
    hint = module.action_candidate_hint(host, record, archive)
    first = observed.candidates[0]
    assert hint == (f"[action-candidate]\n可选操作建议：下一步可先核对候选 {first.candidate_id}（{first.role}）；"
                    "是否操作、如何操作仍由你按原工具与审批决定。") and len(calls) == 1


def test_real_freshness_authority_rejects_an_observation_replaced_for_the_same_target(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_plugin_observation import _payload

    host, record, archive, _repo, _observed = real_observation_turn(tmp_path)
    persist_observation(host, _payload(target={"ref": "tab-3", "generation": "18"}), operation_id="op-2", call_id="read-2")
    calls = install(monkeypatch)
    assert module.action_candidate_hint(host, record, archive) == "" and calls == [], "同一目标有了更新观察，旧候选不再提示"
