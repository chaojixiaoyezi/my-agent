# LLM: manage_models 工具——让主会话代理代替用户直接管理 owner 模型目录（等价于 TUI /model）：列出、快捷新增、
#   保存/编辑服务商与模型、切换当前会话或新会话默认模型、删除、连通性探针与服务商模型发现。唯一写入口仍是
#   settings.model_profiles.execute_model_profile_operation（文件锁内原子落盘）；本工具只做参数整理、身份裁决与回执整形。
#   例外：set_embedding/disable_embedding（语义记忆的向量模型，全局配置）走 settings.embedding_selection，裁决规则（本机管理员、
#   本人目录嵌入档案、与默认对话模型同主机）都在那里，本工具只映射回执。
#   契约：回执永不包含 api_key（只透传投影里的 has_key）；错误文案不回显参数值；delete_provider 声明为 dangerous
#   （密钥不可恢复）走统一审批门；子代理运行内不可用；select 只作用于当前 conversation_thread_id，没有会话时要求改用
#   set_default。改动时同步 tests/test_model_profile_tool.py、docs/design/TUI_MODEL_PROFILES.md 与 prompts/default.md。
# 模块用途: 把“帮我加/换/改/删模型”变成代理可直接执行的结构化工具，用户不必自己进 /model 菜单填表。
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from ..backends.reasoning_control import REASONING_CONTROLS
from ..backends.structured_output_mode import STRUCTURED_OUTPUT_MODES
from ..conversation.authority import current_conversation_task_attributes
from ..runtime_context import current_subagent_run_id
from ..settings.embedding_selection import (
    EMBEDDING_HOST_CHANGE_REASON,
    catalog_write_check,
    model_disable_embedding,
    model_set_embedding,
    semantic_memory_view,
)
from ..settings.model_profiles import (
    ModelProfileError,
    execute_model_profile_operation,
    model_profile_write_check,
)
from ..settings.model_provider_schema import BACKENDS, CAPABILITIES
from ..tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

if TYPE_CHECKING:
    from ..core import SimpleAgent

TOOL_NAME = "manage_models"
_ACTIONS = (
    "list", "add", "add_models", "save_provider", "save_model", "select", "set_default",
    "delete_model", "delete_provider", "probe", "discover", "set_shared", "set_initial", "set_embedding", "disable_embedding",
)
# 向量模型（语义记忆）两个动作：全局设置，裁决与写入都在 settings.embedding_selection（用户 10-02 拍板的结构化规则）。
_EMBEDDING_ACTIONS = frozenset({"set_embedding", "disable_embedding"})
# 写之前就拒绝的两种结果 → 工具控制码；其余失败（参数中心写入出错）按执行失败、副作用未知交给对账。
_EMBEDDING_REFUSALS = {"PARAMETER_BOUNDARY": "TOOL_PERMISSION_DENIED", "EMBEDDING_HOST_DIFFERS": "EMBEDDING_HOST_DIFFERS"}
_PROFILE_ID_ACTIONS = {"select", "set_default", "delete_model", "probe"}
_PROVIDER_ID_ACTIONS = {"save_provider", "delete_provider"}
_DESCRIPTION = (
    "直接管理当前用户的模型目录（等价于 TUI /model）：list 列出、add 快捷新增（服务商+模型一步保存）、"
    "save_provider/save_model 保存或编辑、select 切换当前会话模型、set_default 设置新会话默认模型、"
    "delete_model/delete_provider 删除、probe 连通性测试、discover 读取服务商模型列表（可带未保存的 connection 先看有哪些模型）、"
    "add_models 按一个连接或已有服务商一次加多个模型、set_shared 把自己的模型开放给其他用户、set_initial 指定其他用户的初始模型"
    "（后两项只有管理员能用，由宿主校验）、set_embedding 选向量模型并打开语义记忆、disable_embedding 关闭语义记忆"
    "（用户要求开/关语义记忆时直接调用，用户的要求就是授权，不要再请用户确认或让用户自己去 /model；list 回执的 semantic_memory "
    "给出当前状态和可选的 embedding 模型 id。管理员身份、本人目录与服务商主机由宿主核对，只有回执 needs_user_choice 时才请用户"
    "自己在 /model → 选择模型 → 向量模型 里选；保存后重启 Gateway 生效。会把当前向量模型挪到别的服务商主机的修改——改它的服务商地址、"
    "把它挪到别的服务商、删掉它——也回 needs_user_choice，请用户自己在 /model 里改）。"
    "用户说“帮我加/换/改/删模型或服务商”“默认模型改成 X”“测一下这个模型能不能用”时直接调用，不要让用户自己去 /model 填表。"
    "密钥只作为参数传一次；回执和你的回复都不得复述密钥。delete_provider 会经统一危险动作审批门。"
)
_USE_CASES = (
    "用户给出模型名、接口地址和密钥要求加模型 → action=add（缺上下文窗口先问用户或用 discover 读取）",
    "用户给出接口地址和密钥、要加这个服务商的多个模型 → 先 discover 带 connection 读列表，再 add_models 一次加入",
    "用户要求本会话换成某个已保存模型 → 先 list 取精确 id，再 action=select",
    "用户要求以后新会话默认用某模型 → action=set_default",
    "用户怀疑模型配置不可用 → action=probe 用真实请求验证，不凭列表状态下结论",
    "用户要开语义记忆/向量记忆 → list，取 semantic_memory.choices 里的 id 直接 set_embedding（没有可选项就先 add 一个 embedding 模型）",
)
_AVOID_WHEN = (
    "用 write/edit/shell 直接改 config/model-profiles 目录 → 必须改用 manage_models",
    "用户只是询问当前用哪个模型 → action=list 只读即可，不要顺手 select/set_default",
    "子代理任务里 → 本工具不可用，模型目录只由主会话代理管理",
)
_KEYWORDS = ("加模型", "新增模型", "换模型", "切换模型", "默认模型", "删除模型", "服务商", "api key", "密钥", "/model", "上下文窗口",
             "共享模型", "初始模型", "语义记忆", "向量模型", "embedding")
_EXAMPLES = (
    '{"tool":"manage_models","action":"list"}',
    '{"tool":"manage_models","action":"add","profile":{"model_name":"MiniMax-M2.7","model_backend":"anthropic_compatible",'
    '"api_base":"https://api.example.com/anthropic","api_key":"<用户给的密钥>","model_context_window_tokens":200000}}',
    '{"tool":"manage_models","action":"select","profile_id":"<list 返回的 id>"}',
    '{"tool":"manage_models","action":"save_provider","provider_id":"minimax","editing":true,"provider":{"display_name":"MiniMax 主号"}}',
    '{"tool":"manage_models","action":"probe","profile_id":"<list 返回的 id>"}',
    '{"tool":"manage_models","action":"add_models","connection":{"model_backend":"openai_compatible",'
    '"api_base":"https://api.example.com/v1","api_key":"<用户给的密钥>"},'
    '"models":[{"model_name":"model-a","model_context_window_tokens":128000}]}',
    '{"tool":"manage_models","action":"set_initial","profile_id":"<list 返回的 id>"}',
    '{"tool":"manage_models","action":"set_embedding","profile_id":"<list 返回的 embedding 用途模型 id>"}',
)
_PARAMETERS = {
    "action": "必填。" + "/".join(_ACTIONS) + "。",
    "profile_id": (
        "select/set_default/delete_model/probe/set_embedding 必填，save_model 编辑已有模型时必填；只能用 list 返回的精确 id，"
        "select/set_default 还可用 default 表示显式部署配置。add/save_model 新建时省略，由系统生成。"
    ),
    "provider_id": "save_provider/delete_provider/discover 必填，save_model 新建时必填；1 至 96 个字母、数字、点、横线或下划线。",
    "provider": (
        "save_provider 必填。display_name/api_base/api_key/enabled/capabilities/custom_headers/session_header；"
        "编辑时 api_key 留空表示保留原密钥。"
    ),
    "profile": (
        "add/save_model 必填。add 填 model_name/model_backend/api_base/api_key/model_context_window_tokens（可选 capability、temperature、top_p）；"
        "save_model 填 model_name/model_backend/model_context_window_tokens（provider_id 可放这里或顶层）。"
        "可选 reasoning_control 声明思考控制方式：auto/effort/budget/none，决定智能程度（/effort）怎样发送。"
        "可选 structured_output 声明结构化输出方式：auto/native/json_object（json_object 只适用于 OpenAI 兼容接口），"
        "决定记忆整理等后台结构化调用用哪种输出格式。"
    ),
    "editing": "save_provider/save_model 修改已存在记录时必须为 true；新建时省略。",
    "connection": (
        "discover/add_models 用：一个未保存的连接 {model_backend, api_base, api_key, 可选 custom_headers/session_header/display_name}；"
        "地址、密钥、请求头与会话头都相同时复用已有服务商。与 provider_id 二选一。"
    ),
    "models": (
        "add_models 必填：[{model_name, model_context_window_tokens, 可选 reasoning_levels}]，一次最多 200 个；"
        "同一服务商下已有的同名模型不重复添加。"
    ),
    "model_backend": "add_models 对已有服务商（provider_id）添加时必填：接口类型。",
    "enabled": "set_shared 必填：true 开放共享，false 撤销。",
}
_PROVIDER_SCHEMA = {
    "type": "object",
    "properties": {
        "display_name": {"type": "string"},
        "api_base": {"type": "string"},
        "api_key": {"type": "string"},
        "enabled": {"type": "boolean"},
        "capabilities": {"type": "array", "items": {"type": "string", "enum": sorted(CAPABILITIES)}},
        "custom_headers": {"type": "object"},
        "session_header": {"type": "string"},
    },
    "additionalProperties": False,
}
_PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "provider_id": {"type": "string"},
        "model_name": {"type": "string"},
        "model_backend": {"type": "string", "enum": sorted(BACKENDS)},
        "api_base": {"type": "string"},
        "api_key": {"type": "string"},
        "model_context_window_tokens": {"type": "integer", "minimum": 4096},
        "capability": {"type": "string", "enum": sorted(CAPABILITIES)},
        "enabled": {"type": "boolean"},
        "temperature": {"type": "number"},
        "top_p": {"type": "number"},
        "model_queue_wait_seconds": {"type": "integer", "minimum": 0},
        "usage_tags": {"type": "array", "items": {"type": "string"}},
        "input_modalities": {"type": "array", "items": {"type": "string"}},
        # 服务商声明的思考档位（如 low/medium/high/xhigh/max）；Responses 接口用它对应 /effort 档位。
        "reasoning_levels": {"type": "array", "items": {"type": "string"}},
        # 思考控制方式；取值与 backends/reasoning_control.REASONING_CONTROLS 一致，缺省 auto。
        "reasoning_control": {"type": "string", "enum": list(REASONING_CONTROLS)},
        # 结构化输出方式；取值与 backends/structured_output_mode.STRUCTURED_OUTPUT_MODES 一致，缺省 auto。
        "structured_output": {"type": "string", "enum": list(STRUCTURED_OUTPUT_MODES)},
    },
    "additionalProperties": False,
}
_CONNECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "model_backend": {"type": "string", "enum": sorted(BACKENDS)},
        "api_base": {"type": "string"},
        "api_key": {"type": "string"},
        "custom_headers": {"type": "object"},
        "session_header": {"type": "string"},
        "display_name": {"type": "string"},
    },
    "additionalProperties": False,
}
_MODELS_SCHEMA = {
    "type": "array",
    "maxItems": 200,
    "items": {
        "type": "object",
        "properties": {
            "model_name": {"type": "string"},
            "model_context_window_tokens": {"type": "integer", "minimum": 4096},
            "reasoning_levels": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["model_name", "model_context_window_tokens"],
        "additionalProperties": False,
    },
}
_PARAMETER_SCHEMA = {
    "action": {"type": "string", "enum": list(_ACTIONS)},
    "profile_id": {"type": "string"},
    "provider_id": {"type": "string"},
    "provider": _PROVIDER_SCHEMA,
    "profile": _PROFILE_SCHEMA,
    "editing": {"type": "boolean"},
    "connection": _CONNECTION_SCHEMA,
    "models": _MODELS_SCHEMA,
    "model_backend": {"type": "string", "enum": sorted(BACKENDS)},
    "enabled": {"type": "boolean"},
}
# 回执里删掉的投影字段：thread_id 是宿主内部编号，can_share 只与管理员共享菜单有关。
_DROP_RESULT_FIELDS = ("thread_id", "can_share")
_HINTS = {
    "add": "已保存服务商和模型；本会话立即使用请 select，新会话默认使用请 set_default。",
    "save_provider": "服务商已保存；如需该服务商下的模型请 save_model。",
    "save_model": "模型已保存；本会话立即使用请 select，新会话默认使用请 set_default。",
    "select": "本会话已切换；正在执行的工作片和已有子代理保持原模型。",
    "set_default": "新会话默认模型已更新；当前会话如需立即切换请 select。",
    "delete_model": "模型已删除。",
    "delete_provider": "服务商已删除，其密钥不可恢复。",
    "add_models": "added_models 是实际新增的模型；已存在的同名模型没有重复添加（带了思考档位时只刷新档位）。本会话使用请 select。",
    "set_shared": "共享设置已保存；所有会话的模型选择都没有改动。",
    "set_initial": "其他用户的初始模型已更新（设置时同时开放共享）；只影响没自己选过模型的普通用户。",
}


@dataclass(frozen=True)
class _ModelToolRequest:
    """Validated structured parameters for one manage_models action."""

    action: str
    operation: str
    payload: dict = field(default_factory=dict)


# LLM: 模型侧 schema 与 _PARAMETERS 文案同源；新增 action 时同步 _ACTIONS、effect 映射与测试。
# 函数用途: 组装 manage_models 的模型合同（名称、说明、参数 schema 和使用提示）。
def build_manage_models_model_spec() -> ToolModelSpec:
    properties = copy.deepcopy(_PARAMETER_SCHEMA)
    for name, description in _PARAMETERS.items():
        properties[name]["description"] = description
    return ToolModelSpec(
        name=TOOL_NAME,
        description=_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": ["action"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="capability",
            use_cases=_USE_CASES,
            avoid_when=_AVOID_WHEN,
            keywords=_KEYWORDS,
            examples=_EXAMPLES,
            default_deferred=True,
            deferred_summary="新增、切换、删除或测试模型与服务商，含语义记忆的向量模型（等同 /model）",
        ),
    )


# LLM: effect 由 action 结构化解析：list/probe/discover 只读，delete_provider dangerous，其余 mutating；审批走默认
#   dangerous 门，因此只有删服务商需要用户确认，其余由 owner 审批模式统一裁决。不得按自然语言猜测用户授权。
# 类用途: 把 TUI /model 的全部管理动作暴露给主会话代理，用户口头要求即可增删改切模型。
class ManageModelsTool(BaseTool):
    model_spec = build_manage_models_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy(
            "mutating",
            by_parameter=((
                "action",
                (
                    ("list", "read_only"),
                    ("probe", "read_only"),
                    ("discover", "read_only"),
                    ("delete_provider", "dangerous"),
                ),
            ),),
        ),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(
            parameter_names=("profile_id", "provider_id"),
            parameter_kinds={"profile_id": "logical", "provider_id": "logical"},
        ),
    )

    def __init__(self, agent: SimpleAgent):
        self.agent = agent

    # LLM: 子代理只能继承或按 create_subagents.model 指定模型，不得改写 owner 目录；主会话内始终就绪。
    # 函数用途: 子代理运行里隐藏本工具，避免下级代理替用户改模型目录。
    def availability(self) -> ToolAvailability:
        if current_subagent_run_id(self.agent):
            return ToolAvailability.unavailable("owner model catalog is managed only by the main conversation agent")
        return ToolAvailability.ready()

    # LLM: 副作用：add/save_*/set_default/delete_* 写 owner 的 model-profiles 文件；select 写当前会话线程记录；
    #   probe/discover 向服务商发一次网络请求；set_embedding/disable_embedding 写管理员用户配置与修改账本（全局，重启生效）。
    #   目录写入都在 model_profile_write_check(catalog_write_check(agent)) 里做：会改变当前向量模型端点主机的写入在落盘前被拒
    #   （be 复审 M1），回 needs_user_choice / EMBEDDING_HOST_DIFFERS。ModelProfileError 都在落盘前抛出，标记 not_started；
    #   其余异常保留未知副作用。
    # 函数用途: 校验参数、解析当前会话、调用唯一的模型配置服务，并返回不含密钥的回执。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        request = _parse_request(params)
        if isinstance(request, ToolHandlerOutcome):
            return request
        if request.action in _EMBEDDING_ACTIONS:
            return _embedding_outcome(self.agent, request)
        thread_id = _current_thread_id(self.agent)
        if request.action == "select" and not thread_id:
            return _err(
                "当前上下文没有会话，不能切换会话模型；请改用 set_default 设置新会话默认模型。",
                "MODEL_PROFILE_NO_THREAD",
                effect_outcome="not_started",
            )
        try:
            with model_profile_write_check(catalog_write_check(self.agent)):
                result = execute_model_profile_operation(
                    self.agent, request.operation, request.payload, thread_id=thread_id,
                )
        except ModelProfileError as exc:
            if exc.reason == EMBEDDING_HOST_CHANGE_REASON:
                return _embedding_host_change_outcome(request, str(exc))
            return _err(str(exc), "MODEL_PROFILE_INVALID", effect_outcome="not_started")
        except (OSError, ValueError) as exc:
            # 不回显异常正文：底层错误可能带上请求地址或配置片段。
            return _err(f"模型配置读写失败: {type(exc).__name__}", "TOOL_EXECUTION_FAILED")
        if request.action == "list":
            result = {**result, **_semantic_memory_block(self.agent)}
        return _outcome(request, result)


# LLM: 只做结构整理，不查目录、不发请求；新建 add/save_model 缺 profile_id 时由这里生成 UUID，避免模型自造编号。
# 函数用途: 把工具参数整理成唯一配置服务认识的 operation + payload，参数不合法时返回可纠正的错误。
def _parse_request(params: dict[str, object]) -> _ModelToolRequest | ToolHandlerOutcome:
    action = str(params.get("action") or "").strip().lower()
    if action not in _ACTIONS:
        return _err("action 须为 " + "/".join(_ACTIONS), "TOOL_INVALID_ARGUMENTS", effect_outcome="not_started")
    profile_id = str(params.get("profile_id") or "").strip()
    provider_id = str(params.get("provider_id") or "").strip()
    editing = params.get("editing") is True
    if action == "list":
        return _ModelToolRequest(action, "list")
    if action in _EMBEDDING_ACTIONS:
        if action == "set_embedding" and not profile_id:
            return _missing(action, "profile_id")
        return _ModelToolRequest(action, action, {"profile_id": profile_id} if profile_id else {})
    if action in _PROFILE_ID_ACTIONS:
        if not profile_id:
            return _missing(action, "profile_id")
        return _ModelToolRequest(action, action, {"profile_id": profile_id})
    if action in {"discover", "add_models", "set_shared", "set_initial"}:
        return _catalog_request(action, params, profile_id=profile_id, provider_id=provider_id)
    if action in _PROVIDER_ID_ACTIONS:
        if not provider_id:
            return _missing(action, "provider_id")
        payload: dict[str, object] = {"provider_id": provider_id}
        if action == "save_provider":
            provider = params.get("provider")
            if not isinstance(provider, dict):
                return _missing(action, "provider")
            payload.update(provider=provider, editing=editing)
        return _ModelToolRequest(action, action, payload)
    profile = params.get("profile")
    if not isinstance(profile, dict):
        return _missing(action, "profile")
    if action == "add":
        return _ModelToolRequest(action, "add", {"profile_id": profile_id or str(uuid4()), "profile": profile})
    if editing and not profile_id:
        return _err("save_model 编辑已有模型必须提供 list 返回的 profile_id", "TOOL_INVALID_ARGUMENTS", effect_outcome="not_started")
    model = {**profile, "provider_id": provider_id or profile.get("provider_id")}
    return _ModelToolRequest(action, "save_model", {"profile_id": profile_id or str(uuid4()), "profile": model, "editing": editing})


# LLM: discover/add_models 的来源二选一（未保存的 connection 或已有 provider_id）；models 的模型编号由这里生成 UUID，
#   不让模型自造编号；set_shared/set_initial 只整理结构化字段，管理员权限由宿主 set_shared_profile/set_initial_profile 校验。
# 函数用途: 把连接、批量添加、共享与初始模型四个动作整理成配置服务认识的 payload。
def _catalog_request(action: str, params: dict[str, object], *, profile_id: str, provider_id: str):
    if action in {"set_shared", "set_initial"}:
        if action == "set_shared" and (not profile_id or not isinstance(params.get("enabled"), bool)):
            return _missing(action, "profile_id 和 enabled")
        payload = {"profile_id": profile_id} if action == "set_initial" else {"profile_id": profile_id, "enabled": params["enabled"]}
        return _ModelToolRequest(action, action, payload)
    connection = params.get("connection")
    if (connection is None) == (not provider_id):
        return _err(f"{action} 需要 connection 或 provider_id 其中一个", "TOOL_INVALID_ARGUMENTS", effect_outcome="not_started")
    source = {"connection": connection} if connection is not None else {"provider_id": provider_id}
    if action == "discover":
        return _ModelToolRequest(action, "discover", source)
    models = params.get("models")
    if not isinstance(models, list) or not models:
        return _missing(action, "models")
    if provider_id:
        source["model_backend"] = params.get("model_backend")
    items = [{**item, "profile_id": str(uuid4())} for item in models if isinstance(item, dict)]
    return _ModelToolRequest(action, "add_models", {**source, "models": items})


# LLM: 向量模型两个动作的回执：成功照实带上“重启 Gateway 后生效”；边界拒绝（非管理员、共享或不在本人目录的档案）按
#   TOOL_PERMISSION_DENIED（与 user_config 同口径）；主机不同返回 EMBEDDING_HOST_DIFFERS 和 needs_user_choice。这两种在写之前
#   就停下，标 not_started；参数中心写入失败按 TOOL_EXECUTION_FAILED、原码留在 reported_error_code，副作用未知（可能已写一步），
#   交给对账。不回显密钥或接口地址。
# 函数用途: 执行 set_embedding/disable_embedding 并整理成工具回执。
def _embedding_outcome(agent: object, request: _ModelToolRequest) -> ToolHandlerOutcome:
    try:
        result = (model_set_embedding(agent, request.payload["profile_id"]) if request.action == "set_embedding"
                  else model_disable_embedding(agent))
    except ModelProfileError as exc:
        code = "TOOL_PERMISSION_DENIED" if exc.reason == "profile_not_found" else "MODEL_PROFILE_INVALID"
        return _err(str(exc), code, effect_outcome="not_started")
    body = json.dumps({"action": request.action, **result}, ensure_ascii=False)
    if result.get("ok"):
        return ToolHandlerOutcome(TOOL_NAME, True, body)
    reported = str(result.get("error_code") or "")
    refusal = _EMBEDDING_REFUSALS.get(reported)
    if refusal:
        return ToolHandlerOutcome(TOOL_NAME, False, body, error_code=refusal, effect_outcome="not_started",
                                  failure_stage="validation")
    return ToolHandlerOutcome(TOOL_NAME, False, body, error_code="TOOL_EXECUTION_FAILED",
                              reported_error_code=reported or "TOOL_EXECUTION_FAILED")


# LLM: 目录写入会动到当前向量模型端点主机时（catalog_write_check 拒绝）的回执：和 set_embedding 主机不同同口径——
#   needs_user_choice + EMBEDDING_HOST_DIFFERS，目录没有落盘（not_started）。
# 函数用途: 告诉模型这次修改没做，请用户自己在 /model 里改。
def _embedding_host_change_outcome(request: _ModelToolRequest, message: str) -> ToolHandlerOutcome:
    body = {"action": request.action, "ok": False, "status": "needs_user_choice", "error_code": "EMBEDDING_HOST_DIFFERS",
            "message": message}
    return ToolHandlerOutcome(TOOL_NAME, False, json.dumps(body, ensure_ascii=False), error_code="EMBEDDING_HOST_DIFFERS",
                              effect_outcome="not_started", failure_stage="validation")


# LLM: list 回执的语义记忆视图（只给本机管理员）；视图读不出来时省略，不影响模型列表本身。只读。
# 函数用途: 取 list 要附带的 semantic_memory 字段，没有时返回空字典。
def _semantic_memory_block(agent: object) -> dict:
    try:
        view = semantic_memory_view(agent)
    except (ModelProfileError, OSError, ValueError):
        return {}
    return {"semantic_memory": view} if view is not None else {}


def _missing(action: str, name: str) -> ToolHandlerOutcome:
    return _err(f"{action} 必须提供 {name}", "TOOL_INVALID_ARGUMENTS", effect_outcome="not_started")


# LLM: 会话身份只读 task_attributes 的结构化 conversation_thread_id，不从用户文本或工具参数接受会话编号。
# 函数用途: 取当前会话线程编号；独立命令或无会话上下文返回空串。
def _current_thread_id(agent: object) -> str:
    return str(current_conversation_task_attributes(agent).get("conversation_thread_id") or "").strip()


# LLM: 投影来自 public_model_profiles，本身不含密钥；probe/discover 的 ok=False 是服务商侧事实，映射成工具失败码。
# 函数用途: 把配置服务的返回整理成给模型看的回执，附带下一步提示。
def _outcome(request: _ModelToolRequest, result: dict) -> ToolHandlerOutcome:
    body = {key: value for key, value in result.items() if key not in _DROP_RESULT_FIELDS}
    body["action"] = request.action
    if request.action in {"probe", "discover"}:
        if body.get("ok") is False:
            code = "MODEL_PROBE_FAILED" if request.action == "probe" else "MODEL_DISCOVER_FAILED"
            return ToolHandlerOutcome(TOOL_NAME, False, json.dumps(body, ensure_ascii=False), error_code=code)
        return ToolHandlerOutcome(TOOL_NAME, True, json.dumps(body, ensure_ascii=False))
    for name in ("profile_id", "provider_id"):
        if request.payload.get(name):
            body[name] = request.payload[name]
    body["ok"] = True
    if request.action in _HINTS:
        body["hint"] = _HINTS[request.action]
    return ToolHandlerOutcome(TOOL_NAME, True, json.dumps(body, ensure_ascii=False))


# LLM: 只有调用方证明未落盘时才标 not_started；其余失败保留未知副作用，交给 ToolOperationCoordinator 对账。
# 函数用途: 返回统一格式的工具错误，文案不带参数值。
def _err(msg: str, code: str, *, effect_outcome: str = "") -> ToolHandlerOutcome:
    return ToolHandlerOutcome(
        TOOL_NAME,
        False,
        json.dumps({"error": msg}, ensure_ascii=False),
        error_code=code,
        effect_outcome=effect_outcome,
        failure_stage="validation" if effect_outcome == "not_started" else "",
    )


__all__ = ["ManageModelsTool", "TOOL_NAME", "build_manage_models_model_spec"]
