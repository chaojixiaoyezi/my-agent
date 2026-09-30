# LLM: "连接"是服务商面向用户的说法：地址 + 密钥 + 可选请求头 / 会话头。新增模型时先按连接拉目录（不落盘），勾选后
#   与模型一次保存；地址、密钥、请求头与会话头都相同的 API Key 服务商直接复用，不再逐模型复制凭据。已有服务商/登录账号
#   也可按编号一次加多个模型。决策接口只配 Decision 用途，其余接口配 Agentic。add_connection_models 只能在
#   model_profiles 的锁内保存入口里调用；改动须同步 test_model_connections。
# 模块用途: 支持 /model「新增模型」的"填连接 → 拉列表 → 勾选保存"、登录账号勾选模型，以及连接去重。
from __future__ import annotations

from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .model_provider_schema import BACKENDS, ModelProfileError, validate_model, validate_provider


# LLM: 用途只由接口类型推出（决策接口 → decision），不按模型名或地址猜；展示名缺省取地址的主机名。
# 函数用途: 把客户端填的连接整理成一份服务商配置（校验地址、请求头和会话头），不保存。
def connection_provider(value: object) -> dict:
    if not isinstance(value, dict) or value.get("model_backend") not in BACKENDS:
        raise ModelProfileError("请先选择接口类型，并填写地址与密钥。")
    capability = _capability(value["model_backend"])
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
#   服务商补上本次用途，不改启用状态与展示名。返回实际新增的模型名。
# 函数用途: 按连接或已有服务商，把勾选的模型一次加进去（一次落盘，全部成功或全部不写）。
def add_connection_models(data: dict, payload: dict) -> list[str]:
    models = payload.get("models")
    if not isinstance(models, list) or not models or len(models) > 200:
        raise ModelProfileError("请至少选择一个模型（一次最多 200 个）。")
    if payload.get("provider_id"):
        provider_id, backend = _existing_provider(data, payload)
    else:
        provider_id, backend = _connection_provider_id(data, payload.get("connection"))
    return [name for name in (_add_one(data, provider_id, backend, item) for item in models) if name]


# LLM: 已有服务商必须在本人目录里；接口类型由请求显式给出，不按服务商地址或模型名猜。
# 函数用途: 取出要加模型的已有服务商，并补上这次模型的用途。
def _existing_provider(data: dict, payload: dict) -> tuple[str, str]:
    provider_id, backend = str(payload["provider_id"]), payload.get("model_backend")
    if provider_id not in data["providers"]:
        raise ModelProfileError("服务商不存在，请刷新列表。")
    if backend not in BACKENDS:
        raise ModelProfileError("请先选择接口类型。")
    provider = data["providers"][provider_id]
    provider["capabilities"] = sorted(set(provider["capabilities"]) | {_capability(backend)})
    return provider_id, backend


# LLM: 四项连接字段逐字相等就复用原服务商（补用途），否则新建 provider-<uuid4>；必须有密钥。
# 函数用途: 把新填的连接落到一个服务商编号上。
def _connection_provider_id(data: dict, connection: object) -> tuple[str, str]:
    provider = connection_provider(connection)
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


# LLM: 决策接口只配 decision，其余接口配 agentic；embedding 仍需在模型编辑里显式选择。
# 函数用途: 由接口类型得出模型用途。
def _capability(backend: str) -> str:
    return "decision" if backend == "typesafe_decision" else "agentic"


# LLM: 同一服务商下已有同名、同接口的模型直接跳过（返回空串）；编号冲突且内容不同明确报错，不覆盖。
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
                          "capability": _capability(backend)})
    if any(other["provider_id"] == provider_id and other["model_name"] == row["model_name"] and other["model_backend"] == backend
           for key, other in data["profiles"].items() if key != profile_id):
        return ""
    if profile_id in data["profiles"] and data["profiles"][profile_id] != row:
        raise ModelProfileError("这个模型编号已被使用，请重新打开新增模型。")
    data["profiles"][profile_id] = row
    return row["model_name"]
