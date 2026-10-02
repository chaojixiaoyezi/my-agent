# LLM: 观察三件套的唯一来源无关实现：动作工具发送前按候选 ID 复核并回填 _meta、发送后在信封记 observation_action 事实（不论结果，
#   自动执行幂等靠它）、观察工具成功结果铸 ID 并改写模型投影、提供方按代次拒绝时提升成宿主错误码。PluginProxyTool（声明来自 manifest，activation 来自插件激活）与 MCPProxyTool（声明来自
#   mcp_servers 逐工具表，activation 来自本次固定连接代次）共用；binding 为 None 的代理行为不变。身份只取宿主参数
#   （__run_scope / __operation_id）与绑定时的结构化事实，不取提供方自报。改动须同步 plugin_runtime、mcp_registration 与
#   test_plugin_proxy_observation / test_mcp_observation_binding。
# 模块用途: 让插件和 MCP 的"观察 → 候选 → 动作"走同一条宿主复核链，不各写一份。
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from ..plugin_observation import (
    OBSERVATION_CANDIDATE_UNKNOWN,
    OBSERVATION_ERROR_KEY,
    OBSERVATION_KEY,
    OBSERVATION_META_EXTENSION,
    OBSERVATION_STALE,
    ObservationHostContext,
    ObservationRejected,
    PluginToolObservation,
    PluginToolObservationRef,
    observation_meta,
    parse_observation,
    resolve_action_candidate,
)
from .models import ToolHandlerOutcome, ToolInvocationContext

# 提供方按代次复核候选后拒绝执行的结构化原因 → 宿主 reported_error_code；两种拒绝按合同零副作用
_PROVIDER_OBSERVATION_ERROR_CODES = {"stale": OBSERVATION_STALE, "not_found": OBSERVATION_CANDIDATE_UNKNOWN}
# 候选复核不通过时给模型看的固定提示，不含载荷内容
_REJECTED_MESSAGE = "候选已过期或不存在，请先重新观察再操作"

# 真正发送一次调用的回调：(参数, 执行上下文, 额外 _meta) → 工具结果
ObservationSend = Callable[[dict[str, Any], "ToolInvocationContext | None", "dict[str, object] | None"], ToolHandlerOutcome]


# LLM: provider_id 只记来源（plugin:<id> / mcp:<server>）；activation_id 是提供方代次（插件激活或 MCP 连接代次），换代即旧观察
#   stale；action_tools 是同一提供方内"远端动作工具名 → 宿主注册名"，只含同 target_kind 的 observation_ref 工具；repo 是 owner
#   权威运行库的只读引用，None 时复核一律不通过。observation 与 observation_ref 二选一。
# 类用途: 一个代理工具与其观察声明、来源身份和复核库的绑定。
@dataclass(frozen=True)
class ObservationBinding:
    provider_id: str
    activation_id: str
    tool_name: str
    observation: PluginToolObservation | None = None
    observation_ref: PluginToolObservationRef | None = None
    action_tools: Mapping[str, str] | None = None
    repo: object | None = None

    # 函数用途: 拒绝既不观察也不动作、或两者都声明的绑定。
    def __post_init__(self) -> None:
        if (self.observation is None) == (self.observation_ref is None):
            raise ValueError("观察绑定必须恰好声明观察或观察引用之一")

    # LLM: 动作工具填了候选 ID 先复核新鲜度再发送（不通过记 not_started、不发送）；发送过就在信封记 observation_action（成功、失败、
    #   被提供方拒绝都记，表示这个观察已被动作碰过）；观察工具成功后校验并铸 ID；提供方按代次拒绝时提升为结构化码。
    #   send 是代理自己的发送主体，这里不缓存任何逐次状态到实例上。
    # 函数用途: 带观察合同地执行一次调用。
    def execute(self, params: dict[str, Any], context: ToolInvocationContext | None, send: ObservationSend) -> ToolHandlerOutcome:
        extra_meta = None
        if self.observation_ref is not None:
            try:
                extra_meta = self.action_meta(params)
            except ObservationRejected as exc:
                return self._rejected_outcome(exc.code)
        outcome = send(params, context, extra_meta)
        if self.observation is not None and outcome.ok:
            outcome = self.attach(outcome, params, context)
        if extra_meta is not None:
            outcome = self._with_action_fact(outcome, params, extra_meta)
            if not outcome.ok:
                outcome = self.lift_error(outcome)
        return outcome

    # LLM: 事实只取宿主复核过的 _meta 里的观察编号与模型填的候选编号（已过复核），工具名是本绑定的注册名；写进 result_envelope，
    #   由归档与 tool_completed 事件沿同一投影落库。不改输出正文。
    # 函数用途: 给发送过的动作调用记下“碰过哪个观察的哪个候选”。
    def _with_action_fact(self, outcome: ToolHandlerOutcome, params: dict[str, Any], extra_meta: dict[str, object]) -> ToolHandlerOutcome:
        meta = extra_meta.get(OBSERVATION_META_EXTENSION)
        observation_id = str(meta.get("observation_id") or "") if isinstance(meta, dict) else ""
        fact = {"observation_id": observation_id, "candidate_id": str(params.get(self.observation_ref.param) or "").strip(),
                "tool": self.tool_name}
        return replace(outcome, result_envelope={**outcome.result_envelope, "observation_action": fact})

    # LLM: 只在模型填了 observation_ref.param 时复核：候选按当前 run/task 的权威事件流解析，过期/未知抛 ObservationRejected；
    #   候选所属观察的 activation_id 必须等于本绑定的（提供方换代——插件重新激活或 MCP 子进程重启——后旧观察一律 stale，
    #   不交给新实例猜）；没填参数保持现状（如按选择器执行）。复核通过才把提供方自己的 key 与目标代次放进 _meta，arguments 不能冒充。
    # 函数用途: 生成动作调用要附给提供方的观察 _meta，或判定候选不可用。
    def action_meta(self, params: dict[str, Any]) -> dict[str, object] | None:
        candidate_id = params.get(self.observation_ref.param)
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            return None
        scope = params.get("__run_scope") if isinstance(params.get("__run_scope"), dict) else {}
        observation, candidate = resolve_action_candidate(
            self.repo, run_id=str(scope.get("run_id") or ""), task_id=str(scope.get("task_id") or ""),
            candidate_id=candidate_id.strip(), action_tool=self.tool_name,
        )
        if str(observation.get("activation_id") or "") != self.activation_id:
            raise ObservationRejected(OBSERVATION_STALE)
        return {OBSERVATION_META_EXTENSION: observation_meta(observation, candidate)}

    # LLM: 只处理成功结果里 structuredContent.my_agent_observation；形状合规则铸 ID、把归档权威写进 result_envelope.observation，
    #   模型可见投影只留 candidate_id/role/label/actions；不合规整份丢弃、模型看不到候选，信封记 observation_rejected 原因码。
    # 函数用途: 把提供方的观察载荷变成宿主的结构化观察事实。
    def attach(self, outcome: ToolHandlerOutcome, params: dict[str, Any], context: ToolInvocationContext | None) -> ToolHandlerOutcome:
        try:
            payload = json.loads(outcome.output)
        except (TypeError, ValueError):
            return outcome
        structured = payload.get("structuredContent") if isinstance(payload, dict) else None
        if not isinstance(structured, dict) or OBSERVATION_KEY not in structured:
            return outcome
        envelope = dict(outcome.result_envelope)
        try:
            record = parse_observation(structured[OBSERVATION_KEY], self.host_context(params, context))
        except ObservationRejected as exc:
            structured.pop(OBSERVATION_KEY, None)
            envelope["observation_rejected"] = exc.code
        else:
            structured[OBSERVATION_KEY] = record.model_projection()
            envelope["observation"] = record.to_envelope()
        return replace(outcome, output=json.dumps(payload, ensure_ascii=False), result_envelope=envelope)

    # LLM: 提供方按代次复核候选后拒绝执行时，在 isError 结果的 structuredContent.my_agent_observation_error.code 里给结构化原因
    #   （stale / not_found）。这里把它提升成宿主的 reported_error_code（OBSERVATION_STALE / OBSERVATION_CANDIDATE_UNKNOWN）、
    #   error_code TOOL_INVALID_ARGUMENTS、effect_outcome not_started，信封记 observation_rejected；其它错误原样保留，不解析正文。
    # 函数用途: 让候选过期/不存在成为可结构化分支的失败，而不是笼统的 TOOL_EXECUTION_FAILED。
    def lift_error(self, outcome: ToolHandlerOutcome) -> ToolHandlerOutcome:
        try:
            payload = json.loads(outcome.output)
        except (TypeError, ValueError):
            return outcome
        structured = payload.get("structuredContent") if isinstance(payload, dict) else None
        error = structured.get(OBSERVATION_ERROR_KEY) if isinstance(structured, dict) else None
        code = _PROVIDER_OBSERVATION_ERROR_CODES.get(str(error.get("code") or "")) if isinstance(error, dict) else None
        if code is None:
            return outcome
        return replace(
            outcome, error_code="TOOL_INVALID_ARGUMENTS", reported_error_code=code, effect_outcome="not_started",
            result_envelope={**outcome.result_envelope, "observation_rejected": code},
        )

    # 函数用途: 汇集铸 ID 所需的宿主身份（run/task 来自 __run_scope、operation 来自 __operation_id）与同类动作工具映射。
    def host_context(self, params: dict[str, Any], context: ToolInvocationContext | None) -> ObservationHostContext:
        scope = params.get("__run_scope") if isinstance(params.get("__run_scope"), dict) else {}
        snapshot = getattr(context, "runtime_snapshot", None)
        return ObservationHostContext(
            run_id=str(scope.get("run_id") or getattr(snapshot, "run_id", "") or ""),
            task_id=str(scope.get("task_id") or ""), operation_id=str(params.get("__operation_id") or ""),
            activation_id=self.activation_id, provider_id=self.provider_id, tool_name=self.tool_name,
            target_kind=self.observation.target_kind, max_candidates=self.observation.max_candidates,
            action_tools=dict(self.action_tools or {}),
        )

    # 函数用途: 发送前复核不通过的固定结果：TOOL_INVALID_ARGUMENTS、not_started、信封记原因码，不发送。
    def _rejected_outcome(self, code: str) -> ToolHandlerOutcome:
        return ToolHandlerOutcome(
            self.tool_name, False, json.dumps({"error": _REJECTED_MESSAGE, "code": code}, ensure_ascii=False),
            error_code="TOOL_INVALID_ARGUMENTS", reported_error_code=code, effect_outcome="not_started",
            result_envelope={"observation_rejected": code},
        )


__all__ = ["ObservationBinding", "ObservationSend"]
