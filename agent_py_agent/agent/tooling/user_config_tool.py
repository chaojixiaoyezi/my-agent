from __future__ import annotations

"""Let the main agent read and change its own user-tunable configuration, with honest provenance."""

# LLM: 这个工具解决的真机问题是"用户问能不能改 compact 阈值，模型直接答'我没有权限'"——模型既不知道
#   用户配置在哪，也没有任何入口，于是凭空断言。这里给出结构化事实：生效值、来源（用户配置 vs 随包默认）、
#   白名单可改项、安全边界不可改项及原因、保存位置与生效时机。
#   决策覆盖沿原设置服务，目录与显式探测沿原模型操作；身份只取可信 runner，不开放凭据或任意线程参数。
# 模块用途: 让原获授权主代理读取/修改配置、列出决策模型或按用户要求测试连接，保留统一权限与失败事实。
import json

from ..settings.user_config_capability import (
    TUNABLE_KEYS,
    capability_summary,
    packaged_config_path,
    read_config_fact,
    set_tunable_value,
    user_config_path,
)
from .models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    IdempotencyPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    SandboxPolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)


# LLM: schema 字段来自同一决策登记表，不通过任意属性路径扩大可修改范围；运行时仍由服务严格校验。
# 函数用途: 构造模型可见的字段 patch 描述，使用数值秒数和明确布尔开关。
def _decision_change_properties() -> dict:
    from ..settings.decision_settings_schema import decision_field_schema, decision_field_scopes

    return {path: decision_field_schema(path) for path in decision_field_scopes()}


# LLM: 保留原 main_agent 注册及中央策略；目录只读，探测只接受已存引用和显式预算，当前 runner 身份不可由模型覆盖。
# 类用途: 原配置工具的受控入口；读取、保存与显式联网测试分别复用原服务，不开放凭据写入。
class UserConfigTool(BaseTool):
    model_spec = ToolModelSpec(
        name="user_config",
        description=(
            "读取或修改本机用户级配置（如 compact 触发百分比）。"
            "用户问'这个参数能不能改/现在是多少/配置在哪'时必须先用本工具查结构化事实，"
            "不要凭感觉回答'我没有权限'，也不要把随包默认 YAML 当成用户配置。"
            "action=view 返回生效值、来源、可自助修改清单与安全边界清单；"
            "action=set 只接受白名单内的键，会校验取值、原子写入用户配置，并报告保存位置与生效时机"
            "（当前进程不会热加载）。安全边界（权限模式、危险路径、凭据）永远不可写。"
            "decision_read/decision_patch/decision_reset 读取、字段修改或恢复决策设置继承；"
            "先读 revision 再作为 expected_revision 提交。scope=owner 为长期设置，thread 仅当前可信会话；"
            "时间使用有限正秒数，reset 的 fields 删除覆盖。总开关关闭保留各点模式，observe 也会产生用量；"
            "保存不联网，时间只用于后续请求且不重置正在进行的阶段预算。"
            "decision_models 只读当前用户已保存或获共享授权的脱敏 Decision 目录，可用返回的编号绑定配置。"
            "decision_probe 仅在用户要求测试连接时使用，必须显式传 profile_id 和有限正 timeout_seconds；"
            "它会联网并产生用量，不能在读取目录或保存配置后自动测试，也不能据测试通过声称判断质量可靠。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["view", "set", "decision_read", "decision_patch", "decision_reset", "decision_models", "decision_probe"],
                    "description": "view/set 管理本机配置；decision_read/patch/reset 管理覆盖，decision_models 只读目录，decision_probe 显式测试连接。",
                },
                "key": {
                    "type": "string",
                    "description": "配置键名，例如 memory_compact_auto_trigger_percent。",
                },
                "value": {
                    "type": "string",
                    "description": "action=set 时的目标值（数字也用字符串传，服务端会校验区间）。",
                },
                "scope": {"type": "string", "enum": ["owner", "thread"], "description": "决策设置范围，默认 owner；thread 只指当前可信运行会话。"},
                "expected_revision": {"type": "object", "properties": {"owner": {"type": "integer", "minimum": 0}, "thread": {"type": "integer", "minimum": 0}}, "required": ["owner", "thread"], "additionalProperties": False},
                "changes": {"type": "object", "properties": _decision_change_properties(), "additionalProperties": False, "minProperties": 1},
                "fields": {"type": "array", "items": {"type": "string", "enum": list(_decision_change_properties())}, "minItems": 1, "description": "decision_reset 删除这些覆盖字段以恢复继承。"},
                "profile_id": {"type": "string", "description": "decision_probe 必填：decision_models 返回的已保存 Decision 编号或 shared:编号，不能传模型名称、地址或凭据。"},
                "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "description": "decision_probe 必填：用户明确要求的有限正测试预算秒数，包含准备与完整请求。"},
            },
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="system",
            use_cases=(
                "用户问某个运行参数现在是多少、存在哪个文件里",
                "用户要求修改 compact 触发百分比等用户级偏好",
                "需要说明某项配置为什么不能由模型自行修改",
                "用户要求开启、关闭或调整决策模型等待时间及接入点，并指定长期或本会话范围",
                "用户要求选择已保存的决策配置，或明确要求测试其连接",
            ),
            avoid_when=("需要改权限模式、危险路径或凭据时",),
        ),
    )
    # LLM: view/目录只读，set 改配置，显式 probe 会联网并记用量；保持原 mutating 中央门、串行策略和操作审计。
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        sandbox_policy=SandboxPolicy("none"),
        concurrency_policy=ConcurrencyPolicy("serial"),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(
            mode="declared",
            static_scopes=("user_config:current",),
        ),
        output_policy=OutputPolicy(trust="runtime"),
        promotes_task=False,
        mutates_workspace=False,
    )

    # LLM: 只保留宿主 Agent 引用，决策动作从其可信 runner/owner 获取身份，不构造新 Agent。
    # 函数用途: 接受原工具注册上下文，供决策服务复用原配置及线程存储。
    def __init__(self, agent: object | None = None) -> None:
        self._agent = agent

    # LLM: action 结构化分派；目录和显式探测使用原模型操作，覆盖仍走原 CAS，失败不解析正文或冒充成功。
    # 函数用途: 处理获授权配置操作，将用户要求的联网测试与普通读取、保存明确分开。
    def execute(self, params: dict) -> ToolHandlerOutcome:
        action = str(params.get("action") or "view").strip().lower()
        if action in {"decision_read", "decision_patch", "decision_reset"}:
            return self._decision(action.removeprefix("decision_"), params)
        if action in {"decision_models", "decision_probe"}:
            return self._decision_model_operation(action, params)
        key = str(params.get("key") or "").strip()
        if action == "view":
            return self._view(key)
        if action == "set":
            report = set_tunable_value(key, params.get("value"))
            if not report.get("ok"):
                error = str(report.get("error") or "配置写入被拒绝")
                return ToolHandlerOutcome("user_config", False, error, error_code="TOOL_INVALID_ARGUMENTS")
            return ToolHandlerOutcome(
                "user_config", True, json.dumps(report, ensure_ascii=False, sort_keys=True)
            )
        return ToolHandlerOutcome(
            "user_config",
            False,
            "不支持的配置 action。",
            error_code="TOOL_INVALID_ARGUMENTS",
        )

    # LLM: 线程只来自 current_task_attributes；显式身份参数被原服务白名单拒绝，旧 revision 不自动重试。
    # 函数用途: 调用共同决策设置服务，并将权限、版本冲突及字段错误映射为原工具结构化失败。
    def _decision(self, operation: str, params: dict) -> ToolHandlerOutcome:
        from ..agent_core.runner.context import current_task_attributes
        from ..settings.decision_settings import execute_decision_settings_operation
        from ..settings.decision_settings_schema import (
            DecisionSettingsAccessError,
            DecisionSettingsConflict,
        )
        from ..settings.model_provider_schema import ModelProfileError

        if self._agent is None:
            return ToolHandlerOutcome("user_config", False, "缺少可信用户配置上下文。", error_code="TOOL_PERMISSION_DENIED", effect_outcome="not_started")
        attrs = current_task_attributes(self._agent) or {}
        thread = attrs.get("agent_thread_id") or attrs.get("conversation_thread_id") or ""
        thread_id = thread.strip() if type(thread) is str else ""
        payload = {key: value for key, value in params.items() if key != "action"}
        if payload.get("scope") == "thread" and not thread_id:
            return ToolHandlerOutcome("user_config", False, "当前运行没有可信会话，不能写入临时覆盖。", error_code="TOOL_PERMISSION_DENIED", effect_outcome="not_started")
        try:
            report = execute_decision_settings_operation(self._agent, operation, payload, thread_id=thread_id)
        except DecisionSettingsConflict as exc:
            return ToolHandlerOutcome("user_config", False, str(exc), error_code="STALE_VERSION", reported_error_code="DECISION_SETTINGS_CONFLICT", effect_outcome="not_started")
        except DecisionSettingsAccessError as exc:
            return ToolHandlerOutcome("user_config", False, str(exc), error_code="TOOL_PERMISSION_DENIED", effect_outcome="not_started")
        except ModelProfileError as exc:
            return ToolHandlerOutcome("user_config", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS", effect_outcome="not_started")
        except OSError:
            return ToolHandlerOutcome("user_config", False, "配置存储读写失败，请重新读取核对是否保存。", error_code="TOOL_PERSISTENCE_FAILED", effect_outcome="unknown")
        report["scope_resolution"] = {"source": "current_runner_context", "effective": {"thread_id": thread_id, "scope": report["scope"]}}
        return ToolHandlerOutcome("user_config", True, json.dumps(report, ensure_ascii=False, sort_keys=True))

    # LLM: 只转交原脱敏目录/原探测，不重建后端或配置器；拒绝显式身份和秘密，probe 预算必填，取消原样传播。
    # 函数用途: 按当前可信会话列出决策配置或发起一次用户要求的测试；真实测试失败保持工具失败及结构化报告。
    def _decision_model_operation(self, operation: str, params: dict) -> ToolHandlerOutcome:
        from ..agent_core.runner.context import current_task_attributes
        from ..settings.decision_settings_schema import (
            DecisionSettingsAccessError,
            positive_seconds,
            profile_reference,
        )
        from ..settings.model_profiles import execute_model_profile_operation
        from ..settings.model_provider_schema import ModelProfileError

        if self._agent is None:
            return ToolHandlerOutcome("user_config", False, "缺少可信用户配置上下文。", error_code="TOOL_PERMISSION_DENIED", effect_outcome="not_started")
        attrs = current_task_attributes(self._agent) or {}
        thread = attrs.get("agent_thread_id") or attrs.get("conversation_thread_id") or ""
        thread_id = thread.strip() if type(thread) is str else ""
        try:
            required = {"action", "profile_id", "timeout_seconds"} if operation == "decision_probe" else {"action"}
            if set(params) != required:
                raise ModelProfileError("决策目录仅接受 action；连接测试必须且只能填写 action、profile_id、timeout_seconds，不能指定身份或连接凭据。")
            payload = {}
            if operation == "decision_probe":
                payload = {"profile_id": profile_reference(params["profile_id"]), "timeout_seconds": positive_seconds(params["timeout_seconds"])}
                if not payload["profile_id"]:
                    raise ModelProfileError("连接测试需要明确选择已保存的决策模型编号。")
                if not thread_id:
                    raise DecisionSettingsAccessError("当前运行没有可信会话，不能发起决策连接测试。")
            report = execute_model_profile_operation(self._agent, operation, payload, thread_id=thread_id)
        except InterruptedError:
            raise
        except DecisionSettingsAccessError as exc:
            return ToolHandlerOutcome("user_config", False, str(exc), error_code="TOOL_PERMISSION_DENIED", effect_outcome="not_started")
        except ModelProfileError as exc:
            return ToolHandlerOutcome("user_config", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS", effect_outcome="not_started")
        except OSError:
            return ToolHandlerOutcome("user_config", False, "决策配置读取或测试记录保存失败，请重新读取核对。", error_code="TOOL_PERSISTENCE_FAILED", effect_outcome="unknown")
        scope = "thread" if operation == "decision_probe" else "owner"
        report = {**report, "scope_resolution": {"source": "current_runner_context", "effective": {"thread_id": thread_id, "scope": scope}}}
        output = json.dumps(report, ensure_ascii=False, sort_keys=True)
        if report.get("ok") is not True:
            return ToolHandlerOutcome("user_config", False, output, result_envelope={"decision_report": report},
                error_code="TOOL_EXECUTION_FAILED", reported_error_code="DECISION_PROBE_FAILED", effect_outcome="unknown")
        return ToolHandlerOutcome("user_config", True, output, result_envelope={"decision_report": report})

    # 函数用途: 组装读取结果：单键给生效值/来源，未给键则给白名单与安全边界清单。
    def _view(self, key: str) -> ToolHandlerOutcome:
        user_path = user_config_path()
        packaged = packaged_config_path()
        payload: dict[str, object] = {
            "user_config_path": str(user_path) if user_path is not None else "",
            "user_config_configured": user_path is not None,
            "packaged_config_path": str(packaged),
            "capability": capability_summary(),
        }
        if key:
            payload["fact"] = read_config_fact(key, user_path=user_path, default_path=packaged)
            spec = TUNABLE_KEYS.get(key)
            payload["tunable"] = spec is not None
            if spec is not None:
                payload["describe"] = spec.describe
                payload["effect_when"] = spec.effect
        return ToolHandlerOutcome(
            "user_config", True, json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )


__all__ = ["UserConfigTool"]
