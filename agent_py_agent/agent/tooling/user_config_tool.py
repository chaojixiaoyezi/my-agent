from __future__ import annotations

"""Let the main agent read and change its own user-tunable configuration, with honest provenance."""

# LLM: 这个工具解决的真机问题是"用户问能不能改 compact 阈值，模型直接答'我没有权限'"——模型既不知道
#   用户配置在哪，也没有任何入口，于是凭空断言。这里给出结构化事实：生效值、来源（用户配置 vs 随包默认）、
#   白名单可改项、安全边界不可改项及原因、保存位置与生效时机。
#   安全边界永不开放：权限模式、路径危险根、凭据、宿主控制面路径都不在可写白名单内，写入一律拒绝并给原因。
# 模块用途: 让本机管理员主代理如实回答并受控修改自己的用户级配置。
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


# LLM: 与 gateway_status 一样只注册给 main_agent：它暴露宿主配置路径与生效语义，普通 owner 不需要。
# 类用途: 读取/修改用户级配置的受控入口。
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
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["view", "set"],
                    "description": "view=查询（默认）；set=修改白名单内的配置项。",
                },
                "key": {
                    "type": "string",
                    "description": "配置键名，例如 memory_compact_auto_trigger_percent。",
                },
                "value": {
                    "type": "string",
                    "description": "action=set 时的目标值（数字也用字符串传，服务端会校验区间）。",
                },
            },
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="system",
            use_cases=(
                "用户问某个运行参数现在是多少、存在哪个文件里",
                "用户要求修改 compact 触发百分比等用户级偏好",
                "需要说明某项配置为什么不能由模型自行修改",
            ),
            avoid_when=("需要改权限模式、危险路径或凭据时",),
        ),
    )
    # LLM: view 是只读；set 会改宿主配置文件，所以整体按 mutating 声明，让中央门与审计照常记录。
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

    # LLM: 注册入口与 gateway_status 保持同一形状（宿主传入 agent）；本工具只读宿主环境里的配置路径
    #   与白名单，不需要 agent 实例做权限判断，因此仅保留引用不做任何推断。
    # 函数用途: 接受宿主注册时传入的 agent，供同一批 main_agent 工具统一构造。
    def __init__(self, agent: object | None = None) -> None:
        self._agent = agent

    # LLM: action 是唯一分派依据；缺失按 view 处理（只读默认安全）。任何写入都经过白名单与区间校验，
    #   校验失败按结构化失败返回，不静默忽略、也不冒充成功。
    # 函数用途: 处理一次配置读取或写入调用。
    def execute(self, params: dict) -> ToolHandlerOutcome:
        action = str(params.get("action") or "view").strip().lower()
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
            f"不支持的 action: {action}（只接受 view/set）",
            error_code="TOOL_INVALID_ARGUMENTS",
        )

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
