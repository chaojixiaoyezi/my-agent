# LLM: 参数中心只读登记：主/能力参数按正式 schema，运行守卫只从 typed 部署策略取默认，不读旧守卫 YAML。
#   说明只取随包 agent_config.yaml：该键正上方的连续注释优先，没有再取该键的行尾注释（与加载器同一引号规则），不在代码里再写一份；
#   说明为空的字段只允许留在 test_parameter_registry 的基线名单里。安全等级按显式名单与键名记号结构化判定（指向类记号只对
#   非数字参数生效，凭据按键名最后的完整片段认、与回显脱敏同一条规则），
#   TUNABLE_KEYS 可显式放行（如飞书凭据），BOUNDARY_KEYS 对模型永远拒绝；整数收紧超时须显式登记，/settings 授权不改变 writable。
#   分类只用于展示，未知前缀归“其它”，
#   不参与任何放行判断。只读，不写文件、不调模型。改动须同步 parameter_changes.py、tooling/user_config_tool.py、
#   gateway_parts/settings_control_service.py 与 test_parameter_registry.py。运行时还会按规则派生的参数（配置值不等于
#   实际使用值，如 max_tokens 按窗口夹取、推理强度在不支持的模型上不发送）在 _APPLIED_RULES 登记派生函数，公式本身仍只在原权威位置；
#   修改回执经 applied_value_with 按新值给出同一派生结果。常用层级（COMMON_KEYS，/settings 默认只列这些）是封闭的产品决策名单，
#   登记为 ParameterSpec.common；只影响展示与推荐顺序，不参与任何放行判断。
#   capability 现读开关的时机只从 self_install_switches.CAPABILITY_FRESH_SWITCH_KEYS 取，不复制名单；assistant沟通参数只归展示分类，生效仍沿原配置链。
# 模块用途: 回答“有哪些参数、各是什么意思、谁能改、改了什么时候生效、实际用的是多少”，是参数中心的唯一登记来源。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import MISSING, dataclass, fields
from functools import lru_cache
from pathlib import Path

from .config import AgentConfig
from .config_io import yaml_trailing_comment
from .defaults import effective_max_output_tokens
from .parameter_metadata import field_readers, ranges, unit_for_key
from .user_config_capability import (
    BOUNDARY_KEYS,
    EFFECT_GATEWAY_RESTART,
    EFFECT_IMMEDIATE,
    TUNABLE_KEYS,
    is_credential_key,
    packaged_config_path,
)

SAFETY_FREE = "free"
SAFETY_BOUNDARY = "boundary"
# 参数来源文件（P17，2026-10-01）：登记表覆盖三份配置（log_analysis 已删除，2026-10-01），写入按 source 选文件与正式加载器回读。
SOURCE_AGENT = "agent"
SOURCE_CAPABILITY = "capability"
SOURCE_RUNTIME_GUARD = "runtime_guard"
# 359 决策表的 deployment_only，按 v2 显式把 gateway_user_inflight_limit 留在高级管理员层。
# 原表 99 项减这一项得到 98；不擅自迁移另一项来凑总数。运行时不依赖任务目录或生成副本。
DEPLOYMENT_ONLY_KEYS = frozenset({
    "workspace_root", "my_agent_home", "execution_mode", "my_agent_owner_provider", "my_agent_owner_kind",
    "my_agent_owner_id", "workspace_task_path_template", "memory_path", "local_store_path", "local_store_files_dir",
    "local_store_events_path", "local_store_fts_enabled", "self_dev_worktree", "protect_running_runtime",
    "subagent_workspace", "subagent_role_template_dirs", "self_learning_global_daily_limit", "user_id", "auth_enabled",
    "admin_user_id", "skill_guard_max_files", "skill_guard_max_size_kb", "model_speed_profile_path",
    "gateway_worker_process_count", "gateway_workspace", "gateway_stop_timeout", "gateway_request_timeout",
    "gateway_global_inflight_limit", "gateway_owner_inflight_limit", "enable_gateway_memory_diagnostics",
    "gateway_processing_timeout_seconds", "gateway_request_max_attempts", "sqlite_connection_reuse",
    "background_tick_cheap_checks", "background_idle_owner_max_backoff_seconds", "background_owner_workers",
    "background_threads_per_owner", "owner_agent_pool_max_agents", "owner_agent_idle_seconds", "input_media_max_bytes",
    "input_media_max_files", "input_image_max_pixels", "owner_maintenance_scan_interval_seconds",
    "gateway_ready_timeout_seconds", "gateway_restart_turn_wait_seconds", "gateway_restart_drain_timeout_seconds",
    "gateway_restart_force_on_drain_timeout", "gateway_restart_cooldown_seconds", "gateway_port",
    "gateway_per_user_owner_scoping", "admin_channel_identity_enabled", "gateway_bind_host", "gateway_auth_token",
    "adapter_workspace", "feishu_app_id", "feishu_app_secret", "feishu_verification_token", "feishu_encrypt_key",
    "feishu_callback_port", "feishu_connection_mode", "feishu_ws_proxy", "qq_app_id", "qq_app_secret", "session_workspace",
    "conversation_workspace", "background_claim_ttl_seconds", "collaboration_workspace",
    "collaboration_auto_dispatch_max_runners", "audit_enabled", "audit_log_path", "daemon_mutate_state",
    "daemon_start_runners", "daemon_runner_instruction", "lease_stale_without_heartbeat_seconds", "log_level",
    "extension_plugins", "provider_max_concurrent_requests", "responses_ws_idle_ttl_seconds",
    "responses_ws_max_idle_connections", "plugin_process_sandbox", "plugin_tool_gate_timeout_ms",
    "max_parallel_tool_calls_per_owner", "pty_session_idle_timeout_minutes", "owner_default_permission_mode",
    "tool_approval_timeout_seconds", "feishu_cli_backend_enabled", "feishu_user_oauth_enabled", "feishu_oauth_redirect_uri",
    "feishu_oauth_scopes", "im_model_cards_enabled", "im_group_authorization_required", "feishu_bot_subscriptions_enabled",
    "process_session_wait_slice_seconds", "max_parallel_tool_calls_global", "feishu_cloud_verify_sheet_writes_enabled",
    "decision_agent_timeout_min_seconds", "decision_agent_timeout_max_seconds", "session_pair_hourly_limit",
})
STRUCTURED_PARAMETER_KEYS = frozenset({"tool_catalog_categories", "tool_catalog_deferred_categories", "runner_timeout_by_role"})
# 键名按下划线切成记号，命中任一记号即为安全边界：凭据、权限与审批、管理员、访问与信任名单、网络与外部地址、端口、
# 请求头与回调、环境变量、锁（含飞书私聊闲置锁定这类访问控制时长）。宁可多拦：边界项只是模型不能改，用户仍可在宿主入口或配置文件里改。
_BOUNDARY_TOKENS = frozenset({
    "secret", "password", "encrypt", "credential", "credentials", "cookie", "cookies", "env",
    "sandbox", "permission", "permissions", "approval", "approvals", "auth", "admin", "access", "trusted",
    "dangerous", "allowed", "allow", "deny", "denied", "proxy", "host", "hosts", "port", "endpoint", "url", "urls",
    "network", "egress", "expose", "listen", "header", "headers", "webhook", "callback", "lock",
})
# 指向某处或某种能力的记号（路径与目录、身份、会起进程或加载代码的服务与插件、提示词、审计、令牌与键）：只对文本、列表、
# 开关类参数算边界；整数和小数只是上限、超时、间隔或数量，不会指向任何东西，不因名字里有 path/owner/prompt/token 被拦
# （原来 input_media_token_reserve、cli_audit_limit、background_owner_workers 等因此被误判为边界）。
_TARGET_TOKENS = frozenset({
    "token", "key", "keys", "root", "roots", "dir", "dirs", "path", "paths", "home", "workspace", "workspaces",
    "worktree", "worktrees", "command", "commands", "server", "servers", "plugin", "plugins", "extension", "extensions",
    "app", "provider", "prompt", "prompts", "instruction", "audit", "owner",
})
# 记号规则覆盖不到、但会改变模型流量去向、请求协议、执行权威链、工具可用性、写入保护、特权动作频率、审计记录保留期
# 或桌面控制的键，以及加载器写入的内部元数据（不是用户参数）。
# 收紧超时是整数，不会命中 plugin 指向记号；显式登记才能防止模型自行放大征询预算。
_BOUNDARY_NAMES = frozenset({
    "api_base", "api_key", "model_backend", "computer_use_enabled", "computer_use_observation_enabled", "execution_mode",
    "user_id", "system_prompt",
    # 物理请求资源上限由部署者设置；模型不能把0当作绕过共享并发额度的开关。
    "provider_max_concurrent_requests",
    "gateway_user_inflight_limit",
    "enable_tools", "enable_gateway_restart_tool", "enable_model_profile_tool", "enable_self_map", "gateway_restart_cooldown_seconds",
    "daemon_mutate_state", "daemon_start_runners",
    "config_layers", "config_sources", "config_warnings", "memory_config_warnings", "protect_running_runtime",
    "plugin_tool_gate_timeout_ms",
    # ptyleak：PTY 空闲兜底阈值是资源保护，模型不能把它调大或关掉（0=不限制），只能用户改配置文件。
    "pty_session_idle_timeout_minutes",
    # B7：M 线事件插件总开关是安全边界，只能用户经 /settings 改；显式列名，不靠键名记号猜。
    "plugin_events_enabled",
    # 老插件权限：老格式新授权默认收紧也是安全边界，只能用户经 /settings 改。
    "plugin_legacy_sandbox_default",
    # 规则来源及网络读取由用户控制；模型不能关闭规则来源或自行开启URL加载。
    "agents_source_enabled", "agents_source_network_enabled",
})
# 常用参数：/settings 默认只列这些，user_config 的搜索结果也标出来，方便先推荐。这是封闭的产品决策名单（2026-09-27 配置分类
# 的 common 层：99% 的用户只会碰到它们），不是开放世界的类型识别；其余参数仍可用 /settings all、search、show 查到和修改。
# 守卫见 test_parameter_registry：名单里的键必须存在、不能是安全边界项、必须有说明（过渡期例外单独登记）。顺序即展示顺序。
COMMON_KEYS = (
    "max_tokens", "request_timeout", "model_reasoning_effort",
    "memory_compact_trigger_percent", "conversation_history_max_turns", "memory_top_k", "memory_curator_enabled",
    "enable_self_learning", "enable_subagents", "max_subagents",
    "tool_shell_timeout", "tool_output_externalize_min_chars",
    "timezone", "tui_mouse_capture_default",
)
_CATEGORIES = (
    (("assistant_",), "沟通与展示"),
    (("max_tokens", "model_", "temperature", "top_p", "request_timeout", "stream_", "reasoning_", "anthropic_", "embedding_"),
     "模型请求"),
    (("memory_", "compact_"), "记忆与压缩"),
    (("decision_",), "决策模型 Jev"),
    (("tool_", "shell_", "web_", "read_", "write_"), "工具"),
    (("subagent", "max_subagents", "task_", "acceptance_"), "子代理与任务"),
    (("gateway_", "daemon_", "runner_", "lease_", "scheduler_"), "Gateway 与调度"),
    (("feishu_", "qq_", "wechat_", "im_", "adapter_"), "IM 通道"),
    (("skill", "self_learning", "enable_self_learning", "capability_"), "技能与自学习"),
)


# LLM: 一条参数的只读事实；writable 只由安全等级与显式名单决定，不看自然语言说明。
# 类用途: 参数中心里一个参数的完整登记信息。
@dataclass(frozen=True)
class ParameterSpec:
    key: str
    default: object
    value_type: str
    description: str
    category: str
    safety: str
    masked: bool
    effect: str
    # 是否属于常用层级（COMMON_KEYS）；只用于展示和推荐，不影响能否修改。
    common: bool = False
    # 元数据（P8，2026-10-01）：单位/范围/归属模块/读取方，全部由 parameter_metadata 自动推导；推不出的留空。
    unit: str = ""
    range: str = ""
    owner_module: str = ""
    reader: str = ""
    # 来源文件（P17，2026-10-01）：agent/capability/runtime_guard；写入口按它选目标文件与回读加载器。
    source: str = SOURCE_AGENT

    # LLM: 部署项与无 typed writer 项不宣称可写；模型许可还须通过原边界，展示事实不能授予写权。
    # 函数用途: 报告模型能否实际走参数写入口。
    @property
    def writable(self) -> bool:
        return bool(self.writer) and self.key not in BOUNDARY_KEYS and (self.safety == SAFETY_FREE or self.key in TUNABLE_KEYS)

    # LLM: 展示层级固定取权威封闭名单，不凭说明、actor 或键名臆测部署权限。
    # 函数用途: 区分常用、高级和宿主部署参数。
    @property
    def tier(self) -> str:
        if self.source == SOURCE_RUNTIME_GUARD or self.key in DEPLOYMENT_ONLY_KEYS:
            return "deployment"
        return "common" if self.common else "advanced"

    # LLM: 仅映射本模块与 parameter_changes 实际实现的路由；其它结构和部署来源没有普通参数 writer。
    # 函数用途: 返回实际支持的标量或结构化写路线。
    @property
    def writer(self) -> str:
        if self.tier == "deployment":
            return ""
        if self.key in STRUCTURED_PARAMETER_KEYS:
            return "structured"
        return "scalar" if self.value_type in {"str", "int", "float", "bool"} else ""

    # LLM: 认证用户范围仍只限旧显式白名单，部署层和缺 writer 的值不能用认证作用域绕过。
    # 函数用途: 投影已认证用户控制入口的可写事实。
    @property
    def user_editable(self) -> bool:
        from .user_config_capability import USER_SETTINGS_BOUNDARY_KEYS

        return bool(self.writer) and (self.writable or self.key in USER_SETTINGS_BOUNDARY_KEYS)


# LLM: 显式名单与凭据优先：BOUNDARY_KEYS、_BOUNDARY_NAMES、接口地址与凭据键（is_masked）一定是边界；其余按键名记号判定，
#   _TARGET_TOKENS 只对非数字参数生效（value_type 为 int/float 时不按指向类记号拦），缺省按非数字处理（更严）。
#   TUNABLE_KEYS 的放行在 writable 里处理。
# 函数用途: 判定一个配置键的安全等级。
def classify_safety(key: str, value_type: str = "") -> str:
    if key in BOUNDARY_KEYS or key in _BOUNDARY_NAMES or key.endswith(("api_base", "_url")) or is_masked(key):
        return SAFETY_BOUNDARY
    tokens = _BOUNDARY_TOKENS if value_type in {"int", "float"} else _BOUNDARY_TOKENS | _TARGET_TOKENS
    return SAFETY_BOUNDARY if set(key.split("_")) & tokens else SAFETY_FREE


# LLM: P17 起主配置之外的参数默认按安全边界（授权、权限、路径、执行权威链、运行时门、审批、白名单、执行开关、audit
#   相关一律不能由模型改），只有这里的显式名单按“能力相关的数值上限/纯展示开关”逐键放行并写明理由；宁严勿松，
#   拿不准的键名宁可按边界拒绝，不要猜着放行。列表值本来就拒绝聊天写入，不影响边界分类。
# 常量用途: capability_config.yaml 里可以由模型/聊天修改的键与理由（每个键必须有一句人话理由）。
_EXTRA_FREE_KEYS: dict[str, str] = {
    "enable_capability_package_recommendations": "只控制展示相关能力包摘要，不授予能力、不执行资源",
    "capability_package_selection_max_input_tokens": "选包请求的输入预算上限（0 不设独立配额），纯数量",
    "capability_candidate_limit": "推荐/选包共用的候选数上限，纯数量",
    "capability_bundle_max_tokens": "装配入口的 token 预算（0 仍受模型窗口限制），纯数量",
    "subagent_heartbeat_timeout": "心跳停滞检查秒数（0 关闭），纯超时",
    "subagent_run_timeout": "任务超时秒数（0 关闭），纯超时",
    "subagent_no_progress_attempt_limit": "无进展熔断次数（0 关闭），纯次数",
    "subagent_stream_activity_projection_enabled": "只写时间/阶段/字符计数、不保存正文的展示投影开关",
    "subagent_stream_activity_interval_seconds": "流活动投影最小间隔秒数，纯间隔",
    "subagent_activity_notices_enabled": "阶段提醒开关（复用心跳、不调模型、不判失败）",
    "subagent_first_token_notice_seconds": "首 token 提醒秒数（0 关闭），纯超时",
    "subagent_stream_idle_notice_seconds": "流静默提醒秒数（0 关闭），纯超时",
    "subagent_tool_wait_notice_seconds": "长工具提醒秒数（0 关闭），纯超时",
}


# LLM: 主配置沿用键名记号判定；其余来源默认边界，只有 _EXTRA_FREE_KEYS 显式名单放行。新来源键的 writable
#   只由这份安全等级与 BOUNDARY_KEYS/TUNABLE_KEYS 决定，不看说明文字。
# 函数用途: 按参数来源给安全等级（capability/runtime_guard 默认边界）。
def _safety_for(source: str, key: str, value_type: str) -> str:
    if source == SOURCE_AGENT:
        return classify_safety(key, value_type)
    return SAFETY_FREE if key in _EXTRA_FREE_KEYS else SAFETY_BOUNDARY


# LLM: 与 /settings、user_config、命令行 my-agent config get 的回显脱敏同一条判定（user_config_capability.is_credential_key），
#   按完整片段认凭据名，不按子串。
# 函数用途: 判断一个键的值在回显和账本里是否必须脱敏。
def is_masked(key: str) -> bool:
    return is_credential_key(key)


# LLM: 参数目录展示与写权限投影；只读目录不授配置写权，联测边界键与随包说明。
# 函数用途: 按键名前缀给参数一个展示分类，未知前缀归“其它”。
def category_for(key: str) -> str:
    return next((name for prefixes, name in _CATEGORIES if key.startswith(prefixes)), "其它")


# LLM: 以字段默认值的真实类型为准（bool 先于 int 判断）；默认 None 或工厂值时退回注解文本。只读。
# 函数用途: 给出参数的值类型名（bool/int/float/str/list/dict/other）。
def _value_type(default: object, annotation: object) -> str:
    if isinstance(default, tuple):
        return "list"
    for name, kind in (("bool", bool), ("int", int), ("float", float), ("str", str), ("list", list), ("dict", dict)):
        if isinstance(default, kind):
            return name
    text = str(annotation)
    return next((name for name in ("bool", "int", "float", "str", "list", "dict") if text.startswith(name)), "other")


# LLM: 顶层 `key:` 行正上方连续的 `#` 注释优先（遇空行或其它内容即止，同一注释块只归紧挨着的第一个键）；
#   没有正上方注释时取该行的行尾注释（yaml_trailing_comment，引号里的 # 不算）。同名键只取第一次出现。只读。
# 函数用途: 从 YAML 文本行里读出每个键的中文说明。
def _descriptions_from_lines(lines: list[str]) -> dict[str, str]:
    descriptions: dict[str, str] = {}
    block: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("#"):
            block.append(stripped.lstrip("#").strip())
            continue
        if not raw.startswith((" ", "\t")) and ":" in raw and stripped:
            above = " ".join(line for line in block if line)
            descriptions.setdefault(raw.split(":", 1)[0].strip(), above or yaml_trailing_comment(raw))
        block = []
    return descriptions


# LLM: 参数目录展示与写权限投影；只读目录不授配置写权，联测边界键与随包说明。
# 函数用途: 从随包 YAML 读出每个键的中文说明。
def _yaml_descriptions() -> dict[str, str]:
    return _descriptions_from_lines(packaged_config_path().read_text(encoding="utf-8").splitlines())


# LLM: 参数目录展示与写权限投影；只读目录不授配置写权，联测边界键与随包说明。
# 函数用途: 返回另两份随包配置（capability/runtime_guard）的路径，与主配置同目录。
def _packaged_extra_config_path(source: str) -> Path:
    return packaged_config_path().with_name(f"{source}_config.yaml")


# LLM: 守卫默认与说明只从 RuntimeGuardPolicy 字段取得，不维护 YAML 第二份定义；显示键不授 owner 覆盖权。
# 函数用途: 从唯一 typed 宿主策略生成守卫参数的只读投影。
def _runtime_guard_specs() -> dict[str, ParameterSpec]:
    from .deployment_policy import RuntimeGuardPolicy

    defaults = RuntimeGuardPolicy()
    return {
        item.name: ParameterSpec(
            key=item.name, default=getattr(defaults, item.name), value_type=_value_type(getattr(defaults, item.name), item.type),
            description=item.metadata.get("description", ""), category=category_for(item.name),
            safety=SAFETY_BOUNDARY, masked=False, effect=EFFECT_GATEWAY_RESTART,
            common=False, unit=unit_for_key(item.name), source=SOURCE_RUNTIME_GUARD,
        )
        for item in fields(RuntimeGuardPolicy) if item.init
    }


# LLM: capability 配置有 CapabilityConfig dataclass：类型与默认值以 dataclass 为准（加载器会 coerce），
#   说明仍从随包 capability_config.yaml 注释取；config_warnings 是加载器元数据，不登记（主配置的同名键已登记）。
#   生效时机默认报"重启 Gateway"；唯一现读名单 CAPABILITY_FRESH_SWITCH_KEYS 内的键如实报"马上生效"。
# 函数用途: 从 CapabilityConfig 与随包 YAML 生成 capability 来源的参数登记。
def _capability_specs() -> dict[str, ParameterSpec]:
    from ..capability.config import CapabilityConfig
    from ..capability.self_install_switches import CAPABILITY_FRESH_SWITCH_KEYS

    path = _packaged_extra_config_path(SOURCE_CAPABILITY)
    descriptions = _descriptions_from_lines(path.read_text(encoding="utf-8").splitlines())
    specs: dict[str, ParameterSpec] = {}
    for item in fields(CapabilityConfig):
        if item.name == "config_warnings":
            continue
        default = item.default if item.default is not MISSING else (
            item.default_factory() if item.default_factory is not MISSING else None)
        value_type = _value_type(default, item.type)
        specs[item.name] = ParameterSpec(
            key=item.name, default=default, value_type=value_type,
            description=descriptions.get(item.name, ""), category=category_for(item.name),
            safety=_safety_for(SOURCE_CAPABILITY, item.name, value_type),
            masked=is_masked(item.name),
            effect=EFFECT_IMMEDIATE if item.name in CAPABILITY_FRESH_SWITCH_KEYS else EFFECT_GATEWAY_RESTART,
            common=False, unit=unit_for_key(item.name), source=SOURCE_CAPABILITY,
        )
    return specs


# LLM: 进程内缓存一次；宿主专有 metadata 字段不属于参数目录，不能暴露凭据来源上界或让用户写入。
# 函数用途: 返回用户可见的参数登记表，主配置、capability 与 runtime_guard 仍沿原语义。
@lru_cache(maxsize=1)
def parameter_registry() -> dict[str, ParameterSpec]:
    descriptions = _yaml_descriptions()
    range_texts = ranges()
    readers = field_readers()
    registry: dict[str, ParameterSpec] = {}
    for item in fields(AgentConfig):
        if item.metadata.get("host_only", False):
            continue
        default = item.default if item.default is not MISSING else (
            item.default_factory() if item.default_factory is not MISSING else None)
        value_type = _value_type(default, item.type)
        owner_module, reader = readers.get(item.name, ("", ""))
        registry[item.name] = ParameterSpec(
            key=item.name, default=default, value_type=value_type,
            description=descriptions.get(item.name, ""), category=category_for(item.name),
            safety=classify_safety(item.name, value_type), masked=is_masked(item.name),
            effect=TUNABLE_KEYS[item.name].effect if item.name in TUNABLE_KEYS else EFFECT_GATEWAY_RESTART,
            common=item.name in COMMON_KEYS,
            unit=unit_for_key(item.name), range=range_texts.get(item.name, ""),
            owner_module=owner_module, reader=reader,
        )
    registry.update(_capability_specs())
    registry.update(_runtime_guard_specs())
    return registry


# LLM: 只读。展示层问“这个参数现在运行用的值”：主配置用调用方持有的 Gateway 启动配置；capability 读运行时
#   实际加载的那份文件（capability_path 由调用方按 capability_config_for_agent 同一路径解析，文件不存在时
#   运行时按默认实例走，这里也回落登记默认）；runtime_guard 只读宿主 deployment_policy.json 与 Python 默认。
#   显式 config 的宿主根不明或坏策略返回 unknown，不转读当前目录或 owner 文件。不写文件、不调模型。
# LLM: 部署字段沿用 configured_home_root 的宿主根优先级（含历史默认值的环境覆盖），忽略 owner capability_path；
#   无配置对象、非绝对根或坏配置返回 unknown，不从工作目录猜来源，也不冒充默认。
# 函数用途: 返回一个参数当前的运行值（按来源分派读取）。
def running_value(spec: ParameterSpec, config: object | None = None, *, capability_path: Path | None = None) -> object:
    if spec.source == SOURCE_AGENT:
        return getattr(config, spec.key, spec.default) if config is not None else spec.default
    if spec.source == SOURCE_CAPABILITY:
        from ..capability.runtime_config_reload import (
            DEPLOYMENT_CAPABILITY_KEYS,
            deployment_capability_config,
        )
        if spec.key in DEPLOYMENT_CAPABILITY_KEYS:
            from types import SimpleNamespace

            from ..user_space.home_root import configured_home_root
            try:
                root = configured_home_root(config) if config is not None else None
                if not root or not Path(root).expanduser().is_absolute():
                    return "unknown"
                return getattr(deployment_capability_config(SimpleNamespace(root=Path(root).expanduser())), spec.key)
            except (OSError, AttributeError, TypeError, ValueError):
                return "unknown"
        from ..capability.config import load_capability_config
        from ..capability.runtime_config_reload import resolve_capability_config_path

        path = resolve_capability_config_path(capability_path)
        try:
            return getattr(load_capability_config(path), spec.key)
        except (OSError, ValueError):
            return spec.default
    if spec.source == SOURCE_RUNTIME_GUARD:
        from ..user_space.home_layout import home_paths
        from ..user_space.home_root import configured_home_root
        from .deployment_policy import DeploymentPolicyError, load_deployment_policy

        if config is None:
            return spec.default
        root = configured_home_root(config)
        if not root or not Path(root).expanduser().is_absolute():
            return "unknown"
        try:
            return getattr(load_deployment_policy(trusted_host_paths=home_paths(root)).runtime_guards, spec.key)
        except (OSError, DeploymentPolicyError):
            return "unknown"
    return spec.default


# 加载器写进 AgentConfig 的元数据（配置文件路径、来源、分层、告警），不是用户参数：/settings 的列表与计数、参数搜索都不列出。
# 只是不显示——字段本身不删，登记表里仍保留（/settings show 仍能查看，仍是安全边界）。
LOADER_METADATA_KEYS = frozenset({"config_path", "config_sources", "config_layers", "config_warnings", "memory_config_warnings"})
# 由别的正式入口写入、用户不该手填的配置值（如 /model 登录生成的 OAuth 运行引用）：列表与搜索不列，避免诱导手改；
# 字段与登记照旧，/settings show 仍能查看，安全等级不变。参数减量收口（2026-10-01）时定。
MANAGED_ELSEWHERE_KEYS = frozenset({"model_auth_ref"})


# LLM: 列表视图（/settings 默认与全部视图的总数、分类清单，user_config 的可改数量，参数搜索）统一用这份：
#   去掉 LOADER_METADATA_KEYS 与 MANAGED_ELSEWHERE_KEYS，其余与登记表同序同值。单个参数的查看、修改仍走
#   parameter_registry。只读。改动隐藏集合时同步 test_settings_chat_control 的列表/搜索/show 断言。
# 函数用途: 返回会在 /settings 列表和搜索里出现的参数（不含加载器元数据和由其它入口管理的值）。
def listed_parameters() -> dict[str, ParameterSpec]:
    hidden = LOADER_METADATA_KEYS | MANAGED_ELSEWHERE_KEYS
    return {key: spec for key, spec in parameter_registry().items() if key not in hidden}


# LLM: 按 COMMON_KEYS 的顺序返回常用参数的登记信息；名单里不存在的键直接跳过（守卫测试会先失败），不猜替代项。只读。
# 函数用途: 给 /settings 默认视图列出常用参数。
def common_parameters() -> list[ParameterSpec]:
    registry = parameter_registry()
    return [registry[key] for key in COMMON_KEYS if key in registry]


# LLM: 关键词同时匹配键名与说明（不区分大小写）；键名命中排前，其中完全相同、再以关键词开头、再短键名优先；只读，limit 为 0 不限。
#   只搜 listed_parameters（加载器元数据不出现在结果里）。
# 函数用途: 按关键词查找参数，给用户和模型“这个参数叫什么、在哪”的答案。
def search_parameters(query: str, *, limit: int = 20) -> list[ParameterSpec]:
    needle = str(query or "").strip().lower()
    specs = list(listed_parameters().values())
    if not needle:
        return specs[:limit] if limit else specs
    by_key = sorted((spec for spec in specs if needle in spec.key.lower()),
                    key=lambda spec: (spec.key != needle, not spec.key.startswith(needle), len(spec.key)))
    by_text = [spec for spec in specs if needle not in spec.key.lower() and needle in spec.description.lower()]
    found = by_key + by_text
    return found[:limit] if limit else found


# LLM: 与 /effort 回执同一入口 backends.reasoning_control.describe_config_reasoning_effect（控制方式、协议与服务商档位
#   都从这份配置解析，与发送同一换算），这里不另写支持判断；档位取配置里的全局默认（会话里用 /effort 单独设过的
#   以会话为准，这里不读线程）。只读。
# 函数用途: 说明全局推理强度档位在这个模型上实际会怎样发送，例如当前模型不支持调节时本设置不改变请求。
def _reasoning_effect(config: object) -> str:
    from ..backends.reasoning_control import describe_config_reasoning_effect
    from .reasoning_effort import configured_reasoning_level

    return describe_config_reasoning_effect(configured_reasoning_level(config), config)


# 运行时按规则派生的参数：登记“由配置算出实际使用值”的唯一函数与一句中文规则；公式不在这里另写。
_APPLIED_RULES: dict[str, tuple[Callable[[object], object], str]] = {
    "max_tokens": (effective_max_output_tokens, "按模型上下文窗口夹取：不超过窗口 ÷ 4，窗口没填时按 128000 算"),
    "model_reasoning_effort": (_reasoning_effect, "按当前模型的思考控制方式换算：不支持调节的模型不发送任何推理参数；"
                                                  "会话里用 /effort 单独设过的以会话为准"),
}
# 修改回执里的新值是渲染后的文本；按登记类型转回，派生规则拿到的类型与重启后加载的一致
_TEXT_TO_TYPE: dict[str, Callable[[str], object]] = {
    "int": int, "float": float, "bool": lambda text: text.strip().lower() == "true",
}


# LLM: 只对 _APPLIED_RULES 登记的参数返回 (实际使用值, 规则说明)，其余返回 None；config 决定按哪个模型的窗口算
#   （user_config 工具传本片会话模型的配置，/settings 传 Gateway 启动配置即默认模型）。只读；派生失败返回 None，不猜值。
# 函数用途: 给查看入口一个“配置值之外实际使用的值”，例如 64K 的 max_tokens 在 128K 窗口模型上实际是 32768。
def applied_value(key: str, config: object) -> tuple[object, str] | None:
    rule = _APPLIED_RULES.get(key)
    if rule is None or config is None:
        return None
    reader, text = rule
    try:
        return reader(config), text
    except (TypeError, ValueError):
        return None


# LLM: 只读视图：替换的那个键取给定值，其余属性原样取自底层配置；不复制、不校验、不写回。
# 类用途: 让派生规则按“假设已经改成新值”的配置计算。
class _ConfigWithValue:
    # LLM: 三个内部属性在构造时直接写入实例，__getattr__ 只在找不到属性时才被调用，不会递归。
    # 函数用途: 记住底层配置与要替换的一个键值。
    def __init__(self, base: object, key: str, value: object) -> None:
        self._base, self._key, self._value = base, key, value

    # LLM: 只拦被替换的键；底层没有的属性照常抛 AttributeError，由派生规则自己的默认处理。
    # 函数用途: 被替换的键返回新值，其余属性转给底层配置。
    def __getattr__(self, name: str) -> object:
        return self._value if name == self._key else getattr(self._base, name)


# LLM: 只对 _APPLIED_RULES 登记的参数计算；新值按登记类型从回执文本转回，转不了返回 None，不猜。config 决定按哪个模型算
#   （user_config 传本片会话模型的配置，/settings 传 Gateway 启动配置即默认模型）。只读，不写配置。
# 函数用途: 给修改回执算出“按新值在这个模型上的实际效果”，让改了但当前模型不起作用的情况当下就能看见。
def applied_value_with(key: str, value: object, config: object) -> tuple[object, str] | None:
    spec = parameter_registry().get(key)
    if key not in _APPLIED_RULES or spec is None or config is None:
        return None
    try:
        typed = _TEXT_TO_TYPE.get(spec.value_type, str)(str(value))
    except ValueError:
        return None
    return applied_value(key, _ConfigWithValue(config, key, typed))


__all__ = [
    "COMMON_KEYS",
    "SAFETY_BOUNDARY",
    "SAFETY_FREE",
    "SOURCE_AGENT",
    "SOURCE_CAPABILITY",
    "SOURCE_RUNTIME_GUARD",
    "ParameterSpec",
    "applied_value",
    "applied_value_with",
    "category_for",
    "classify_safety",
    "common_parameters",
    "is_masked",
    "parameter_registry",
    "running_value",
    "search_parameters",
]
