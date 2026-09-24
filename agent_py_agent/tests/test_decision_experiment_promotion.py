"""F1b 授权内自动晋升：/experiment apply 只让宿主在证据规则满足时经原设置 CAS 把本会话 skill_tool 改为 apply；
真实设置服务、真实 E1 授权入口、原 Gateway 请求文件与回合转换锁；样本条目为合成结构化事实，无网络、无真实模型。"""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_command
from agent_py_agent.agent.conversation.decision_policy import decision_owner_ref
from agent_py_agent.agent.gateway_parts import request_experiment_promotion as promotion
from agent_py_agent.agent.gateway_parts import request_experiment_records as records
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
from agent_py_agent.agent.settings.decision_settings import (
    execute_decision_settings_operation as execute,
)
from agent_py_agent.agent.settings.decision_settings_schema import DecisionSettingsAccessError
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_settings import host_at, patch

KEY = records.EXPERIMENT_RECORDS_KEY
FIELD = "points.skill_tool.mode"


# LLM: 真实原设置宿主（owner/thread 锁与 CAS）与原 Gateway 目录；决策连接只是已保存引用，不联网。
# 函数用途: 建立开启决策与实验能力、skill_tool 仍为 off 的隔离 owner 与会话。
def lane_at(tmp_path):
    host = host_at(tmp_path / "owner")
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    key, _ = decision(host)
    patch(host, {"enabled": True, "experiment_enabled": True, "profile_id": key})
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.processing.mkdir(parents=True)
    paths.terminal.mkdir(parents=True)
    return SimpleNamespace(host=host, thread=thread, paths=paths, owner_ref=decision_owner_ref(host))


@pytest.fixture
def lane(tmp_path):
    return lane_at(tmp_path)


# LLM: 沿原入口解析命令并冻结 system_task，再经 GatewayTaskBindingWriter 走真实 E1 授权；params 模拟主轮已绑定 run/attempt。
# 函数用途: 建立一条 processing 中的 /experiment 请求，完成宿主授权并返回其 Gateway 上下文。
def start_turn(lane, request_id, mode):
    command = parse_conversation_command(f"/experiment {mode} skill_tool 10m 1 50000 核对来源")
    request = {"id": request_id, "kind": "ask", "status": "processing", "turn_phase": "open",
               "execution_attempt_id": "exec-" + request_id, "user_id": "alice",
               "metadata": {"channel": "chat", "user_id": "alice"}, "goal": command.prompt,
               "system_task": command.to_request_payload()}
    path = lane.paths.processing / f"{request_id}.json"
    path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    writer = GatewayTaskBindingWriter(path, request_id, request, "exec-" + request_id)
    thread_id, notices = lane.thread.thread_id, []
    params = SimpleNamespace(thread_id=thread_id, run_id="run-" + request_id, task_id="", attempt_id="attempt-" + request_id,
                             request_id=request_id, task_attributes={"conversation_thread_id": thread_id},
                             on_chunk=notices.append)
    receipt = writer.grant_decision_experiment(lane.host, params)
    return SimpleNamespace(request_path=path, request_id=request_id, request=request, agent=lane.host, writer=writer,
                           receipt=receipt, notices=notices)


# LLM: 合成与生产同 schema 的实验条目（候选/结算为结构化事实），经真实写入器落进请求记录。
# 函数用途: 为一条实验请求写入一次只观察实验的对照记录。
def sample(lane, turn, *, outcome="charged", owner_ref=None):
    records.record_decision_experiment_sample(turn, {
        "schema": "decision_experiment_record.v1", "record_id": "call-" + turn.request_id, "status": "observed",
        "point": "skill_tool", "realized": None, "settlement": {"outcome": outcome},
        "refs": {"owner_ref": owner_ref or lane.owner_ref, "thread_id": lane.thread.thread_id, "request_id": turn.request_id},
        "candidate": {"status": "projected", "shortlist_names": ["presentation_optional_a", "read_file"],
                      "deferred_names": ["presentation_optional_b"], "deferred_count": 1, "names_truncated": False}})


def finish(turn, tools=("presentation_optional_a",)):
    result = SimpleNamespace(runtime_status="ok", turn_end_reason="completed",
                             archive_tool_calls=[{"tool": name} for name in tools])
    records.finish_decision_experiment_turn(turn, result)


# LLM: 已结束的旧实验请求按原终态目录保存，证据链沿授权回执指针回读它们。
# 函数用途: 生成一条已完成、已归档的历史实验样本请求。
def prior_sample(lane, request_id, mode, **changes):
    turn = start_turn(lane, request_id, mode)
    sample(lane, turn, **changes)
    finish(turn)
    turn.request_path.replace(lane.paths.terminal / turn.request_path.name)
    return turn


def view(lane):
    return execute(lane.host, "read", {"scope": "thread"}, thread_id=lane.thread.thread_id)


def stored_block(turn):
    stored = json.loads(turn.request_path.read_text(encoding="utf-8"))
    assert stored.get(KEY) == turn.request.get(KEY), "文件与内存中的请求记录必须一致"
    return stored.get(KEY) or {}


# LLM: 标准晋升前置：两条历史样本加本轮一条（共三条可比较样本），本轮授权动作由 mode 决定。
# 函数用途: 准备一条证据已满足规则、等待回合收尾的实验请求。
def ready_turn(lane, mode="apply"):
    for index in range(2):
        prior_sample(lane, f"req-{index}", "observe")
    turn = start_turn(lane, "req-now", mode)
    sample(lane, turn)
    return turn


def test_apply_command_grants_observe_plus_apply_and_the_call_stays_observe(lane):
    command = parse_conversation_command("/experiment apply skill_tool 10m 1 50000 任务")
    assert command.valid and command.attributes["mode"] == "apply" and command.prompt == "任务"
    turn = start_turn(lane, "req-now", "apply")
    assert turn.receipt["operations"] == ["observe", "apply"] and turn.receipt["previous_request_id"] == ""
    assert turn.receipt["thread_id"] == lane.thread.thread_id and "apply" in turn.notices[0]
    authorization = view(lane)["experiment_authorization"]
    assert authorization["operations"] == ["observe", "apply"] and authorization["status"] == "active"
    assert view(lane)["effective"]["points"]["skill_tool"]["effective_mode"] == "off", "授权本身不改设置"
    observe = start_turn(lane, "req-next", "observe")
    assert observe.receipt["operations"] == ["observe"] and observe.receipt["previous_request_id"] == "req-now"


def test_apply_grant_promotes_once_through_the_settings_cas_with_a_receipt(lane):
    turn = ready_turn(lane)
    before = view(lane)
    finish(turn)
    after = view(lane)
    assert after["overrides"]["thread"][FIELD] == "apply"
    assert after["effective"]["points"]["skill_tool"]["effective_mode"] == "apply"
    assert after["revision"] == {"owner": before["revision"]["owner"], "thread": before["revision"]["thread"] + 1}
    receipt = stored_block(turn)["promotion"]
    assert (receipt["status"], receipt["reason"]) == ("applied", "")
    assert receipt["authorization_id"] == turn.receipt["authorization_id"]
    assert (receipt["field"], receipt["scope"], receipt["to"]) == (FIELD, "thread", "apply")
    assert receipt["before"] == {"thread_override_present": False, "thread_override": None, "effective_mode": "off",
                                 "revision": before["revision"]}
    assert receipt["after"] == {"thread_override_present": True, "thread_override": "apply", "effective_mode": "apply",
                                "revision": after["revision"]}
    assert receipt["evaluation"]["status"] == "proposal" and receipt["evaluation"]["comparable_count"] == 3
    assert receipt["evaluation"]["record_ids"] == ["call-req-now", "call-req-1", "call-req-0"]
    assert view(lane)["experiment_authorization"]["status"] == "active", "晋升不撤销或改写授权信封"


def test_replay_and_restart_never_apply_twice(lane):
    turn = ready_turn(lane)
    finish(turn)
    applied = view(lane)
    finish(turn)
    reloaded = {**json.loads(turn.request_path.read_text(encoding="utf-8")), "execution_attempt_id": "exec-restarted"}
    turn.request_path.write_text(json.dumps(reloaded, ensure_ascii=False), encoding="utf-8")
    restarted = SimpleNamespace(request_path=turn.request_path, request_id=turn.request_id, request=reloaded, agent=lane.host)
    finish(restarted)
    assert view(lane)["revision"] == applied["revision"]
    assert stored_block(restarted)["promotion"]["status"] == "applied"


@pytest.mark.parametrize("in_memory", [True, False])
def test_crash_left_promoting_marker_is_never_retried(lane, monkeypatch, in_memory):
    turn = ready_turn(lane)
    marker = {"status": "promoting", "authorization_id": turn.receipt["authorization_id"]}
    if in_memory:
        records.run_in_turn(turn, "experiment_record", lambda: records.update_experiment_records(
            turn, lambda block: {**block, "promotion": marker}))
    else:
        # 内存快判放行之后、锁内写入之前，另一执行者在文件里留下标记：锁内“已有回执即放弃”必须挡住。
        evaluate = promotion.request_experiment_evaluation

        def concurrent(context, *, thread_id):
            result = evaluate(context, thread_id=thread_id)
            stored = json.loads(turn.request_path.read_text(encoding="utf-8"))
            stored[KEY]["promotion"] = marker
            turn.request_path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
            return result

        monkeypatch.setattr(promotion, "request_experiment_evaluation", concurrent)
    finish(turn)
    assert stored_block(turn)["promotion"] == marker
    assert FIELD not in view(lane)["overrides"]["thread"]


def test_observe_only_authorization_never_applies_but_the_proposal_is_readable(lane):
    turn = ready_turn(lane, "observe")
    before = view(lane)
    finish(turn)
    assert view(lane)["revision"] == before["revision"] and "promotion" not in stored_block(turn)
    evaluation = turn.writer.decision_experiment_evaluation(lane.host, lane.thread.thread_id, before["experiment_authorization"])
    assert evaluation["status"] == "proposal" and evaluation["latest_promotion"] is None


def test_user_change_between_read_and_write_is_kept_and_promotion_skips(lane, monkeypatch):
    turn = ready_turn(lane)
    original, calls = promotion.execute_decision_settings_operation, []

    # 锁内第一次读之后、任何后续设置调用之前，用户改了另一个字段；晋升必须用第一次读到的版本做 CAS。
    def racing(agent, operation, payload, *, thread_id=""):
        calls.append(operation)
        if len(calls) == 2:
            patch(lane.host, {"points.skill_tool.timeout_seconds": 3}, thread_id=thread_id, scope="thread")
        return original(agent, operation, payload, thread_id=thread_id)

    monkeypatch.setattr(promotion, "execute_decision_settings_operation", racing)
    finish(turn)
    overrides = view(lane)["overrides"]["thread"]
    assert FIELD not in overrides and overrides["points.skill_tool.timeout_seconds"] == 3
    receipt = stored_block(turn)["promotion"]
    assert (receipt["status"], receipt["reason"], receipt["after"]) == ("skipped", "settings_conflict", None)


# LLM: 用户在授权之后、回合收尾之前的任何修改（线程层、用户层或默认配置改变有效模式）都让晋升跳过，绝不覆盖。
# 函数用途: 施加一种“用户后改”并返回预期的跳过原因。
def _later_change(lane, case):
    thread_id = lane.thread.thread_id
    if case == "thread":
        patch(lane.host, {"points.skill_tool.timeout_seconds": 3}, thread_id=thread_id, scope="thread")
        return "settings_changed"
    if case == "owner":
        patch(lane.host, {"timeout_seconds": 3}, thread_id=thread_id)
        return "settings_changed"
    lane.host.capability_config.decision_skill_tool_mode = "observe"
    return "point_not_off"


@pytest.mark.parametrize("case", ["thread", "owner", "default"])
def test_any_later_user_change_skips_without_overwrite(lane, case):
    turn = ready_turn(lane)
    reason = _later_change(lane, case)
    finish(turn)
    receipt = stored_block(turn)["promotion"]
    assert (receipt["status"], receipt["reason"]) == ("skipped", reason)
    assert FIELD not in view(lane)["overrides"]["thread"]


# LLM: 撤销走原 user_config 同一服务；到期只改晋升模块读到的墙钟；替换是同会话又一次宿主授权。
# 函数用途: 让本轮 apply 授权失效并返回预期的跳过原因。
def _invalidate(lane, turn, case, monkeypatch):
    if case == "revoked":
        current = view(lane)
        execute(lane.host, "experiment_revoke", {"scope": "thread", "expected_revision": current["revision"],
                                                 "authorization_id": turn.receipt["authorization_id"]},
                thread_id=lane.thread.thread_id)
        return "authorization_revoked"
    if case == "expired":
        expires_at = view(lane)["experiment_authorization"]["expires_at"]
        monkeypatch.setattr(promotion, "time", SimpleNamespace(time=lambda: expires_at))
        return "authorization_expired"
    start_turn(lane, "req-newer", "observe")
    return "authorization_replaced"


@pytest.mark.parametrize("case", ["revoked", "expired", "replaced"])
def test_revoked_expired_or_replaced_authorization_never_applies(lane, monkeypatch, case):
    turn = ready_turn(lane)
    reason = _invalidate(lane, turn, case, monkeypatch)
    finish(turn)
    receipt = stored_block(turn)["promotion"]
    assert (receipt["status"], receipt["reason"]) == ("skipped", reason)
    assert FIELD not in view(lane)["overrides"]["thread"]


# LLM: 证据不满足规则的几种形态；本轮 Jev 候选都是“完美”短名单（召回 1、有节省），仍不能越过规则。
# 函数用途: 准备一条 apply 授权请求，其证据链不满足规则，并返回预期原因码。
def _weak_evidence(lane, case):
    if case == "one_sample":
        turn = start_turn(lane, "req-now", "apply")
        sample(lane, turn)
        return turn, ("presentation_optional_a",), "insufficient_samples"
    for index in range(2):
        changes = {"outcome": "usage_unknown"} if case == "usage_unknown" and index == 0 else {}
        changes.update({"owner_ref": "other-owner"} if case == "foreign" else {})
        prior_sample(lane, f"req-{index}", "observe", **changes)
    turn = start_turn(lane, "req-now", "apply")
    sample(lane, turn)
    tools = ("presentation_optional_b",) if case == "recall" else ("presentation_optional_a",)
    return turn, tools, {"recall": "recall_below_one", "usage_unknown": "settlement_not_charged",
                         "foreign": "insufficient_samples"}[case]


@pytest.mark.parametrize("case", ["one_sample", "recall", "usage_unknown", "foreign"])
def test_a_jev_answer_cannot_trigger_apply_outside_the_rule(lane, case):
    turn, tools, reason = _weak_evidence(lane, case)
    finish(turn, tools)
    receipt = stored_block(turn)["promotion"]
    assert (receipt["status"], receipt["reason"]) == ("skipped", "evaluation_keep_observing")
    assert reason in receipt["evaluation"]["reasons"] and receipt["before"] is None
    assert FIELD not in view(lane)["overrides"]["thread"]


def test_rejected_grant_or_model_tools_have_no_path_to_grant_or_apply(lane):
    for index in range(2):
        prior_sample(lane, f"req-{index}", "observe")
    patch(lane.host, {"experiment_enabled": False})
    turn = start_turn(lane, "req-now", "apply")
    assert turn.receipt["status"] == "rejected" and turn.receipt["code"] == "experiment_disabled"
    finish(turn)
    assert "promotion" not in stored_block(turn) and FIELD not in view(lane)["overrides"]["thread"]
    actions = UserConfigTool(lane.host).model_spec.input_schema["properties"]["action"]["enum"]
    assert not {"decision_experiment_authorize", "decision_experiment_apply", "decision_promote"} & set(actions)
    with pytest.raises(DecisionSettingsAccessError):
        execute(lane.host, "experiment_authorize", {"scope": "thread", "expected_revision": view(lane)["revision"]},
                thread_id=lane.thread.thread_id)
