# LLM: 参数中心的只读登记表：每个 AgentConfig 字段一条（键、默认值、值类型、中文说明、分类、安全等级、是否脱敏、生效时机）。
#   说明只取随包 agent_config.yaml 里该键正上方的连续注释，不在代码里再写一份；安全等级按显式名单与键名记号结构化判定，
#   user_config_capability.TUNABLE_KEYS 可显式放行（如飞书凭据），BOUNDARY_KEYS 永远拒绝。分类只用于展示，未知前缀归“其它”，
#   不参与任何放行判断。只读，不写文件、不调模型。改动须同步 parameter_changes.py、tooling/user_config_tool.py、
#   gateway_parts/settings_control_service.py 与 test_parameter_registry.py。
# 模块用途: 回答“有哪些参数、各是什么意思、谁能改、改了什么时候生效”，是参数中心的唯一登记来源。
from __future__ import annotations

from dataclasses import MISSING, dataclass, fields
from functools import lru_cache

from .config import AgentConfig
from .user_config_capability import (
    BOUNDARY_KEYS,
    EFFECT_GATEWAY_RESTART,
    TUNABLE_KEYS,
    packaged_config_path,
)

SAFETY_FREE = "free"
SAFETY_BOUNDARY = "boundary"
# 键名按下划线切成记号，命中任一记号即为安全边界：凭据、权限与审批、身份、路径与目录、外部地址与端口、会起进程或加载代码的
# 服务与插件、网络放行名单。宁可多拦：边界项只是模型不能改，用户仍可在宿主入口或配置文件里改。
_BOUNDARY_TOKENS = frozenset({
    "secret", "token", "password", "encrypt", "credential", "credentials", "cookie", "cookies", "key", "keys", "env",
    "sandbox", "permission", "permissions", "approval", "approvals", "auth", "admin", "owner", "access", "trusted",
    "dangerous", "allowed", "allow", "deny", "denied", "root", "roots", "dir", "dirs", "path", "paths", "home",
    "workspace", "workspaces", "worktree", "worktrees", "command", "commands", "server", "servers", "plugin", "plugins", "extension",
    "extensions", "proxy", "host", "hosts", "port", "endpoint", "url", "urls", "network", "egress", "expose", "listen",
    "header", "headers", "webhook", "callback", "app", "provider", "prompt", "prompts", "instruction", "audit", "lock",
})
_SECRET_TOKENS = frozenset({"secret", "token", "password", "encrypt", "credential", "credentials", "cookie", "cookies"})
# 记号规则覆盖不到、但会改变模型流量去向、请求协议、执行权威链、工具可用性、特权动作频率或桌面控制的键，
# 以及加载器写入的内部元数据（不是用户参数）。
_BOUNDARY_NAMES = frozenset({
    "api_base", "api_key", "model_backend", "computer_use_enabled", "execution_mode", "user_id", "system_prompt",
    "enable_tools", "enable_gateway_restart_tool", "enable_model_profile_tool", "gateway_restart_cooldown_seconds",
    "result_check_execute_tests", "daemon_mutate_state", "daemon_start_runners", "external_knowledge_api_sources",
    "external_knowledge_database_sources", "external_knowledge_index_file_name",
    "config_layers", "config_sources", "config_warnings", "memory_config_warnings",
})
_CATEGORIES = (
    (("max_tokens", "model_", "temperature", "top_p", "request_timeout", "stream_", "reasoning_", "anthropic_"), "模型请求"),
    (("memory_", "compact_"), "记忆与压缩"),
    (("decision_",), "决策模型 Jev"),
    (("tool_", "shell_", "web_", "read_", "write_"), "工具"),
    (("subagent", "max_subagents", "task_", "result_check_", "acceptance_"), "子代理与任务"),
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

    @property
    def writable(self) -> bool:
        return self.key not in BOUNDARY_KEYS and (self.safety == SAFETY_FREE or self.key in TUNABLE_KEYS)


# LLM: 显式名单优先：BOUNDARY_KEYS 与 _BOUNDARY_NAMES 一定是边界；其余按键名记号判定。TUNABLE_KEYS 的放行在 writable 里处理。
# 函数用途: 判定一个配置键的安全等级。
def classify_safety(key: str) -> str:
    if key in BOUNDARY_KEYS or key in _BOUNDARY_NAMES or key.endswith(("api_base", "_url")):
        return SAFETY_BOUNDARY
    return SAFETY_BOUNDARY if set(key.split("_")) & _BOUNDARY_TOKENS else SAFETY_FREE


# 函数用途: 判断一个键的值在回显和账本里是否必须脱敏。
def is_masked(key: str) -> bool:
    return bool(set(key.split("_")) & _SECRET_TOKENS) or key.endswith("api_key")


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


# LLM: 只认顶层 `key:` 行正上方连续的 `#` 注释，遇空行或其它内容即止；同一注释块只归紧挨着的第一个键。只读。
# 函数用途: 从随包 YAML 读出每个键的中文说明。
def _yaml_descriptions() -> dict[str, str]:
    descriptions: dict[str, str] = {}
    block: list[str] = []
    for raw in packaged_config_path().read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            block.append(stripped.lstrip("#").strip())
            continue
        if not raw.startswith((" ", "\t")) and ":" in raw and stripped:
            descriptions.setdefault(raw.split(":", 1)[0].strip(), " ".join(line for line in block if line))
        block = []
    return descriptions


# LLM: 进程内缓存一次；AgentConfig 字段或随包 YAML 变化只随新版本发布生效，不需要热刷新。
# 函数用途: 返回全部参数的登记表（键 → ParameterSpec）。
@lru_cache(maxsize=1)
def parameter_registry() -> dict[str, ParameterSpec]:
    descriptions = _yaml_descriptions()
    registry: dict[str, ParameterSpec] = {}
    for item in fields(AgentConfig):
        default = item.default if item.default is not MISSING else (
            item.default_factory() if item.default_factory is not MISSING else None)
        registry[item.name] = ParameterSpec(
            key=item.name, default=default, value_type=_value_type(default, item.type),
            description=descriptions.get(item.name, ""), category=category_for(item.name),
            safety=classify_safety(item.name), masked=is_masked(item.name),
            effect=TUNABLE_KEYS[item.name].effect if item.name in TUNABLE_KEYS else EFFECT_GATEWAY_RESTART,
        )
    return registry


# LLM: 关键词同时匹配键名与说明（不区分大小写）；键名命中排前，其中完全相同、再以关键词开头、再短键名优先；只读，limit 为 0 不限。
# 函数用途: 按关键词查找参数，给用户和模型“这个参数叫什么、在哪”的答案。
def search_parameters(query: str, *, limit: int = 20) -> list[ParameterSpec]:
    needle = str(query or "").strip().lower()
    specs = list(parameter_registry().values())
    if not needle:
        return specs[:limit] if limit else specs
    by_key = sorted((spec for spec in specs if needle in spec.key.lower()),
                    key=lambda spec: (spec.key != needle, not spec.key.startswith(needle), len(spec.key)))
    by_text = [spec for spec in specs if needle not in spec.key.lower() and needle in spec.description.lower()]
    found = by_key + by_text
    return found[:limit] if limit else found


__all__ = [
    "SAFETY_BOUNDARY",
    "SAFETY_FREE",
    "ParameterSpec",
    "category_for",
    "classify_safety",
    "is_masked",
    "parameter_registry",
    "search_parameters",
]
