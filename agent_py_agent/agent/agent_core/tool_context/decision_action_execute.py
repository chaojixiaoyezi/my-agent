# LLM: action_candidate 点的自动执行规划与记账（设计稿第 7 节，片 D）。只做"要不要替主模型点这一下"的结构化判定：能力开关
#   action_candidate_auto_execute_enabled 为 True（capability_config_for_agent，只认 True）、宿主是本机管理员主代理（本轮快照
#   owner_type=main_agent 且 home 身份结构化 local/main，两者都查）、所选候选恰有一个"可自动执行"的动作（本轮快照里有该工具、
#   处理器带 observation_ref 绑定、schema 的 required ⊆ {observation_ref.param}；0 个不执行，≥2 个算 ambiguous，不按名字挑）、
#   且本 run 里还没有任何动作碰过这个观察（runtime_events 里的 observation_action 事实，宿主执行与模型执行共用）、且本 run 里宿主自动执行
#   的次数还没到 AUTO_EXECUTIONS_PER_RUN_MAX_COUNT（ae 定：同一 run 最多一次，不按目标分；重新观察后观察编号/候选编号都会换，按观察编号
#   幂等挡不住重复提交；模型自己的动作不计入上限）。满足就把一次宿主
#   ToolCall（operation_id = action_candidate:auto:<observation_id>，幂等键由它派生）计划进 params.host_actions；执行、审批、记录
#   由 tool_loop/round_execution 走模型调用同一条链。不执行输入类动作、同一观察只计划一次、复核拒绝/失败/用户拒绝/取消都不重试、
#   不改选别的候选。决策账补充行 record_kind=auto_execution 与工具账共用 decision_ref。改动须同步 test_decision_action_execute.py。
# 模块用途: 决定宿主要不要按 Jev 的选择自动执行一次点击类动作，并把执行与否记进决策账；不碰工具执行链本身。
from __future__ import annotations

import math
from dataclasses import dataclass
from types import SimpleNamespace

from ...capability.runtime_config_reload import capability_config_for_agent
from ...conversation.decision_outcome_log import append_decision_outcome, decision_outcome_row
from ...plugin_observation import observation_actions
from ...tooling.runtime_contracts import ToolCall, ToolResult
from ...user_space.owner_access import is_complete_local_admin_owner

# 决策发起的调用在工具账与审批 binding 里的 actor 值；模型发起的记录是 "model"
AUTO_ACTOR = "decision"
# 决策账补充行 auto_execution.reason 的封闭原因码（没执行时记其一）
SKIP_OWNER_SCOPE = "owner_scope"
SKIP_NO_AUTO_ACTION = "no_auto_action"
SKIP_AMBIGUOUS_ACTION = "ambiguous_action"
SKIP_ALREADY_ACTED = "already_acted"
SKIP_RUN_LIMIT = "run_limit_reached"
SKIP_INTERRUPTED = "interrupted"
AUTO_RECORD_KIND = "auto_execution"
_OPERATION_PREFIX = "action_candidate:auto:"
_CALL_ID_PREFIX = "host-action-"
# decision_ref 里取响应输入摘要的前缀长度，够区分同一操作下的不同响应
_DIGEST_PREFIX_CHARS = 16
# 同一 run 里宿主自动执行的硬上限（ae 定，不做配置项）：重新观察后 Jev 可能再选同一个按钮，按观察编号的幂等挡不住重复提交
AUTO_EXECUTIONS_PER_RUN_MAX_COUNT = 1


# LLM: 一次已采用的决策选择的结构化事实：阶段（operation_id/run/task）、结果（含响应摘要）、宿主校验过的观察与所选候选。
#   decision_ref 由阶段操作编号与响应输入摘要组成，工具账与决策账都写它，不含正文。
# 类用途: 把"Jev 选了谁"固定成一份不可变记录，供规划、执行记账共用。
@dataclass(frozen=True)
class AutoExecutionSelection:
    point: str
    stage: object
    outcome: object
    observation: dict
    candidate: dict

    # 函数用途: 决策结果编号：<阶段操作编号>#<响应输入摘要前缀>。
    @property
    def decision_ref(self) -> str:
        digest = str(getattr(getattr(self.outcome, "response", None), "input_digest", "") or "")
        return f"{getattr(self.stage, 'operation_id', '')}#{digest[:_DIGEST_PREFIX_CHARS]}"


# LLM: 计划好的宿主调用：call 是完整 ToolCall（宿主铸的 call_id/operation_id），actor 固定 decision；round_execution 按它执行、
#   审批、记录，然后调 record_auto_execution 写决策账。
# 类用途: params.host_actions 里的一条待执行宿主动作。
@dataclass(frozen=True)
class HostAction:
    call: ToolCall
    selection: AutoExecutionSelection
    actor: str = AUTO_ACTOR

    # 函数用途: 透出所属决策的结果编号。
    @property
    def decision_ref(self) -> str:
        return self.selection.decision_ref


# 函数用途: 读能力开关；只认 True，缺配置或读不到都算关。
def auto_execution_enabled(agent: object) -> bool:
    return getattr(capability_config_for_agent(agent), "action_candidate_auto_execute_enabled", False) is True


# LLM: 开关关着直接返回 None、不记账（功能没开）；开着时按 属主 → 可执行动作 → 幂等 的顺序查，任一不满足写一行 skipped 补充行并返回
#   None；通过时把 HostAction 追加进 params.host_actions（有副作用：改本轮参数列表）并返回它。不执行工具、不发请求。
# 函数用途: 在建议已采用后，决定要不要计划一次宿主自动执行。
def plan_auto_execution(agent: object, record: object, selection: AutoExecutionSelection) -> HostAction | None:
    if not auto_execution_enabled(agent):
        return None
    params = record.params
    actions = getattr(params, "host_actions", None)
    snapshot = getattr(params, "tool_runtime_snapshot", None)
    reason = _scope_reason(agent, snapshot)
    runtime, param, reason = (None, "", reason) if reason else _auto_action(snapshot, selection.candidate)
    reason = reason or _history_reason(agent, record, selection)
    if not reason and not isinstance(actions, list):
        reason = SKIP_NO_AUTO_ACTION
    if reason:
        record_auto_skip(agent, selection, reason)
        return None
    action = HostAction(_host_call(record.call, runtime, param, selection), selection)
    actions.append(action)
    return action


# LLM: 属主范围两道都查：本轮工具快照的 owner_type 必须是 main_agent，且 home 身份必须是字段齐全的本机 local/main
#   （is_complete_local_admin_owner；缺字段不算管理员）。不以"工具不存在"代替判定。
# 函数用途: 非本机管理员主代理一律不自动执行。
def _scope_reason(agent: object, snapshot: object) -> str:
    if str(getattr(snapshot, "owner_type", "") or "") != "main_agent":
        return SKIP_OWNER_SCOPE
    if not is_complete_local_admin_owner(getattr(agent, "home_paths", None)):
        return SKIP_OWNER_SCOPE
    return ""


# LLM: 可自动执行 = 本轮快照里的工具（snapshot.runtime(name) 非 None 即可用）、处理器公开属性 observation_binding 带 observation_ref、
#   且 schema required ⊆ {observation_ref.param}（只需候选编号，不需要文字/选择器）。恰好一个才执行；没有记 no_auto_action，
#   两个以上记 ambiguous_action，不按工具名排优先级。
# 函数用途: 从所选候选的动作里找出唯一能只凭候选编号执行的工具，返回 (runtime, 参数名, 原因码)。
def _auto_action(snapshot: object, candidate: dict) -> tuple[object, str, str]:
    lookup = getattr(snapshot, "runtime", None)
    qualifying = []
    for name in candidate.get("actions", ()):
        runtime = lookup(name) if callable(lookup) else None
        param = _candidate_only_param(runtime)
        if param:
            qualifying.append((runtime, param))
    if len(qualifying) == 1:
        return (*qualifying[0], "")
    return None, "", SKIP_NO_AUTO_ACTION if not qualifying else SKIP_AMBIGUOUS_ACTION


# 函数用途: 工具只凭候选编号就能调用时返回该参数名，否则空串。
def _candidate_only_param(runtime: object) -> str:
    ref = getattr(getattr(getattr(runtime, "handler", None), "observation_binding", None), "observation_ref", None)
    param = str(getattr(ref, "param", "") or "")
    if not param:
        return ""
    schema = getattr(getattr(runtime, "model_spec", None), "input_schema", None)
    required = schema.get("required") if isinstance(schema, dict) else None
    required_names = {str(item) for item in required} if isinstance(required, list) else set()
    return param if required_names <= {param} else ""


# LLM: 历史事实只读 owner 权威库 runtime_events 里当前 run/task 的 observation_action（宿主执行与模型执行共用），不读正文：
#   同一观察已有任何动作（含失败/被提供方拒绝的发送）就不再执行（already_acted）；本 run 里 actor=decision 的动作数已到
#   AUTO_EXECUTIONS_PER_RUN_MAX_COUNT 也不再执行（run_limit_reached），模型自己的动作不计。没有权威库按"没有动作"处理（新鲜度门已先要求有库）。
# 函数用途: 这个观察已被碰过、或本 run 的自动执行已到上限时给出原因码。
def _history_reason(agent: object, record: object, selection: AutoExecutionSelection) -> str:
    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    observation_id = selection.observation["observation_id"]
    acted = observation_actions(repo, run_id=record.call.run_id, task_id=str(getattr(record.params, "task_id", "") or ""))
    if any(item.get("observation_id") == observation_id for item in acted):
        return SKIP_ALREADY_ACTED
    if sum(1 for item in acted if item.get("actor") == AUTO_ACTOR) >= AUTO_EXECUTIONS_PER_RUN_MAX_COUNT:
        return SKIP_RUN_LIMIT
    return ""


# LLM: 只读本 run 内存里的归档列表（params.archive_tool_calls）：actor=decision 且信封带 observation_action 的记录，按观察编号/候选编号
#   回到同一列表里那次观察的归档取 role/label/region（归档是候选内容的唯一权威），再算粗位置；结果只有 ok 或 failed:<错误码>。
#   给 Jev 材料当外部数据（软约束，不替代宿主侧的上限）；不读权威库、不读正文。
# 函数用途: 列出本 run 宿主已自动执行过的动作（role、label、粗位置、结果），没有就空列表。
def host_executed_actions(params: object) -> list[dict]:
    archives = [item for item in (getattr(params, "archive_tool_calls", None) or ()) if isinstance(item, dict)]
    observations = {}
    for item in archives:
        observation = (item.get("tool_result_envelope") or {}).get("observation")
        if isinstance(observation, dict) and isinstance(observation.get("observation_id"), str):
            observations[observation["observation_id"]] = observation
    rows = []
    for item in archives:
        action = (item.get("tool_result_envelope") or {}).get("observation_action")
        if item.get("actor") != AUTO_ACTOR or not isinstance(action, dict):
            continue
        rows.append(_executed_row(observations.get(str(action.get("observation_id") or "")), action, item))
    return rows


# 函数用途: 一条已执行动作的外部数据投影：候选 role/label/粗位置 + 结果（ok / failed:<错误码>）；候选找不到时只有结果。
def _executed_row(observation: dict | None, action: dict, archive: dict) -> dict:
    candidates = (observation or {}).get("candidates") or []
    candidate = next((c for c in candidates if isinstance(c, dict) and c.get("candidate_id") == action.get("candidate_id")), None)
    row: dict = {"result": "ok" if archive.get("ok") is True else f"failed:{archive.get('error_code') or ''}"}
    if candidate is not None:
        row.update({"role": candidate.get("role"), "label": candidate.get("label")})
        position = coarse_position((observation or {}).get("frame"), candidate.get("region"))
        if position is not None:
            row["position"] = position
    return row


# LLM: 宿主调用身份：call_id/operation_id 都从观察编号铸，幂等键由 ToolCall 按 (run, operation) 派生；run/turn/attempt/协议沿原观察
#   调用；参数只有候选编号；schema_hash 取本轮快照里该工具的真实值。
# 函数用途: 构造要执行的宿主 ToolCall。
def _host_call(origin: ToolCall, runtime: object, param: str, selection: AutoExecutionSelection) -> ToolCall:
    observation_id = selection.observation["observation_id"]
    return ToolCall(
        call_id=_CALL_ID_PREFIX + observation_id, tool_name=runtime.model_spec.name,
        arguments={param: selection.candidate["candidate_id"]}, source_protocol=origin.source_protocol,
        schema_hash=runtime.model_spec.schema_hash, run_id=origin.run_id, turn_id=origin.turn_id,
        attempt_id=origin.attempt_id, operation_id=_OPERATION_PREFIX + observation_id,
    )


# LLM: 决策账补充行：复用主行投影（record_kind=auto_execution 标识），result_category 改成 auto_execution:<executed|skipped:码>，
#   不重复记供应商模型身份；auto_execution 里是执行的结构化事实（operation_id/ok/error_code/effect_outcome 等）或跳过原因码。
# 函数用途: 把一次自动执行的结果写进决策账（写文件副作用）。
def record_auto_execution(agent: object, action: HostAction, result: ToolResult | None, *, reason: str = "") -> None:
    if result is None:
        record_auto_skip(agent, action.selection, reason or SKIP_INTERRUPTED)
        return
    facts = {
        "executed": True, "operation_id": action.call.operation_id, "tool": action.call.tool_name, "ok": result.ok,
        "error_code": result.error_code, "reported_error_code": result.reported_error_code,
        "effect_outcome": result.effect_outcome, "handler_executed": result.handler_executed, "status": result.status,
    }
    append_decision_outcome(agent, _auto_row(action.selection, "executed", facts))


# 函数用途: 开关开着但没执行时记一行带原因码的补充行（写文件副作用）。
def record_auto_skip(agent: object, selection: AutoExecutionSelection, reason: str) -> None:
    facts = {"executed": False, "operation_id": "", "ok": False, "error_code": "", "effect_outcome": "not_started",
             "reason": reason}
    append_decision_outcome(agent, _auto_row(selection, f"skipped:{reason}", facts))


# 函数用途: 生成补充行；候选与观察只记宿主铸的编号。
def _auto_row(selection: AutoExecutionSelection, category: str, facts: dict) -> dict:
    outcome = selection.outcome
    marked = SimpleNamespace(mode=getattr(outcome, "mode", ""), status=getattr(outcome, "status", ""), reason="",
                             response=getattr(outcome, "response", None))
    row = decision_outcome_row(selection.stage, selection.point, marked, 0.0)
    row.pop("requested_model", None)
    row.pop("model_version", None)
    row["record_kind"] = AUTO_RECORD_KIND
    row["result_category"] = f"{AUTO_RECORD_KIND}:{category}"
    row["decision_ref"] = selection.decision_ref
    row["actor"] = AUTO_ACTOR
    row["auto_execution"] = {"observation_id": selection.observation["observation_id"],
                             "candidate_id": selection.candidate["candidate_id"], **facts}
    return row


# LLM: 粗位置是通用几何扩展的派生值：候选 region（截图像素 [x, y, w, h]）中心按 frame.size×frame.scale 归一到 0–1，先夹到 [0, 1]
#   再保留一位小数；没有 region、没有 frame、尺寸/缩放非正或非有限数都不给位置，不补默认值。
# 函数用途: 算一个候选在目标里的归一化粗位置 {x, y}，供决策材料用；不可算时返回 None。
def coarse_position(frame: object, region: object) -> dict[str, float] | None:
    if type(frame) is not dict or not isinstance(region, (list, tuple)) or len(region) != 4:
        return None
    try:
        size, scale = frame["size"], frame["scale"]
        width, height = float(size[0]) * float(scale[0]), float(size[1]) * float(scale[1])
        x, y, w, h = (float(item) for item in region)
        center = ((x + w / 2) / width, (y + h / 2) / height)
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
        return None
    if width <= 0 or height <= 0 or not all(math.isfinite(value) for value in center):
        return None
    return {"x": round(min(1.0, max(0.0, center[0])), 1), "y": round(min(1.0, max(0.0, center[1])), 1)}


__all__ = [
    "AUTO_ACTOR", "AUTO_EXECUTIONS_PER_RUN_MAX_COUNT", "AUTO_RECORD_KIND", "SKIP_ALREADY_ACTED", "SKIP_AMBIGUOUS_ACTION",
    "SKIP_INTERRUPTED", "SKIP_NO_AUTO_ACTION", "SKIP_OWNER_SCOPE", "SKIP_RUN_LIMIT", "AutoExecutionSelection", "HostAction",
    "auto_execution_enabled", "coarse_position", "host_executed_actions", "plan_auto_execution", "record_auto_execution",
    "record_auto_skip",
]
