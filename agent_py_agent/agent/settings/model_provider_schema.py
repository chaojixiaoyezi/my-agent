# LLM: provider/model 用途、持久目录代次与迁移只有这个事实源；代次不能从凭据推导，decision 不可解析为生成模型。
# 模块用途: 校验原模型目录与不含秘密的版本快照，让聊天和决策共用存储并保持用途、恢复边界。
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit
from uuid import UUID

from ..backends.provider_headers import validate_headers, validate_session_header
from ..backends.sampling import validate_top_p

SCHEMA = "owner_model_profiles.v5"
BACKENDS = {"openai_compatible", "anthropic_compatible", "openai_responses", "typesafe_decision"}
CAPABILITIES = {"agentic", "embedding", "decision"}


# LLM: 此类错误必须仅使用固定脱敏文案；HTTP/TUI 可直接公开。
# 类用途: 表示用户可以在模型表单中修正的配置错误。
class ModelProfileError(ValueError):
    pass


# LLM: 持久代次由原保存事务生成 UUID4，不接受秘密摘要或任意展示文本；缺失只在显式旧版迁移中表示未知。
# 函数用途: 严格检查可公开比较的随机目录版本，错误不包含输入值。
def validate_catalog_generation(value: object) -> str:
    if not isinstance(value, str) or len(value) != 32:
        raise ModelProfileError("模型目录代次格式无效。")
    try:
        token = UUID(value)
        if token.hex != value or token.version != 4:
            raise ValueError("generation")
    except ValueError:
        raise ModelProfileError("模型目录代次格式无效。") from None
    return value


# LLM: 这里只存原引用、可信目录位置标识和随机代次，不携带凭据或配置；共享快照必须同时覆盖来源及发布。
# 类用途: 为 pending 建议保存可跨重启核对的模型目录版本，旧未知值不能伪装成有效快照。
@dataclass(frozen=True)
class ModelProfileGeneration:
    profile_id: str
    authority_id: str
    catalog_generation: str
    shared_generation: str | None = None

    # LLM: 直接构造和反序列化共用严格验证；这里只检查结构，不授予 owner/shared 权限。
    # 函数用途: 拒绝伪造格式、默认模型和不完整共享版本，后续仍须读原目录复核。
    def __post_init__(self) -> None:
        try:
            if not isinstance(self.profile_id, str):
                raise ValueError("profile")
            profile = UUID(self.profile_id.removeprefix("shared:"))
            if self.profile_id.startswith("shared:") and self.profile_id != "shared:" + str(profile):
                raise ValueError("shared profile")
        except ValueError:
            raise ModelProfileError("模型目录版本引用无效。") from None
        if not isinstance(self.authority_id, str) or not re.fullmatch(r"[0-9a-f]{64}", self.authority_id):
            raise ModelProfileError("模型目录版本归属无效。")
        validate_catalog_generation(self.catalog_generation)
        if self.profile_id.startswith("shared:"):
            validate_catalog_generation(self.shared_generation)
        elif self.shared_generation is not None:
            raise ModelProfileError("私有模型不能携带共享发布代次。")

    # LLM: 序列化只能输出这个冻结合同的字段，不能从配置补入路径或秘密。
    # 函数用途: 生成 pending 可以保存的原目录版本信封。
    def to_dict(self) -> dict:
        return {"schema": "model_profile_generation.v1", **asdict(self)}

    # LLM: 未知/缺失字段不得被默认填成可采用版本；旧 pending 缺整个信封由调用方保留原模型。
    # 函数用途: 从持久 pending 严格恢复版本快照，不读盘也不初始化目录。
    @classmethod
    def from_dict(cls, value: object) -> ModelProfileGeneration:
        fields = {"profile_id", "authority_id", "catalog_generation", "shared_generation"}
        if (type(value) is not dict or set(value) != fields | {"schema"}
                or value["schema"] != "model_profile_generation.v1"):
            raise ModelProfileError("模型目录版本信封无效。")
        return cls(**{key: value[key] for key in fields})


# LLM: 地址只接受显式 HTTP(S)，不接受内嵌认证/query；不对内网地址做模型名称猜测。
# 函数用途: 检查基础地址并保留用户配置的路径前缀。
def validate_base_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 33 for c in value):
        raise ModelProfileError("接口地址不能为空或含有空白/控制字符。")
    try:
        url = urlsplit(value)
        valid = url.scheme in {"http", "https"} and bool(url.hostname) and not (url.username or url.password or url.query or url.fragment)
        _ = url.port
    except ValueError:
        valid = False
    if not valid:
        raise ModelProfileError("地址须为 http(s) 接口基础地址，不要包含密钥、查询参数或片段。")
    return value.rstrip("/")


# LLM: ID 是 owner 文件内稳定键，不是目录；后续编辑必须保持 ID 不变。
# 函数用途: 校验服务商的短编号，避免不可见字符及歧义。
def validate_provider_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise ModelProfileError("Provider ID 请使用 1 至 96 个字母、数字、点、横线或下划线。")
    return value


# LLM: 保留/清除 secret 是明确字段操作；用途不从模型名猜，OAuth 状态只校验不公开。
# 函数用途: 检查服务商的明确用途和连接配置；允许无密钥保存草稿，发请求前另行检查。
def validate_provider(value: object) -> dict:
    if not isinstance(value, dict):
        raise ModelProfileError("服务商配置须为对象。")
    name = value.get("display_name", "")
    key = value.get("api_key", "")
    if any(not isinstance(text, str) or len(text) > 4096 or any(ord(c) < 32 for c in text) for text in (name, key)):
        raise ModelProfileError("展示名或密钥格式不合法。")
    if not name.strip():
        raise ModelProfileError("服务商展示名不能为空。")
    enabled = value.get("enabled", True)
    capabilities = value.get("capabilities", ["agentic"])
    if type(enabled) is not bool or not isinstance(capabilities, list) or not capabilities or any(not isinstance(c, str) or c not in CAPABILITIES for c in capabilities):
        raise ModelProfileError("请明确选择启用状态及 Agentic/Embedding/Decision 能力。")
    try:
        headers = validate_headers(value.get("custom_headers", {}))
        session = validate_session_header(value.get("session_header", ""))
    except ValueError as exc:
        raise ModelProfileError(str(exc)) from exc
    if session and session.lower() in {key.lower() for key in headers}:
        raise ModelProfileError("会话头请使用专门选项，不要同时填写静态值。")
    result = {"display_name": name.strip(), "api_base": validate_base_url(value.get("api_base")),
            "api_key": key.strip(), "enabled": enabled, "capabilities": sorted(set(capabilities)),
            "custom_headers": headers, "session_header": session}
    if value.get("auth"):
        from .model_oauth_schema import stored_oauth

        if key.strip():
            raise ModelProfileError("同一服务商不能同时保存 API Key 和 OAuth 登录，请分开创建。")
        result["auth"] = stored_oauth(value["auth"], result["api_base"])
    return result


# LLM: 模型引用不持有第二份 secret；decision 只走独立决策协议，不能进入生成适配器；同步用途隔离测试。
# 函数用途: 检查模型协议、用途、容量和适用采样；型号允许透传，决策模型不接受无效的生成参数。
def validate_model(value: object) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("model_backend"), str) or value["model_backend"] not in BACKENDS:
        raise ModelProfileError("请选择 OpenAI Chat、OpenAI Responses、Anthropic 或 TypeSafe 决策接口。")
    name = value.get("model_name")
    if not isinstance(name, str) or not name.strip() or len(name) > 4096 or any(ord(c) < 32 for c in name):
        raise ModelProfileError("模型名称不能为空或包含控制字符。")
    window = value.get("model_context_window_tokens")
    if isinstance(window, bool) or not str(window).isascii() or not str(window).isdigit() or not 4096 <= int(window) <= 2**31 - 1:
        raise ModelProfileError("上下文窗口请填写 4096 至 2147483647 之间的整数 tokens。")
    capability = value.get("capability", "agentic")
    if not isinstance(capability, str) or capability not in CAPABILITIES or type(value.get("enabled", True)) is not bool:
        raise ModelProfileError("模型用途或启用状态不合法。")
    if (capability == "decision") != (value["model_backend"] == "typesafe_decision"):
        raise ModelProfileError("TypeSafe 决策接口必须配合 Decision 用途，不能用于聊天生成。")
    if capability == "decision" and any(value.get(key) not in (None, "") for key in ("temperature", "top_p", "model_queue_wait_seconds")):
        raise ModelProfileError("决策模型不使用温度、top_p 或生成排队预算；等待时间由决策设置控制。")
    result = {"provider_id": validate_provider_id(value.get("provider_id")), "model_name": name.strip(),
            "model_backend": value["model_backend"], "model_context_window_tokens": int(window),
            "capability": capability, "enabled": value.get("enabled", True)}
    temperature = value.get("temperature")
    if temperature not in (None, ""):
        try:
            number = float(temperature)
        except (TypeError, ValueError) as exc:
            raise ModelProfileError("温度须留空或填写 0 至 2 的数值。") from exc
        if isinstance(temperature, bool) or not math.isfinite(number) or not 0 <= number <= 2:
            raise ModelProfileError("温度须留空或填写 0 至 2 的数值。")
        result["temperature"] = str(number)
    try:
        top_p = validate_top_p(value.get("top_p"))
    except ValueError as exc:
        raise ModelProfileError(str(exc)) from exc
    if top_p is not None:
        result["top_p"] = top_p
    queue = value.get("model_queue_wait_seconds")
    if queue not in (None, ""):
        try:
            seconds = float(queue)
        except (TypeError, ValueError):
            raise ModelProfileError("排队预算须为 0 至 86400 秒。") from None
        if isinstance(queue, bool) or not math.isfinite(seconds) or not 0 <= seconds <= 86400:
            raise ModelProfileError("排队预算须为 0 至 86400 秒。")
        result["model_queue_wait_seconds"] = seconds
    return result


# LLM: 扁平输入立即变成 provider 引用；用途必须保留，不能在快捷新增中把 decision 降为 agentic。
# 函数用途: 校验快捷新增的完整连接信息及用途；秘密仍只保存到服务商，非生成字段不传入 AgentConfig。
def validate_model_profile(value: object) -> dict:
    if not isinstance(value, dict):
        raise ModelProfileError("模型配置须为对象。")
    model = validate_model({**value, "provider_id": "validation"})
    provider = validate_provider({"display_name": model["model_name"], "capabilities": [model["capability"]], **value,
        "custom_headers": value.get("model_custom_headers", {}), "session_header": value.get("model_session_header", "")})
    if not provider["api_key"]:
        raise ModelProfileError("模型名称、地址和密钥不能为空。")
    row = {key: model[key] for key in ("model_name", "model_backend", "model_context_window_tokens", "temperature", "top_p", "model_queue_wait_seconds", "capability", "enabled") if key in model}
    row.update(api_base=provider["api_base"], api_key=provider["api_key"])
    if provider["custom_headers"]:
        row["model_custom_headers"] = provider["custom_headers"]
    if provider["session_header"]:
        row["model_session_header"] = provider["session_header"]
    return row


# LLM: 迁移保持 selected/profile UUID、密钥和传输头，不写源文件；同步 v1 完整连接回归。
# 函数用途: 将旧版逐模型连接转换为一对一服务商，保留采样、身份头与已有子代理模型引用。
def migrate_v1(data: dict) -> dict:
    migrated = {**data, "schema": "owner_model_profiles.v3", "providers": {}, "profiles": {}}
    for profile_id, value in data["profiles"].items():
        if value.get("capability") == "decision" or value.get("model_backend") == "typesafe_decision":
            raise ModelProfileError("旧模型目录不能包含决策模型，请使用当前配置入口新增。")
        row = validate_model_profile(value)
        provider_id = "provider-" + profile_id
        migrated["providers"][provider_id] = validate_provider({**row, "display_name": row["model_name"],
            "capabilities": [row["capability"]], "custom_headers": row.get("model_custom_headers", {}),
            "session_header": row.get("model_session_header", "")})
        migrated["profiles"][profile_id] = validate_model({**row, "provider_id": provider_id})
    return migrate_v3(migrated)


# LLM: v2 只拥有 agentic/embedding 用途；迁移只改内存版本，拒绝把未来协议伪装成旧配置。
# 函数用途: 显式升级原模型目录，不改编号、凭据、选择或源文件；下一次修改才写入当前版本。
def migrate_v2(data: dict) -> dict:
    for row in data["profiles"].values():
        if row.get("capability", "agentic") == "decision" or row.get("model_backend") == "typesafe_decision":
            raise ModelProfileError("旧模型目录不能包含决策模型，请使用当前配置入口新增。")
    if any("decision" in row.get("capabilities", []) for row in data["providers"].values()):
        raise ModelProfileError("旧服务商目录不能包含决策用途。")
    return migrate_v3({**data, "schema": "owner_model_profiles.v3"})


# LLM: v3 没有决策覆盖；迁移串到 v4 的显式代次迁移，不允许夹带未来字段后被静默清除。
# 函数用途: 只在内存新增空决策覆盖和未知目录代次，下一次原事务保存才写当前版本。
def migrate_v3(data: dict) -> dict:
    from .decision_settings_schema import empty_decision_settings

    if "decision_settings" in data:
        raise ModelProfileError("旧模型目录不能包含新版本决策覆盖。")
    return migrate_v4({**data, "schema": "owner_model_profiles.v4", "decision_settings": empty_decision_settings()})


# LLM: 旧目录没有持久代次，不能由读取生成历史证明；拒绝旧 schema 夹带未来版本字段。
# 函数用途: 将 v4 在内存归一成未知版本，原保存或已启用准备才初始化随机代次。
def migrate_v4(data: dict) -> dict:
    if "catalog_generation" in data:
        raise ModelProfileError("旧模型目录不能包含新版本目录代次。")
    return {**data, "schema": SCHEMA, "catalog_generation": None}


# LLM: 唯一解析点校验调用方所需用途，即使跳过启用检查也不能混用；OAuth 只返回绑定引用。
# 函数用途: 获得指定用途的连接字段，默认只供聊天生成；决策消费者须显式请求 decision，不发网络请求。
def resolved_model(data: dict, profile_id: str, *, require_enabled: bool = True, capability: str = "agentic") -> dict:
    from .model_oauth_schema import has_credential, oauth_binding

    model = data["profiles"][profile_id]
    provider = data["providers"][model["provider_id"]]
    if capability not in CAPABILITIES or model["capability"] != capability or capability not in provider["capabilities"]:
        raise ModelProfileError("模型及服务商用途与本次请求不一致。")
    if require_enabled and (not model["enabled"] or not provider["enabled"] or not has_credential(provider)):
        raise ModelProfileError("这个模型或服务商未启用、缺少密钥或尚未登录。")
    result = {**{key: model[key] for key in ("model_name", "model_backend", "model_context_window_tokens", "temperature", "top_p", "model_queue_wait_seconds") if key in model},
            "api_base": provider["api_base"], "api_key": provider["api_key"],
            "model_custom_headers": dict(provider["custom_headers"]), "model_session_header": provider["session_header"]}
    auth = provider.get("auth")
    if auth:
        if auth["mode"] == "chatgpt" and model["model_backend"] != "openai_responses":
            raise ModelProfileError("ChatGPT 订阅登录需要 OpenAI Responses 接口。")
        result["model_auth_ref"] = {"provider_id": model["provider_id"], "mode": auth["mode"],
                                   "generation": auth.get("generation", ""), "binding": oauth_binding(provider)}
    else:
        result["model_auth_ref"] = {}
    return result
