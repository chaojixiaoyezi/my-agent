# LLM: 参数中心的只读登记表：每个 AgentConfig 字段一条（键、默认值、值类型、中文说明、分类、安全等级、是否脱敏、生效时机）。
#   说明只取随包 agent_config.yaml：该键正上方的连续注释优先，没有再取该键的行尾注释（与加载器同一引号规则），不在代码里再写一份；
#   说明为空的字段只允许留在 test_parameter_registry 的基线名单里。安全等级按显式名单与键名记号结构化判定（指向类记号只对
#   非数字参数生效，凭据按键名最后的完整片段认、与回显脱敏同一条规则），
#   user_config_capability.TUNABLE_KEYS 可显式放行（如飞书凭据），BOUNDARY_KEYS 永远拒绝。分类只用于展示，未知前缀归“其它”，
#   不参与任何放行判断。只读，不写文件、不调模型。改动须同步 parameter_changes.py、tooling/user_config_tool.py、
#   gateway_parts/settings_control_service.py 与 test_parameter_registry.py。运行时还会按规则派生的参数（配置值不等于
#   实际使用值，如 max_tokens 按窗口夹取、推理强度在不支持的模型上不发送）在 _APPLIED_RULES 登记派生函数，公式本身仍只在原权威位置；
#   修改回执经 applied_value_with 按新值给出同一派生结果。常用层级（COMMON_KEYS，/settings 默认只列这些）是封闭的产品决策名单，
#   登记为 ParameterSpec.common；只影响展示与推荐顺序，不参与任何放行判断。
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
    TUNABLE_KEYS,
    is_credential_key,
    packaged_config_path,
)

SAFETY_FREE = "free"
SAFETY_BOUNDARY = "boundary"
# 参数来源文件（P17，2026-10-01）：登记表覆盖四份配置，写入按 source 选文件与正式加载器回读。
SOURCE_AGENT = "agent"
SOURCE_CAPABILITY = "capability"
SOURCE_RUNTIME_GUARD = "runtime_guard"
SOURCE_LOG_ANALYSIS = "log_analysis"
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
# （cli_audit_cleanup_days 缩短会提前删掉审计证据）或桌面控制的键，以及加载器写入的内部元数据（不是用户参数）。
_BOUNDARY_NAMES = frozenset({
    "api_base", "api_key", "model_backend", "computer_use_enabled", "execution_mode", "user_id", "system_prompt",
    "enable_tools", "enable_gateway_restart_tool", "enable_model_profile_tool", "gateway_restart_cooldown_seconds",
    "daemon_mutate_state", "daemon_start_runners",
    "config_layers", "config_sources", "config_warnings", "memory_config_warnings", "protect_running_runtime",
    "cli_audit_cleanup_days",
})
# 常用参数：/settings 默认只列这些，user_config 的搜索结果也标出来，方便先推荐。这是封闭的产品决策名单（2026-09-27 配置分类
# 的 common 层：99% 的用户只会碰到它们），不是开放世界的类型识别；其余参数仍可用 /settings all、search、show 查到和修改。
# 守卫见 test_parameter_registry：名单里的键必须存在、不能是安全边界项、必须有说明（过渡期例外单独登记）。顺序即展示顺序。
COMMON_KEYS = (
    "max_tokens", "request_timeout", "temperature", "model_reasoning_effort",
    "memory_compact_auto_trigger_percent", "conversation_history_max_turns", "memory_top_k", "memory_curator_enabled",
    "enable_self_learning", "enable_subagents", "max_subagents",
    "tool_shell_timeout", "tool_shell_output_max_chars", "tool_read_max_chars", "tool_list_max_entries",
    "tool_search_max_matches", "tool_web_max_chars", "tool_http_timeout", "tool_output_externalize_min_chars",
    "timezone", "tui_mouse_capture_default",
)
_CATEGORIES = (
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
    # 来源文件（P17，2026-10-01）：agent/capability/runtime_guard/log_analysis；写入口按它选目标文件与回读加载器。
    source: str = SOURCE_AGENT

    @property
    def writable(self) -> bool:
        return self.key not in BOUNDARY_KEYS and (self.safety == SAFETY_FREE or self.key in TUNABLE_KEYS)


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
    "enable_capability_package_selection": "只读已授权包摘要做选择，不执行包内脚本、不扩大工具权限",
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
    "decision_agent_timeout_min_seconds": "只约束 my-agent 自调 decision_patch 的等待下限，用户菜单不受限",
    "decision_agent_timeout_max_seconds": "只约束 my-agent 自调 decision_patch 的等待上限，用户菜单不受限",
    "session_pair_hourly_limit": "每对会话每小时消息上限（0 不限制），纯频控数量",
}


# LLM: 主配置沿用键名记号判定；其余来源默认边界，只有 _EXTRA_FREE_KEYS 显式名单放行。新来源键的 writable
#   只由这份安全等级与 BOUNDARY_KEYS/TUNABLE_KEYS 决定，不看说明文字。
# 函数用途: 按参数来源给安全等级（capability/runtime_guard/log_analysis 默认边界）。
def _safety_for(source: str, key: str, value_type: str) -> str:
    if source == SOURCE_AGENT:
        return classify_safety(key, value_type)
    return SAFETY_FREE if key in _EXTRA_FREE_KEYS else SAFETY_BOUNDARY


# LLM: 与 /settings、user_config、命令行 config-get 的回显脱敏同一条判定（user_config_capability.is_credential_key），
#   按完整片段认凭据名，不按子串。
# 函数用途: 判断一个键的值在回显和账本里是否必须脱敏。
def is_masked(key: str) -> bool:
    return is_credential_key(key)


# 函数用途: 按键名前缀给参数一个展示分类，未知前缀归“其它”。
def category_for(key: str) -> str:
    return next((name for prefixes, name in _CATEGORIES if key.startswith(prefixes)), "其它")


# LLM: 以字段默认值的真实类型为准（bool 先于 int 判断）；默认 None 或工厂值时退回注解文本。只读。
# 函数用途: 给出参数的值类型名（bool/int/float/str/list/dict/other）。
def _value_type(default: object, annotation: object) -> str:
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


# 函数用途: 从随包 YAML 读出每个键的中文说明。
def _yaml_descriptions() -> dict[str, str]:
    return _descriptions_from_lines(packaged_config_path().read_text(encoding="utf-8").splitlines())


# 函数用途: 返回另三份随包配置（capability/runtime_guard/log_analysis）的路径，与主配置同目录。
def _packaged_extra_config_path(source: str) -> Path:
    return packaged_config_path().with_name(f"{source}_config.yaml")


# LLM: 另三份配置没有 dataclass 的按 YAML 顶层键登记：说明、默认值、类型都从随包 YAML 取（与主配置同一注释规则）。
#   这些配置没有加载器元数据键，全部进登记表；effect 统一是重启生效（进程会缓存已加载配置）。
# 函数用途: 从一份随包 YAML 生成参数登记（runtime_guard / log_analysis 共用）。
def _yaml_source_specs(source: str) -> dict[str, ParameterSpec]:
    from .config_io import load_simple_yaml

    path = _packaged_extra_config_path(source)
    descriptions = _descriptions_from_lines(path.read_text(encoding="utf-8").splitlines())
    values = load_simple_yaml(path)
    return {
        key: ParameterSpec(
            key=key, default=value, value_type=_value_type(value, type(value)),
            description=descriptions.get(key, ""), category=category_for(key),
            safety=_safety_for(source, key, _value_type(value, type(value))),
            masked=is_masked(key), effect=EFFECT_GATEWAY_RESTART,
            common=False, unit=unit_for_key(key), source=source,
        )
        for key, value in values.items()
    }


# LLM: capability 配置有 CapabilityConfig dataclass：类型与默认值以 dataclass 为准（加载器会 coerce），
#   说明仍从随包 capability_config.yaml 注释取；config_warnings 是加载器元数据，不登记（主配置的同名键已登记）。
# 函数用途: 从 CapabilityConfig 与随包 YAML 生成 capability 来源的参数登记。
def _capability_specs() -> dict[str, ParameterSpec]:
    from ..capability.config import CapabilityConfig

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
            masked=is_masked(item.name), effect=EFFECT_GATEWAY_RESTART,
            common=False, unit=unit_for_key(item.name), source=SOURCE_CAPABILITY,
        )
    return specs


# LLM: 进程内缓存一次；四份随包配置或 dataclass 变化只随新版本发布生效，不需要热刷新。
# 函数用途: 返回全部参数的登记表（键 → ParameterSpec），主配置 + capability + runtime_guard + log_analysis。
@lru_cache(maxsize=1)
def parameter_registry() -> dict[str, ParameterSpec]:
    descriptions = _yaml_descriptions()
    range_texts = ranges()
    readers = field_readers()
    registry: dict[str, ParameterSpec] = {}
    for item in fields(AgentConfig):
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
    registry.update(_yaml_source_specs(SOURCE_RUNTIME_GUARD))
    registry.update(_yaml_source_specs(SOURCE_LOG_ANALYSIS))
    return registry


# LLM: 只读。展示层问“这个参数现在运行用的值”：主配置用调用方持有的 Gateway 启动配置；另三份配置从各自
#   随包默认文件读（用户配置里没有覆盖时就是实际运行值，读不到就回落登记默认）。不写文件、不调模型。
# 函数用途: 返回一个参数当前的运行值（按来源分派读取）。
def running_value(spec: ParameterSpec, config: object | None = None) -> object:
    if spec.source == SOURCE_AGENT:
        return getattr(config, spec.key, spec.default) if config is not None else spec.default
    if spec.source == SOURCE_CAPABILITY:
        from ..capability.config import load_capability_config

        try:
            return getattr(load_capability_config(_packaged_extra_config_path(SOURCE_CAPABILITY)), spec.key)
        except (OSError, ValueError):
            return spec.default
    if spec.source == SOURCE_RUNTIME_GUARD:
        from .runtime_guard_config import runtime_guard_data

        return runtime_guard_data().get(spec.key, spec.default)
    from .config_io import load_simple_yaml

    try:
        return load_simple_yaml(_packaged_extra_config_path(SOURCE_LOG_ANALYSIS)).get(spec.key, spec.default)
    except OSError:
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
    "SOURCE_LOG_ANALYSIS",
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
