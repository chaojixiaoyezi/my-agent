from __future__ import annotations

"""Let the main agent read and change its own user-tunable configuration, with honest provenance."""

# LLM: 这个工具解决的真机问题是"用户问能不能改 compact 阈值，模型直接答'我没有权限'"——模型既不知道
#   用户配置在哪，也没有任何入口，于是凭空断言。这里给出结构化事实：生效值、来源（用户配置 vs 随包默认）、
#   白名单可改项、安全边界不可改项及原因、保存位置与生效时机。
#   决策覆盖沿原设置服务；模型只读/撤销实验许可，建立许可必须是宿主显式用户控制，身份不取模型参数。
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
from ..user_space.owner_access import is_local_admin_owner
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


# LLM: 身份只来自 Agent 的已解析 home；缺失上下文绝不能因空 provider/kind 被当作本机管理员。
# 函数用途: 决定 legacy view/set 是否能展示和执行，普通 user 始终只能操作自己的决策设置。
def _is_main_owner(agent: object | None) -> bool:
    home_paths = getattr(agent, "home_paths", None)
    # 原 owner 判据兼容空 provider/kind；工具提权入口必须额外要求已解析的完整身份。
    return (home_paths is not None
            and all(type(getattr(home_paths, field, None)) is str and getattr(home_paths, field).strip()
                    for field in ("owner_provider", "owner_kind", "owner_id"))
            and is_local_admin_owner(home_paths))


# LLM: 普通 owner 的模型 schema 只保留决策字段，action 必填；运行时仍须二次拒绝 legacy，不能只依赖展示。
# 函数用途: 从原工具声明裁剪出同名 decision-only 视图，不复制设置服务或模型目录逻辑。
def _decision_only_spec(full_spec: ToolModelSpec) -> ToolModelSpec:
    properties = {key: value for key, value in full_spec.input_schema["properties"].items() if key not in {"key", "value"}}
    properties["action"] = {**properties["action"],
        "enum": ["decision_read", "decision_patch", "decision_reset", "decision_models", "decision_probe", "decision_experiment_revoke"],
        "description": "仅管理当前可信 owner 的决策设置；读取后携完整 revision 修改。连接测试须用户明确要求。"}
    return ToolModelSpec(
        name=full_spec.name,
        description=("读取或修改当前用户自己的决策设置。先 decision_read 获取 owner/thread revision，"
                     "再用 decision_patch 或 decision_reset 修改并读回生效值；scope=thread 只用于当前可信会话。"
                     "decision_models 只读脱敏目录；decision_probe 仅在用户明确要求测试连接时使用，会联网并产生用量。"
                     "decision_experiment_revoke 只能撤销当前会话已有授权，不能建立授权。"
                     "本工具不开放本机全局配置、凭据或其他用户设置。"),
        input_schema={"type": "object", "properties": properties, "required": ["action"], "additionalProperties": False},
        hints=ToolModelHints(category="system", use_cases=(
            "用户要求开启、关闭或调整自己的决策模型等待时间与接入点",
            "用户要求查看自己的决策模型设置、已保存模型或撤销当前实验授权",
            "用户明确要求测试已保存决策模型连接",
        ), avoid_when=("需要本机全局配置、凭据或其他用户设置时",)),
    )


# LLM: 子代理自己的 thread 优先；仅非子代理主回合可从线程本地 RunParams 读取 Gateway 绑定，绝不接受模型参数或父线程回退。
# 函数用途: 统一决策覆盖与模型目录所需的当前可信会话，保留身份来源给原工具回执。
def _decision_thread_scope(agent: object) -> tuple[str, str]:
    from ..runtime_context import current_subagent_run_id, current_task_attributes

    attrs = current_task_attributes(agent) or {}
    value = attrs.get("agent_thread_id") or attrs.get("conversation_thread_id")
    if type(value) is str and value.strip():
        return value.strip(), "current_runner_context"
    if current_subagent_run_id(agent):
        return "", "current_runner_context"
    current = getattr(agent, "_current_run_params", None)
    run_attrs = getattr(current, "task_attributes", None)
    if isinstance(run_attrs, dict):
        value = run_attrs.get("agent_thread_id") or run_attrs.get("conversation_thread_id")
        if type(value) is str and value.strip():
            return value.strip(), "current_run_params"
    return "", "current_runner_context"


# LLM: 只在本会话已有实验授权信封、且当前运行由 Gateway 宿主写入器承载时附只读 experiment_evaluation；
#   读取器来自宿主运行参数（不是模型参数），失败只给结构化 unavailable，不影响设置读取，也绝不触发晋升或写入。
#   评估块插在授权信封之后，避免被大段字段目录挤出有界预览；没有信封时原输出逐字节不变。
# 函数用途: 让模型在读取决策设置时能看到实验证据是否已满足晋升建议。
def _with_experiment_evaluation(agent: object, report: dict, thread_id: str) -> dict:
    authorization = report.get("experiment_authorization")
    writer = getattr(getattr(agent, "_current_run_params", None), "conversation_task_binding_callback", None)
    reader = getattr(writer, "decision_experiment_evaluation", None)
    if authorization is None or not thread_id or not callable(reader):
        return report
    try:
        evaluation = reader(agent, thread_id, authorization)
    except Exception:  # noqa: BLE001 只读证据读取失败不能影响设置读取
        evaluation = {"status": "unavailable", "reasons": ["evidence_unreadable"]}
    keys = list(report)
    position = keys.index("experiment_authorization") + 1
    return {**{key: report[key] for key in keys[:position]}, "experiment_evaluation": evaluation,
            **{key: report[key] for key in keys[position:]}}


# LLM: main_agent 保留完整配置能力，普通 owner 仅得 decision-only 视图；目录只读，探测只接受已存引用和显式预算。
# 类用途: 按可信 owner 身份展示配置工具，读取、保存与显式联网测试仍复用原服务。
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
            "decision_experiment_revoke 可用当前授权编号撤销本会话实验；本工具不能建立实验授权，能力开关不代表用户授权。"
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
                    "enum": ["view", "set", "decision_read", "decision_patch", "decision_reset", "decision_experiment_revoke", "decision_models", "decision_probe"],
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
                "authorization_id": {"type": "string", "description": "decision_experiment_revoke 必填：decision_read 返回的当前完整实验授权编号；不能用来建立许可。"},
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

    # LLM: 缺主 owner 身份时生成 decision-only 模型声明；后续执行再复核身份，不能依赖模型 schema 当权限门。
    # 函数用途: 接受原工具注册上下文，并按当前可信 owner 缩窄普通用户可见动作。
    def __init__(self, agent: object | None = None) -> None:
        self._agent = agent
        if not _is_main_owner(agent):
            self.model_spec = _decision_only_spec(type(self).model_spec)

    # LLM: decision-only 的非决策动作在 handler 内再次拒绝，即使绕开模型 schema 也不能触及进程级 MY_AGENT_CONFIG。
    # 函数用途: 按结构化 action 分派原设置服务，并对普通 owner 阻断 legacy view/set。
    def execute(self, params: dict) -> ToolHandlerOutcome:
        action = str(params.get("action") or "view").strip().lower()
        if action in {"decision_read", "decision_patch", "decision_reset", "decision_experiment_revoke"}:
            return self._decision(action.removeprefix("decision_"), params)
        if action in {"decision_models", "decision_probe"}:
            return self._decision_model_operation(action, params)
        if not _is_main_owner(self._agent):
            return ToolHandlerOutcome("user_config", False, "当前用户不能访问本机全局配置。",
                error_code="TOOL_PERMISSION_DENIED", effect_outcome="not_started")
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

    # LLM: 主会话可读取宿主线程本地 RunParams，子代理只读自身 runner 身份；撤销只能引用原许可并使用完整 CAS。
    #   read 在本会话已有实验授权时附只读实验证据评估（_with_experiment_evaluation），模型无法据此授权或晋升。
    # 函数用途: 调用共同决策设置服务修改原覆盖或撤销许可，身份与过期版本不能由模型覆盖。
    def _decision(self, operation: str, params: dict) -> ToolHandlerOutcome:
        from ..settings.decision_settings import execute_decision_settings_operation
        from ..settings.decision_settings_schema import (
            DecisionSettingsAccessError,
            DecisionSettingsConflict,
        )
        from ..settings.model_provider_schema import ModelProfileError

        if self._agent is None:
            return ToolHandlerOutcome("user_config", False, "缺少可信用户配置上下文。", error_code="TOOL_PERMISSION_DENIED", effect_outcome="not_started")
        thread_id, source = _decision_thread_scope(self._agent)
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
        report["scope_resolution"] = {"source": source, "effective": {"thread_id": thread_id, "scope": report["scope"]}}
        if operation == "read":
            report = _with_experiment_evaluation(self._agent, report, thread_id)
        # 原投影先列 revision，再列大段字段目录；排序会把 CAS 版本挤出有界模型预览。
        return ToolHandlerOutcome("user_config", True, json.dumps(report, ensure_ascii=False))

    # LLM: 目录与探测复用同一可信 thread 裁决；不重建后端或配置器，拒绝显式身份和秘密，取消原样传播。
    # 函数用途: 按当前可信会话列出决策配置或发起一次用户要求的测试；真实测试失败保持工具失败及结构化报告。
    def _decision_model_operation(self, operation: str, params: dict) -> ToolHandlerOutcome:
        from ..settings.decision_settings_schema import (
            DecisionSettingsAccessError,
            positive_seconds,
            profile_reference,
        )
        from ..settings.model_profiles import execute_model_profile_operation
        from ..settings.model_provider_schema import ModelProfileError

        if self._agent is None:
            return ToolHandlerOutcome("user_config", False, "缺少可信用户配置上下文。", error_code="TOOL_PERMISSION_DENIED", effect_outcome="not_started")
        thread_id, source = _decision_thread_scope(self._agent)
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
        report = {**report, "scope_resolution": {"source": source, "effective": {"thread_id": thread_id, "scope": scope}}}
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
