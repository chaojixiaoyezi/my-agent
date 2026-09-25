# LLM: 插件观察候选的唯一宿主实现：校验只读插件工具结果里的 my_agent_observation 载荷形状（整份接受或整份拒绝，记结构化原因码），
#   用宿主上下文铸 observation_id / candidate_id，生成写进该次调用原归档 tool_result_envelope.observation 的记录与模型可见的
#   有界投影，并按 owner 权威库 runtime_events 里 tool_completed 事件的 seq 顺序判定"当前观察"和候选新鲜度。role/label 只是
#   external_data，不参与任何机器判断；不解析自由文本；不另建观察账本。改动须同步 plugin_runtime.PluginProxyTool、
#   agent_core/tool_runtime_ledger._append_runtime_event、runtime_db.repository.events_for_agent_run 与决策线的 action_candidate 点。
# 模块用途: 让"这次观察看到了哪些可操作对象"成为结构化事实，供模型填候选 ID、宿主发送前复核新鲜度、决策点只在 ID 里选。
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# 插件在成功结果 structuredContent 里放候选的保留键，以及动作工具拒绝候选时的保留键
OBSERVATION_KEY = "my_agent_observation"
OBSERVATION_ERROR_KEY = "my_agent_observation_error"
OBSERVATION_SCHEMA = "plugin_observation.v1"
# 动作调用时宿主附给插件的 _meta 扩展键与版本
OBSERVATION_META_EXTENSION = "my-agent/observation"
OBSERVATION_META_VERSION = "1"
# 宿主发送前复核的结构化拒绝码
OBSERVATION_STALE = "OBSERVATION_STALE"
OBSERVATION_CANDIDATE_UNKNOWN = "OBSERVATION_CANDIDATE_UNKNOWN"
MAX_CANDIDATES = 64
MAX_ACTIONS = 8
MAX_LABEL_CHARS = 120
_OBSERVATION_ID = re.compile(r"obs-[0-9a-f]{24}\Z")
_CANDIDATE_ID = re.compile(r"cand-[0-9a-f]{16}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")     # key / role：短标识，不含空白
_TARGET_TEXT = re.compile(r"[^\x00-\x1f\x7f]{1,128}\Z")           # target.ref ≤128、generation ≤64 由长度另限


# LLM: reason 是稳定机器码（observation_rejected:<code> 里的 code 部分）；错误正文不含载荷内容。
# 类用途: 观察载荷形状不合规时整份拒绝的原因。
class ObservationRejected(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(f"插件观察载荷不合规：{code}")
        self.code = code


# LLM: 身份全部来自宿主（run/task/operation/激活/注册名），不取插件自报；actions 映射是本包内"插件工具名 → 宿主注册名"，
#   只含与本观察同 target_kind 的 observation_ref 工具。
# 类用途: 铸造观察 ID 与校验候选 actions 所需的宿主上下文。
@dataclass(frozen=True)
class ObservationHostContext:
    run_id: str
    task_id: str
    operation_id: str
    activation_id: str
    plugin_id: str
    tool_name: str
    target_kind: str
    max_candidates: int
    action_tools: Mapping[str, str]


# 类用途: 一个宿主铸过 ID 的候选；key 是插件自己能解析回对象的键，宿主不解释它。
@dataclass(frozen=True)
class ObservationCandidate:
    candidate_id: str
    key: str
    role: str
    label: str
    actions: tuple[str, ...]


# LLM: to_envelope 是归档里的唯一权威形状（含插件自己的 target_ref 与代次，动作时要原样交还插件复核）；model_projection 隐去
#   key / target_ref / 代次；event_payload 是写进 runtime_events 的查找投影（字段与信封一致，不含 label/role）。
# 类用途: 一次合规观察的完整宿主记录。
@dataclass(frozen=True)
class ObservationRecord:
    observation_id: str
    plugin_id: str
    activation_id: str
    tool_name: str
    target_kind: str
    target_ref: str
    target_ref_hash: str
    generation: str
    content_hash: str
    candidates: tuple[ObservationCandidate, ...]
    run_id: str = ""
    task_id: str = ""
    operation_id: str = ""

    # 函数用途: 归档 tool_result_envelope.observation 的写法。
    def to_envelope(self) -> dict[str, Any]:
        return {
            "schema": OBSERVATION_SCHEMA, "observation_id": self.observation_id, "plugin_id": self.plugin_id,
            "activation_id": self.activation_id, "tool": self.tool_name, "target_kind": self.target_kind,
            "target_ref": self.target_ref, "target_ref_hash": self.target_ref_hash, "generation": self.generation,
            "content_hash": self.content_hash,
            "candidates": [{"candidate_id": c.candidate_id, "key": c.key, "role": c.role, "label": c.label,
                            "actions": list(c.actions)} for c in self.candidates],
        }

    # 函数用途: 模型可见结果里替换 my_agent_observation 的有界投影：只给宿主铸的 ID、role、label 与动作工具名。
    def model_projection(self) -> dict[str, Any]:
        return {"observation_id": self.observation_id,
                "candidates": [{"candidate_id": c.candidate_id, "role": c.role, "label": c.label, "actions": list(c.actions)}
                               for c in self.candidates]}

    # 函数用途: 写进 runtime_events tool_completed 载荷的查找投影（不含 label/role）。
    def event_payload(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id, "activation_id": self.activation_id, "target_kind": self.target_kind,
            "target_ref": self.target_ref, "target_ref_hash": self.target_ref_hash, "generation": self.generation,
            "content_hash": self.content_hash, "task_id": self.task_id, "operation_id": self.operation_id,
            "candidates": [{"candidate_id": c.candidate_id, "key": c.key, "actions": list(c.actions)} for c in self.candidates],
        }


# 函数用途: 规范 JSON 后取 sha256 十六进制。
def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# 函数用途: 校验一段短标识（key / role）。
def _token(value: object, code: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ObservationRejected(code)
    return value


# LLM: 形状不合规就整份拒绝：schema、target.ref/generation、候选数量（1..min(声明, 宿主上限)）、key 重复、label 超长、
#   actions 越界（必须是本包同 target_kind 的 observation_ref 工具名，1..8 个，不重复）都算不合规。不部分采纳。
# 函数用途: 把插件载荷校验成宿主观察记录，并铸 observation_id / candidate_id。
def parse_observation(payload: object, context: ObservationHostContext) -> ObservationRecord:
    if not isinstance(payload, dict) or payload.get("schema") != OBSERVATION_SCHEMA:
        raise ObservationRejected("schema")
    if set(payload) - {"schema", "target", "candidates"}:
        raise ObservationRejected("unknown_field")
    target = payload.get("target")
    if not isinstance(target, dict) or set(target) != {"ref", "generation"}:
        raise ObservationRejected("target")
    ref, generation = target.get("ref"), target.get("generation")
    if not isinstance(ref, str) or not _TARGET_TEXT.fullmatch(ref):
        raise ObservationRejected("target_ref")
    if not isinstance(generation, str) or not 1 <= len(generation) <= 64 or not _TARGET_TEXT.fullmatch(generation):
        raise ObservationRejected("target_generation")
    rows = payload.get("candidates")
    limit = max(1, min(int(context.max_candidates), MAX_CANDIDATES))
    if not isinstance(rows, list) or not 1 <= len(rows) <= limit:
        raise ObservationRejected("candidate_count")
    canonical = tuple(_canonical_candidate(row, context) for row in rows)
    if len({row["key"] for row in canonical}) != len(canonical):
        raise ObservationRejected("duplicate_key")
    identity = [context.run_id, context.task_id, context.operation_id, context.activation_id, context.tool_name, ref, generation, list(canonical)]
    observation_id = "obs-" + _digest(identity)[:24]
    candidates = tuple(
        ObservationCandidate("cand-" + _digest([observation_id, row["key"]])[:16], row["key"], row["role"], row["label"], tuple(row["actions"]))
        for row in canonical
    )
    return ObservationRecord(
        observation_id=observation_id, plugin_id=context.plugin_id, activation_id=context.activation_id,
        tool_name=context.tool_name, target_kind=context.target_kind, target_ref=ref, target_ref_hash=_digest(ref)[:24],
        generation=generation, content_hash=_digest(list(canonical))[:24], candidates=candidates,
        run_id=context.run_id, task_id=context.task_id, operation_id=context.operation_id,
    )


# 函数用途: 校验一个候选项并把 actions 换成宿主注册名（顺序保留、去重）。
def _canonical_candidate(row: object, context: ObservationHostContext) -> dict[str, Any]:
    if not isinstance(row, dict) or set(row) != {"key", "role", "label", "actions"}:
        raise ObservationRejected("candidate_shape")
    key = _token(row["key"], "candidate_key")
    role = _token(row["role"], "candidate_role")
    label = row["label"]
    if not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL_CHARS or any(ord(ch) < 32 for ch in label):
        raise ObservationRejected("candidate_label")
    actions = row["actions"]
    if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS or len(set(actions)) != len(actions):
        raise ObservationRejected("candidate_actions")
    mapped = []
    for action in actions:
        if not isinstance(action, str) or action not in context.action_tools:
            raise ObservationRejected("action_not_declared")
        mapped.append(context.action_tools[action])
    return {"key": key, "role": role, "label": label, "actions": mapped}


# LLM: 只读 runtime_events：先按既有 run_id 找权威 AgentRun，再取该 agent_run 全部 tool_completed 事件（跨 attempt 共享），
#   只保留 ok 且带 observation 的事件；任何库错误按"没有观察"处理，不抛给调用方。
# 函数用途: 列出一个 run 里按发生顺序排列的观察事件载荷。
def _observation_events(repo: object, *, run_id: str, task_id: str) -> list[dict[str, Any]]:
    if repo is None or not run_id:
        return []
    try:
        agent_run = repo.agent_run_for_run_id(run_id)
        if agent_run is None:
            return []
        events = repo.events_for_agent_run(str(agent_run["agent_run_id"]), event_type="tool_completed")
    except (sqlite3.Error, OSError, AttributeError, KeyError, TypeError):
        return []
    rows = []
    for event in events:
        payload = event.get("payload") if isinstance(event, dict) else None
        observation = payload.get("observation") if isinstance(payload, dict) else None
        if not isinstance(observation, dict) or payload.get("ok") is not True:
            continue
        if task_id and str(observation.get("task_id") or "") != task_id:
            continue
        rows.append(observation)
    return rows


# LLM: 同一 run/task、同一 activation_id 与 target_ref_hash 下最新一次成功观察为 current，更早的都 stale；顺序取事件 seq。
# 函数用途: 取当前观察的事件载荷，没有则 None。
def current_observation(repo: object, *, run_id: str, task_id: str, activation_id: str, target_ref_hash: str) -> dict[str, Any] | None:
    current = None
    for observation in _observation_events(repo, run_id=run_id, task_id=task_id):
        if observation.get("activation_id") == activation_id and observation.get("target_ref_hash") == target_ref_hash:
            current = observation
    return current


# LLM: True 只在该 observation_id 存在且仍是其 (activation_id, target_ref_hash) 下最新一次成功观察时成立；查不到或已被更新的观察
#   取代都是 False，不猜。
# 函数用途: 决策点与发送前复核共用的新鲜度判定。
def observation_is_current(repo: object, *, run_id: str, task_id: str, observation_id: str) -> bool:
    if not isinstance(observation_id, str) or not _OBSERVATION_ID.fullmatch(observation_id):
        return False
    events = _observation_events(repo, run_id=run_id, task_id=task_id)
    match = next((row for row in events if row.get("observation_id") == observation_id), None)
    if match is None:
        return False
    latest = current_observation(repo, run_id=run_id, task_id=task_id, activation_id=str(match.get("activation_id") or ""),
                                 target_ref_hash=str(match.get("target_ref_hash") or ""))
    return latest is not None and latest.get("observation_id") == observation_id


# LLM: 发送前复核：候选 ID 形状、在当前 run/task 的观察事件里存在、所属观察仍 current、候选 actions 含本动作工具（注册名）。
#   任一不成立抛 ObservationRejected(OBSERVATION_STALE | OBSERVATION_CANDIDATE_UNKNOWN)；通过返回 (观察载荷, 候选)。
# 函数用途: 把模型填的 candidate_id 换回插件的 key 与目标代次，供 _meta 回填。
def resolve_action_candidate(repo: object, *, run_id: str, task_id: str, candidate_id: str, action_tool: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(candidate_id, str) or not _CANDIDATE_ID.fullmatch(candidate_id):
        raise ObservationRejected(OBSERVATION_CANDIDATE_UNKNOWN)
    for observation in reversed(_observation_events(repo, run_id=run_id, task_id=task_id)):
        candidate = next((c for c in observation.get("candidates", ()) if isinstance(c, dict) and c.get("candidate_id") == candidate_id), None)
        if candidate is None:
            continue
        if not observation_is_current(repo, run_id=run_id, task_id=task_id, observation_id=str(observation.get("observation_id") or "")):
            raise ObservationRejected(OBSERVATION_STALE)
        if action_tool not in (candidate.get("actions") or ()):
            raise ObservationRejected(OBSERVATION_CANDIDATE_UNKNOWN)
        return observation, candidate
    raise ObservationRejected(OBSERVATION_CANDIDATE_UNKNOWN)


# 函数用途: 动作调用时附给插件的 _meta 载荷：观察 ID、插件自己的 key 与目标引用/代次，让插件按代次复核后再执行。
def observation_meta(observation: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {"version": OBSERVATION_META_VERSION, "observation_id": observation.get("observation_id"), "key": candidate.get("key"),
            "target": {"ref": observation.get("target_ref"), "generation": observation.get("generation")}}


# LLM: 只读归档信封里宿主写过的 observation，形状不对返回 None；供 tool_completed 事件写载荷时复用同一投影。
# 函数用途: 从归档 tool_result_envelope.observation 还原事件载荷投影。
def observation_event_payload_from_envelope(envelope: object, *, task_id: str, operation_id: str) -> dict[str, Any] | None:
    if not isinstance(envelope, dict) or envelope.get("schema") != OBSERVATION_SCHEMA:
        return None
    candidates = envelope.get("candidates")
    if not isinstance(candidates, list):
        return None
    return {
        "observation_id": envelope.get("observation_id"), "activation_id": envelope.get("activation_id"),
        "target_kind": envelope.get("target_kind"), "target_ref": envelope.get("target_ref", ""),
        "target_ref_hash": envelope.get("target_ref_hash"), "generation": envelope.get("generation"),
        "content_hash": envelope.get("content_hash"), "task_id": task_id, "operation_id": operation_id,
        "candidates": [{"candidate_id": c.get("candidate_id"), "key": c.get("key"), "actions": list(c.get("actions") or [])}
                       for c in candidates if isinstance(c, dict)],
    }


__all__ = [
    "MAX_ACTIONS", "MAX_CANDIDATES", "MAX_LABEL_CHARS", "OBSERVATION_CANDIDATE_UNKNOWN", "OBSERVATION_ERROR_KEY",
    "OBSERVATION_KEY", "OBSERVATION_META_EXTENSION", "OBSERVATION_META_VERSION", "OBSERVATION_SCHEMA", "OBSERVATION_STALE",
    "ObservationCandidate", "ObservationHostContext", "ObservationRecord", "ObservationRejected", "current_observation",
    "observation_event_payload_from_envelope", "observation_is_current", "observation_meta", "parse_observation",
    "resolve_action_candidate",
]
