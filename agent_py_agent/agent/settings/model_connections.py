# LLM: "连接"是服务商面向用户的说法：地址 + 密钥 + 可选请求头 / 会话头。新增模型时先按连接拉目录（不落盘），勾选后
#   与模型一次保存；地址、密钥、请求头与会话头都相同的 API Key 服务商直接复用，不再逐模型复制凭据。已有服务商/登录账号
#   也可按编号一次加多个模型。默认用途按接口推导，TUI 可显式传 embedding 并同步写入服务商与模型用途。add_connection_models 只能在
#   model_profiles 的锁内保存入口里调用；改动须同步 test_model_connections。
# 模块用途: 支持 /model「新增模型」的"填连接 → 拉列表 → 勾选保存"、登录账号勾选模型，以及连接去重。
from __future__ import annotations

from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .model_provider_schema import (
    BACKENDS,
    CAPABILITIES,
    ModelProfileError,
    validate_model,
    validate_provider,
)


# LLM: 默认用途由接口类型推出（决策接口 → decision），只有写入调用方可显式指定另一个已登记用途；展示名缺省取地址主机名。
# 函数用途: 把客户端连接和本次明确用途整理成服务商配置（校验地址、请求头和会话头），不保存。
def connection_provider(value: object) -> dict:
    if (not isinstance(value, dict) or not isinstance(value.get("model_backend"), str)
            or value["model_backend"] not in BACKENDS):
        raise ModelProfileError("请先选择接口类型，并填写地址与密钥。")
    capability = value.get("capability", _capability(value["model_backend"]))
    if not isinstance(capability, str) or capability not in CAPABILITIES:
        raise ModelProfileError("模型用途不合法。")
    host = urlsplit(str(value.get("api_base") or "")).hostname or "连接"
    return validate_provider({"display_name": str(value.get("display_name") or host), "api_base": value.get("api_base"),
                              "api_key": value.get("api_key", ""), "custom_headers": value.get("custom_headers") or {},
                              "session_header": value.get("session_header", ""), "capabilities": [capability]})


# LLM: 只复用 API Key 服务商；登录账号（auth）不参与，四项连接字段逐字相等才算同一连接。
# 函数用途: 判断已保存的服务商是否就是这次填的连接。
def _same_connection(row: dict, provider: dict) -> bool:
    return (not row.get("auth") and row["api_base"] == provider["api_base"] and row["api_key"] == provider["api_key"]
            and row["custom_headers"] == provider["custom_headers"] and row["session_header"] == provider["session_header"])


# LLM: 在调用方已持有的目录锁内修改内存数据；模型编号由客户端生成以便重试幂等，同一服务商下同名同接口的模型不重复添加。
#   两种来源：connection（新填的连接，按四项字段复用或新建服务商）或 provider_id（已有服务商/登录账号，另给 model_backend）。
#   capability 可显式指定；服务商与每个模型都写同一用途，省略时维持按接口推导。返回实际新增的模型名。
# 函数用途: 按连接或已有服务商，把勾选的模型一次加进去（一次落盘，全部成功或全部不写）。
def add_connection_models(data: dict, payload: dict) -> list[str]:
    models = payload.get("models")
    if not isinstance(models, list) or not models or len(models) > 200:
        raise ModelProfileError("请至少选择一个模型（一次最多 200 个）。")
    connection = payload.get("connection")
    backend = payload.get("model_backend") if payload.get("provider_id") else (
        connection.get("model_backend") if isinstance(connection, dict) else None)
    if not isinstance(backend, str) or backend not in BACKENDS:
        raise ModelProfileError("请先选择接口类型。")
    capability = payload.get("capability", _capability(backend))
    if not isinstance(capability, str) or capability not in CAPABILITIES:
        raise ModelProfileError("模型用途不合法。")
    if payload.get("provider_id"):
        provider_id, backend = _existing_provider(data, payload, capability)
    else:
        provider_id, backend = _connection_provider_id(data, connection, capability)
    return [name for name in (
        _add_one(data, provider_id, backend, {**item, "capability": capability} if isinstance(item, dict) else item)
        for item in models) if name]


# LLM: 已有服务商必须在本人目录里；接口类型与用途由请求显式给出，不按服务商地址或模型名猜。
# 函数用途: 取出要加模型的已有服务商，并补上这次明确用途。
def _existing_provider(data: dict, payload: dict, capability: str) -> tuple[str, str]:
    provider_id, backend = str(payload["provider_id"]), payload.get("model_backend")
    if provider_id not in data["providers"]:
        raise ModelProfileError("服务商不存在，请刷新列表。")
    if not isinstance(backend, str) or backend not in BACKENDS:
        raise ModelProfileError("请先选择接口类型。")
    provider = data["providers"][provider_id]
    provider["capabilities"] = sorted(set(provider["capabilities"]) | {capability})
    return provider_id, backend


# LLM: 四项连接字段逐字相等就复用原服务商（补本次用途），否则新建 provider-<uuid4>；必须有密钥。
# 函数用途: 把新填的连接及用途落到一个服务商编号上。
def _connection_provider_id(data: dict, connection: object, capability: str) -> tuple[str, str]:
    value = {**connection, "capability": capability} if isinstance(connection, dict) else connection
    provider = connection_provider(value)
    if not provider["api_key"]:
        raise ModelProfileError("请填写密钥。")
    provider_id = next((key for key, row in data["providers"].items() if _same_connection(row, provider)), "")
    if provider_id:
        existing = data["providers"][provider_id]
        existing["capabilities"] = sorted(set(existing["capabilities"]) | set(provider["capabilities"]))
    else:
        provider_id = "provider-" + str(uuid4())
        data["providers"][provider_id] = provider
    return provider_id, connection["model_backend"]


# LLM: 仅提供按接口类型得出的默认用途；调用方显式传入 capability 时不经过此推导。
# 函数用途: 为旧的对话/决策新增流程补出兼容的默认用途。
def _capability(backend: str) -> str:
    return "decision" if backend == "typesafe_decision" else "agentic"


# LLM: 同一服务商下已有同名、同接口的模型不重复添加（返回空串），用途采用本次显式 capability 或原接口默认；
#   若条目带服务商目录声明的 reasoning_levels，仅刷新思考档位（其它字段不动）；编号冲突且内容不同明确报错，不覆盖。
# 函数用途: 校验并加入一个勾选的模型。
def _add_one(data: dict, provider_id: str, backend: str, item: object) -> str:
    if not isinstance(item, dict):
        raise ModelProfileError("模型条目格式不正确。")
    try:
        profile_id = str(UUID(str(item.get("profile_id"))))
    except ValueError as exc:
        raise ModelProfileError("模型编号无效。") from exc
    row = validate_model({"provider_id": provider_id, "model_name": item.get("model_name"), "model_backend": backend,
                          "model_context_window_tokens": item.get("model_context_window_tokens"),
                          "capability": item.get("capability", _capability(backend)),
                          "reasoning_levels": item.get("reasoning_levels")})
    existing = next((other for key, other in data["profiles"].items() if key != profile_id and other["provider_id"] == provider_id
                     and other["model_name"] == row["model_name"] and other["model_backend"] == backend), None)
    if existing is not None:
        # 已添加的同名模型不重复添加；只用这次带来的服务商声明刷新思考档位（其它字段不动）。
        if row.get("reasoning_levels"):
            existing["reasoning_levels"] = row["reasoning_levels"]
        return ""
    if profile_id in data["profiles"] and data["profiles"][profile_id] != row:
        raise ModelProfileError("这个模型编号已被使用，请重新打开新增模型。")
    data["profiles"][profile_id] = row
    return row["model_name"]
