"""插件观察候选宿主合同：载荷形状整份接受/拒绝、宿主铸 ID 稳定、模型投影隐去 key、按事件序判定当前观察与候选新鲜度、事件载荷投影。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_runtime_ledger
from agent_py_agent.agent.plugin_observation import (
    OBSERVATION_CANDIDATE_UNKNOWN,
    OBSERVATION_SCHEMA,
    OBSERVATION_STALE,
    ObservationHostContext,
    ObservationRejected,
    current_observation,
    observation_is_current,
    observation_meta,
    parse_observation,
    resolve_action_candidate,
)

ACTIONS = {"click": "plugin__browser-lite_1a2b3c4d__click_5e6f7a8b", "fill": "plugin__browser-lite_1a2b3c4d__fill_9c0d1e2f"}


# 函数用途: 造一份宿主上下文；默认身份来自宿主字段，不取插件自报。
def _context(**overrides) -> ObservationHostContext:
    values = dict(run_id="run-1", task_id="task-1", operation_id="op-1", activation_id="a" * 64, plugin_id="browser-lite",
                  tool_name="plugin__browser-lite_1a2b3c4d__read_00000000", target_kind="page", max_candidates=50, action_tools=ACTIONS)
    values.update(overrides)
    return ObservationHostContext(**values)


# 函数用途: 造一份合规的插件观察载荷。
def _payload(**overrides) -> dict:
    payload = {"schema": OBSERVATION_SCHEMA, "target": {"ref": "tab-3", "generation": "17"},
               "candidates": [{"key": "e5", "role": "button", "label": "提交订单", "actions": ["click"]},
                              {"key": "e9", "role": "textbox", "label": "收货人", "actions": ["fill", "click"]}]}
    payload.update(overrides)
    return payload


def test_parse_mints_stable_host_ids_and_maps_actions_to_registry_names():
    record = parse_observation(_payload(), _context())
    again = parse_observation(json.loads(json.dumps(_payload())), _context())
    assert record == again, "同一宿主身份与载荷铸出同一 ID"
    assert record.observation_id.startswith("obs-") and len(record.observation_id) == 28
    assert [c.candidate_id[:5] for c in record.candidates] == ["cand-", "cand-"] and len({c.candidate_id for c in record.candidates}) == 2
    assert record.candidates[1].actions == (ACTIONS["fill"], ACTIONS["click"]), "actions 换成宿主注册名并保序"
    assert record.target_ref_hash != "tab-3" and record.target_ref == "tab-3" and record.generation == "17"
    assert parse_observation(_payload(), _context(operation_id="op-2")).observation_id != record.observation_id, "另一次调用是另一个观察"
    projection = record.model_projection()
    assert set(projection) == {"observation_id", "candidates"} and set(projection["candidates"][0]) == {"candidate_id", "role", "label", "actions"}
    assert "key" not in json.dumps(projection) and "tab-3" not in json.dumps(projection), "模型看不到插件 key、目标引用与代次"
    envelope = record.to_envelope()
    assert envelope["schema"] == OBSERVATION_SCHEMA and envelope["candidates"][0]["key"] == "e5"
    assert envelope["target_ref"] == "tab-3" and envelope["generation"] == "17", "归档信封保留插件目标引用与代次，动作时交还插件复核"


@pytest.mark.parametrize("change, code", [
    (lambda p: p.update(schema="plugin_observation.v0"), "schema"),
    (lambda p: p.update(extra=1), "unknown_field"),
    (lambda p: p.update(target={"ref": "tab-3"}), "target"),
    (lambda p: p["target"].update(ref="x" * 129), "target_ref"),
    (lambda p: p["target"].update(generation=""), "target_generation"),
    (lambda p: p.update(candidates=[]), "candidate_count"),
    (lambda p: p.update(candidates=p["candidates"] * 26), "candidate_count"),
    (lambda p: p["candidates"][1].update(key="e5"), "duplicate_key"),
    (lambda p: p["candidates"][0].pop("role"), "candidate_shape"),
    (lambda p: p["candidates"][0].update(key="bad key"), "candidate_key"),
    (lambda p: p["candidates"][0].update(label="x" * 121), "candidate_label"),
    (lambda p: p["candidates"][0].update(actions=[]), "candidate_actions"),
    (lambda p: p["candidates"][0].update(actions=["click", "click"]), "candidate_actions"),
    (lambda p: p["candidates"][0].update(actions=["open"]), "action_not_declared"),
])
def test_malformed_payload_is_rejected_as_a_whole_with_a_structured_code(change, code):
    payload = _payload()
    change(payload)
    with pytest.raises(ObservationRejected) as caught:
        parse_observation(payload, _context())
    assert caught.value.code == code


def test_declared_max_candidates_is_clamped_by_the_host_limit():
    many = _payload(candidates=[{"key": f"e{i}", "role": "button", "label": f"按钮 {i}", "actions": ["click"]} for i in range(3)])
    with pytest.raises(ObservationRejected) as caught:
        parse_observation(many, _context(max_candidates=2))
    assert caught.value.code == "candidate_count"
    assert len(parse_observation(many, _context(max_candidates=999)).candidates) == 3, "声明超过宿主上限时按 64 夹住，3 个仍合规"


# 类用途: 假的 owner 权威库：按 agent_run 保存 tool_completed 事件，append 时自增 seq。
class _Repo:
    def __init__(self):
        self.events, self.seq = [], 0

    def agent_run_for_run_id(self, run_id):
        return {"agent_run_id": "agentrun-" + run_id, "task_run_id": ""} if run_id == "run-1" else None

    def events_for_agent_run(self, agent_run_id, *, event_type="", limit=2000):
        return [e for e in self.events if e["agent_run_id"] == agent_run_id and (not event_type or e["event_type"] == event_type)]

    def append_event(self, *, event_type, attempt_id, agent_run_id, task_run_id="", payload=None):
        self.seq += 1
        self.events.append({"seq": self.seq, "event_type": event_type, "attempt_id": attempt_id, "agent_run_id": agent_run_id,
                            "task_run_id": task_run_id, "payload": dict(payload or {})})
        return self.events[-1]


# 函数用途: 经产品的 tool_completed 事件写法把一次观察归档记进假库，返回记录。
def _record_observation(repo, payload, *, operation_id, attempt_id="attempt-1", ok=True):
    record = parse_observation(payload, _context(operation_id=operation_id))
    archive = {"run_id": "run-1", "task_id": "task-1", "operation_id": operation_id, "attempt_id": attempt_id, "tool": record.tool_name,
               "ok": ok, "error_code": "", "idempotency_key": "", "runtime_gate": {"allowed": True},
               "tool_result_envelope": {"observation": record.to_envelope()}}
    tool_runtime_ledger._append_runtime_event(SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo)), archive)
    return record


def test_latest_successful_observation_per_target_is_current_and_older_ones_are_stale():
    repo = _Repo()
    first = _record_observation(repo, _payload(), operation_id="op-1")
    second = _record_observation(repo, _payload(target={"ref": "tab-3", "generation": "18"}), operation_id="op-2", attempt_id="attempt-2")
    other = _record_observation(repo, _payload(target={"ref": "tab-9", "generation": "1"}), operation_id="op-3")
    stored = repo.events[0]["payload"]["observation"]
    assert set(stored) == {"observation_id", "activation_id", "target_kind", "target_ref", "target_ref_hash", "generation", "content_hash",
                          "task_id", "operation_id", "candidates"} and "label" not in json.dumps(stored), "事件载荷是查找投影，不带 label/role"
    assert observation_is_current(repo, run_id="run-1", task_id="task-1", observation_id=second.observation_id) is True
    assert observation_is_current(repo, run_id="run-1", task_id="task-1", observation_id=first.observation_id) is False, "同目标更新后旧观察 stale（跨 attempt 共享）"
    assert observation_is_current(repo, run_id="run-1", task_id="task-1", observation_id=other.observation_id) is True, "另一目标各自判定"
    current = current_observation(repo, run_id="run-1", task_id="task-1", activation_id="a" * 64, target_ref_hash=first.target_ref_hash)
    assert current is not None and current["observation_id"] == second.observation_id and current["generation"] == "18"
    assert observation_is_current(repo, run_id="run-1", task_id="other-task", observation_id=second.observation_id) is False, "task 不匹配不算"
    assert observation_is_current(repo, run_id="run-2", task_id="task-1", observation_id=second.observation_id) is False, "没有权威 AgentRun 行按不新鲜"
    assert observation_is_current(None, run_id="run-1", task_id="task-1", observation_id=second.observation_id) is False


def test_failed_calls_and_malformed_ids_never_count_as_observations():
    repo = _Repo()
    good = _record_observation(repo, _payload(), operation_id="op-1")
    _record_observation(repo, _payload(target={"ref": "tab-3", "generation": "19"}), operation_id="op-2", ok=False)
    assert observation_is_current(repo, run_id="run-1", task_id="task-1", observation_id=good.observation_id) is True, "失败调用带的观察不算"
    assert observation_is_current(repo, run_id="run-1", task_id="task-1", observation_id="obs-not-hex") is False


def test_resolve_action_candidate_checks_freshness_and_declared_actions():
    repo = _Repo()
    record = _record_observation(repo, _payload(), operation_id="op-1")
    button, textbox = record.candidates
    observation, candidate = resolve_action_candidate(repo, run_id="run-1", task_id="task-1", candidate_id=button.candidate_id, action_tool=ACTIONS["click"])
    assert candidate["key"] == "e5" and observation["generation"] == "17"
    meta = observation_meta(observation, candidate)
    assert meta == {"version": "1", "observation_id": record.observation_id, "key": "e5", "target": {"ref": "tab-3", "generation": "17"}}
    with pytest.raises(ObservationRejected) as unknown:
        resolve_action_candidate(repo, run_id="run-1", task_id="task-1", candidate_id=button.candidate_id, action_tool=ACTIONS["fill"])
    assert unknown.value.code == OBSERVATION_CANDIDATE_UNKNOWN, "该候选没有声明 fill"
    with pytest.raises(ObservationRejected) as missing:
        resolve_action_candidate(repo, run_id="run-1", task_id="task-1", candidate_id="cand-0000000000000000", action_tool=ACTIONS["click"])
    assert missing.value.code == OBSERVATION_CANDIDATE_UNKNOWN
    with pytest.raises(ObservationRejected) as shape:
        resolve_action_candidate(repo, run_id="run-1", task_id="task-1", candidate_id="e5", action_tool=ACTIONS["click"])
    assert shape.value.code == OBSERVATION_CANDIDATE_UNKNOWN, "插件 key 不能当候选 ID"
    _record_observation(repo, _payload(target={"ref": "tab-3", "generation": "18"}), operation_id="op-2")
    with pytest.raises(ObservationRejected) as stale:
        resolve_action_candidate(repo, run_id="run-1", task_id="task-1", candidate_id=textbox.candidate_id, action_tool=ACTIONS["fill"])
    assert stale.value.code == OBSERVATION_STALE, "新观察一到旧候选就过期"
