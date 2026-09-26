"""管理员控制（每个用户的 Jev/审计开关、跨用户审计许可）与统一审计工具的组合验证，不联网、不读日志或正文。"""
from __future__ import annotations

import json
import os
import stat
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import (
    model_call_summary,
    record_model_call_finished,
    record_model_call_timeout,
)
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallLedger,
    ModelCallLedgerOptions,
    ModelCallStartedParams,
)
from agent_py_agent.agent.contracts.tool_manifest_contract import tool_manifest_payload
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.tooling.admin_controls_tool import AdminControlsTool
from agent_py_agent.agent.user_space.owner_admin_controls import (
    OwnerAdminControlsError,
    cross_owner_audit_allowed,
    owner_home_paths_by_id,
    read_owner_admin_controls,
    set_owner_admin_controls,
)

ALICE = "providers/feishu/users/ou-alice"
# (provider, owner_kind, owner_id)
MAIN, ALICE_OWNER = ("local", "main", "main"), ("feishu", "user", "ou-alice")


# LLM: 真实 Agent 注册链与 owner 身份，所有 owner 共用一个临时 MY_AGENT_HOME，管理员才能看到其它用户目录。
# 函数用途: 构造本机 main 或远程 user/group owner 的 Agent，用于工具注册与执行。
def _agent(tmp_path, monkeypatch, owner=MAIN) -> SimpleAgent:
    provider, owner_kind, owner_id = owner
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    return SimpleAgent(
        AgentConfig(model_backend="echo", enable_tools=True,
                    my_agent_owner_provider=provider, my_agent_owner_kind=owner_kind, my_agent_owner_id=owner_id),
        tmp_path / "project",
    )


# LLM: 用真实原账本生成决策用途分区（finished 带已报输入、timed_out 无用量），不伪造 summary 结构。
# 函数用途: 生成一份包含若干决策调用的 model_calls 摘要。
def _decision_calls(*, finished: int, timed_out: int, input_tokens: int = 5000) -> dict:
    ledger = ModelCallLedger(ModelCallLedgerOptions(max_records=64))
    for index in range(finished + timed_out):
        call_id = f"decision-{index}"
        ledger.started(ModelCallStartedParams(call_id, "typesafe_decision", "jev", 42, request_id="req", run_id="run",
                                              metadata={"purpose": "decision", "logical_call_id": call_id}))
        if index < finished:
            record_model_call_finished(ledger, call_id, SimpleNamespace(usage={"input_tokens": input_tokens}))
        else:
            record_model_call_timeout(ledger=ledger, call_id=call_id, timeout_seconds=2.0, timeout_stage="wall_clock")
    return model_call_summary(SimpleNamespace(_model_call_ledger=ledger), request_id="req", run_id="run")


# LLM: 经正式增量入口写一条用量事件，created_at 由 now 指定（请求编号随之派生），用来验证时间窗。
# 函数用途: 给某个会话追加一条决策用量事件。
def _persist(agent, thread_id, summary, now):
    agent.conversation_store.model_usage.append_snapshot_once({
        "thread_id": thread_id, "request_id": f"req-{int(now)}", "run_id": "run", "task_id": "task",
        "source": "test", "model_calls": summary, "now": now})


# LLM: 按 Gateway 队列布局写一份请求记录（含不该出现在审计里的正文字段），可指定修改时间。
# 函数用途: 构造审计扫描用的请求记录。
def _request_record(root, request_id, thread_id, age_hours=0.0):
    path = root / "requests" / "done" / f"{request_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "id": request_id, "kind": "ask", "goal": "SECRET-PROMPT-TEXT",
        "conversation_claim": {"schema_version": "gateway_conversation_claim.v1", "request_id": request_id,
                               "thread_id": thread_id, "task_id": f"gateway:{request_id}"},
        "model_selection_observation": {"schema": "gateway_model_selection_observation.v1", "thread_id": thread_id,
                                        "status": "observed", "reason": "", "choice": "profile-b", "adopted": False,
                                        "adoption_eligibility": "not_evaluated", "requested_mode": "observe",
                                        "frozen_profile_id": "profile-a"},
        "capability_presentation_observation": {"schema": "gateway_capability_presentation_observation.v1", "entries": [
            {"point": "skill_tool", "mode": "observe", "status": "deadline", "reason": "budget_exhausted",
             "adopted": False, "retain_reason": "deadline", "shortlist_tool_names": ["SECRET-TOOL-LIST"]}]},
    }), encoding="utf-8")
    when = time.time() - age_hours * 3600
    os.utime(path, (when, when))
    return path


# LLM: 写入器只借请求路径推出队列根目录；其余字段与真实 Gateway 一致，不执行任何回合。
# 函数用途: 让 Agent 处在一个 Gateway 回合里，审计工具可以经宿主写入器读取请求记录。
def _in_gateway_turn(agent, root, thread_id):
    writer = GatewayTaskBindingWriter(request_path=root / "requests" / "processing" / "current.json", request_id="current")
    agent._current_run_params = SimpleNamespace(conversation_task_binding_callback=writer,
                                                task_attributes={"conversation_thread_id": thread_id})


# 函数用途: 调一次工具并把输出解析成 JSON。
def _call(agent, name, params):
    outcome = agent.tools.tools[name].execute(params)
    return outcome, json.loads(outcome.output)


def test_registration_audit_for_main_and_user_admin_controls_only_for_local_main(tmp_path, monkeypatch):
    main = _agent(tmp_path, monkeypatch)
    alice = _agent(tmp_path, monkeypatch, ALICE_OWNER)
    group = _agent(tmp_path, monkeypatch, ("feishu", "group", "group-a"))
    visible = {name: set(tool_manifest_payload(agent.tools.runtime_snapshot(run_id=name))["visible_tools"])
               for name, agent in (("main", main), ("alice", alice), ("group", group))}
    assert {"audit_records", "admin_controls"} <= visible["main"]
    assert "audit_records" in visible["alice"] and "admin_controls" not in visible["alice"]
    assert not {"audit_records", "admin_controls"} & visible["group"]
    assert AdminControlsTool.runtime_policy.approval_policy.mode == "always"


def test_owner_controls_default_fail_closed_and_keep_other_policy_fields(tmp_path, monkeypatch):
    main = _agent(tmp_path, monkeypatch)
    _agent(tmp_path, monkeypatch, ALICE_OWNER)
    alice_home = owner_home_paths_by_id(main.home_paths, ALICE)
    assert read_owner_admin_controls(alice_home) == {"decision_model_allowed": True, "audit_allowed": True,
                                                     "cross_owner_audit_allowed": False, "source": "default"}
    policy = alice_home.owner_tool_policy_json
    before = json.loads(policy.read_text())
    saved = set_owner_admin_controls(main.home_paths, alice_home, {"decision_model_allowed": False}, actor="local/main")
    assert saved["decision_model_allowed"] is False and saved["audit_allowed"] is True and saved["source"] == "policy"
    after = json.loads(policy.read_text())
    assert {key: after[key] for key in before} == before
    assert after["admin_controls"]["updated_by"] == "local/main"
    assert stat.S_IMODE(policy.stat().st_mode) == 0o600
    # 坏块或坏文件一律按不允许
    after["admin_controls"]["audit_allowed"] = "yes"
    policy.write_text(json.dumps(after))
    assert read_owner_admin_controls(alice_home)["source"] == "unreadable"
    assert not any(read_owner_admin_controls(alice_home)[key] for key in ("decision_model_allowed", "audit_allowed"))
    policy.write_text("{broken")
    assert read_owner_admin_controls(alice_home)["audit_allowed"] is False
    with pytest.raises(OwnerAdminControlsError) as refused:
        set_owner_admin_controls(main.home_paths, alice_home, {"audit_allowed": True}, actor="local/main")
    assert refused.value.reason == "policy_unreadable"


@pytest.mark.parametrize("case", [
    ("alice", "alice", {"audit_allowed": False}, "not_admin"),
    ("main", "alice", {"cross_owner_audit_allowed": True}, "admin_self_only"),
    ("main", "alice", {"audit_allowed": "false"}, "invalid_changes"),
    ("main", "alice", {"unknown": True}, "invalid_changes"),
    ("main", "alice", {}, "invalid_changes"),
])
def test_owner_control_writes_are_admin_only_and_structured(tmp_path, monkeypatch, case):
    admin_side, target, changes, reason = case
    main = _agent(tmp_path, monkeypatch)
    alice = _agent(tmp_path, monkeypatch, ALICE_OWNER)
    homes = {"main": main.home_paths, "alice": alice.home_paths}
    with pytest.raises(OwnerAdminControlsError) as refused:
        set_owner_admin_controls(homes[admin_side], homes[target], changes, actor="x")
    assert refused.value.reason == reason


def test_cross_owner_flag_only_counts_for_the_admin(tmp_path, monkeypatch):
    main = _agent(tmp_path, monkeypatch)
    alice = _agent(tmp_path, monkeypatch, ALICE_OWNER)
    assert not cross_owner_audit_allowed(main.home_paths)
    set_owner_admin_controls(main.home_paths, main.home_paths, {"cross_owner_audit_allowed": True}, actor="local/main")
    assert cross_owner_audit_allowed(main.home_paths)
    # 用户自己的策略里手写同名值也不生效
    policy = alice.home_paths.owner_tool_policy_json
    data = json.loads(policy.read_text())
    data["admin_controls"] = {"schema": "owner_admin_controls.v1", "cross_owner_audit_allowed": True}
    policy.write_text(json.dumps(data))
    assert not cross_owner_audit_allowed(alice.home_paths)
    assert read_owner_admin_controls(alice.home_paths)["cross_owner_audit_allowed"] is False


@pytest.mark.parametrize("owner_id", ["", "main", "local/other", "providers/feishu/users/../x", "providers/feishu/admins/x",
                                      "providers/fei shu/users/x", "providers/feishu/users/ou-nobody", 7])
def test_owner_id_resolution_rejects_non_canonical_or_missing_owners(tmp_path, monkeypatch, owner_id):
    main = _agent(tmp_path, monkeypatch)
    assert owner_home_paths_by_id(main.home_paths, owner_id) is None


def test_admin_tool_lists_and_sets_and_the_change_takes_effect_immediately(tmp_path, monkeypatch):
    alice = _agent(tmp_path, monkeypatch, ALICE_OWNER)
    main = _agent(tmp_path, monkeypatch)
    outcome, listed = _call(main, "admin_controls", {"action": "list"})
    assert outcome.ok and [row["owner_id"] for row in listed["owners"]] == ["local/main", ALICE]
    assert "cross_owner_audit_allowed" in listed["owners"][0] and "cross_owner_audit_allowed" not in listed["owners"][1]
    outcome, saved = _call(main, "admin_controls", {"action": "set", "owner_id": ALICE,
                                                    "changes": {"decision_model_allowed": False, "audit_allowed": False}})
    assert outcome.ok and saved["owner"]["decision_model_allowed"] is False
    # 用户那边下一次调用即生效：决策设置读取显示被管理员关闭，审计被拒
    read = json.loads(alice.tools.tools["user_config"].execute({"action": "decision_read"}).output)
    assert read["admin_decision_model_allowed"] is False
    denied, body = _call(alice, "audit_records", {"topic": "decision"})
    assert not denied.ok and denied.error_code == "AUDIT_ACCESS_DENIED" and body["reason"] == "audit_disabled_by_admin"
    for params, code in (({"action": "set", "owner_id": ALICE, "changes": {"cross_owner_audit_allowed": True}}, "ADMIN_CONTROL_DENIED"),
                         ({"action": "set", "owner_id": "providers/feishu/users/ou-nobody", "changes": {"audit_allowed": True}},
                          "TOOL_INVALID_ARGUMENTS"),
                         ({"action": "drop"}, "TOOL_INVALID_ARGUMENTS")):
        refused, _ = _call(main, "admin_controls", params)
        assert not refused.ok and refused.error_code == code and refused.effect_outcome == "not_started"
    # 替身绕过注册直接调用也按 home 复核管理员身份
    refused, body = _call(SimpleNamespace(tools=SimpleNamespace(tools={"admin_controls": AdminControlsTool(alice)})),
                          "admin_controls", {"action": "list"})
    assert not refused.ok and body["reason"] == "not_admin"


def test_audit_reads_own_usage_and_observations_within_the_window_only(tmp_path, monkeypatch):
    alice = _agent(tmp_path, monkeypatch, ALICE_OWNER)
    bob = _agent(tmp_path, monkeypatch, ("feishu", "user", "ou-bob"))
    mine = alice.conversation_store.threads.get_or_create({"canonical_user_id": "ou-alice", "owner_id": ALICE})
    other = bob.conversation_store.threads.get_or_create({"canonical_user_id": "ou-bob", "owner_id": "bob"})
    now = time.time()
    _persist(alice, mine.thread_id, _decision_calls(finished=2, timed_out=3), now - 60)
    _persist(alice, mine.thread_id, _decision_calls(finished=1, timed_out=0), now - 3 * 86400)
    _persist(bob, other.thread_id, _decision_calls(finished=4, timed_out=0), now - 60)
    queue = tmp_path / "gateway"
    _request_record(queue, "req-mine", mine.thread_id)
    _request_record(queue, "req-mine-old", mine.thread_id, 72)
    _request_record(queue, "req-bob", other.thread_id)
    _in_gateway_turn(alice, queue, mine.thread_id)

    outcome, report = _call(alice, "audit_records", {"topic": "decision"})
    assert outcome.ok, outcome.output
    [owner] = report["owners"]
    assert owner["owner_id"] == ALICE and owner["admin_controls"] == {"decision_model_allowed": True, "audit_allowed": True}
    usage = owner["usage"]
    assert (usage["calls"], usage["finished"], usage["timed_out"], usage["input_tokens_reported"]) == (5, 2, 3, 10000)
    assert usage["threads"][0]["thread_id"] == mine.thread_id
    assert owner["settings"]["points"]["model_selection"] == "off"
    entries = report["observations"]["entries"]
    assert {entry["request_id"] for entry in entries} == {"req-mine"}
    assert {entry["kind"] for entry in entries} == {"model_selection", "capability_presentation"}
    assert all(entry["owner_id"] == ALICE for entry in entries)
    assert "SECRET" not in outcome.output
    # 当前会话范围与参数校验
    outcome, report = _call(alice, "audit_records", {"topic": "decision", "scope": "current_thread", "since_hours": 100})
    assert outcome.ok and report["owners"][0]["usage"]["calls"] == 6
    for bad in ({"topic": "logs"}, {"topic": "decision", "scope": "everyone"}, {"topic": "decision", "since_hours": 0},
                {"topic": "decision", "limit": 1000}):
        refused, _ = _call(alice, "audit_records", bad)
        assert not refused.ok and refused.error_code == "TOOL_INVALID_ARGUMENTS"


def test_all_owners_scope_needs_admin_and_explicit_cross_owner_permission(tmp_path, monkeypatch):
    alice = _agent(tmp_path, monkeypatch, ALICE_OWNER)
    main = _agent(tmp_path, monkeypatch)
    thread = alice.conversation_store.threads.get_or_create({"canonical_user_id": "ou-alice", "owner_id": ALICE})
    _persist(alice, thread.thread_id, _decision_calls(finished=1, timed_out=1), time.time() - 30)
    queue = tmp_path / "gateway"
    _request_record(queue, "req-alice", thread.thread_id)
    for agent in (alice, main):
        refused, body = _call(agent, "audit_records", {"topic": "decision", "scope": "all_owners"})
        assert not refused.ok and refused.error_code == "AUDIT_ACCESS_DENIED" and body["reason"] == "cross_owner_not_allowed"
    set_owner_admin_controls(main.home_paths, main.home_paths, {"cross_owner_audit_allowed": True}, actor="local/main")
    _in_gateway_turn(main, queue, "")
    outcome, report = _call(main, "audit_records", {"topic": "decision", "scope": "all_owners"})
    assert outcome.ok, outcome.output
    by_owner = {row["owner_id"]: row for row in report["owners"]}
    assert set(by_owner) == {"local/main", ALICE}
    assert by_owner[ALICE]["usage"]["calls"] == 2 and by_owner["local/main"]["usage"]["calls"] == 0
    assert [entry["owner_id"] for entry in report["observations"]["entries"]][:1] == [ALICE]
    # 用户那边即使手写跨用户许可也仍被拒
    refused, _ = _call(alice, "audit_records", {"topic": "decision", "scope": "all_owners"})
    assert not refused.ok


def test_audit_without_gateway_context_reports_observations_unavailable(tmp_path, monkeypatch):
    alice = _agent(tmp_path, monkeypatch, ALICE_OWNER)
    outcome, report = _call(alice, "audit_records", {"topic": "decision"})
    assert outcome.ok and report["observations"] == {"available": False, "reason": "no_gateway_request_context"}
    refused, body = _call(alice, "audit_records", {"topic": "decision", "scope": "current_thread"})
    assert not refused.ok and body["reason"] == "no_trusted_thread"
