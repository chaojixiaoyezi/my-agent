# LLM: 此模块仅修改锁内内存数据，文件原子写由 model_profiles 唯一入口负责；没有网络或 LLM 副作用。
# 模块用途: 实现服务商和模型的新增、编辑、删除、选择，保护选中模型和秘密字段。
from __future__ import annotations

from uuid import UUID

from .model_provider_schema import (
    ModelProfileError,
    resolved_model,
    validate_model,
    validate_model_profile,
    validate_provider,
    validate_provider_id,
)


# LLM: 模型稳定编号不来自显示名称，新增重试复用同 ID，异值拒绝。
# 函数用途: 检查模型编号格式。
def _model_id(value: object) -> str:
    try:
        return str(UUID(str(value)))
    except ValueError as exc:
        raise ModelProfileError("模型配置编号无效。") from exc


# LLM: 编辑空 key/headers 保留原值；OAuth 表单不能注入令牌，改变认证参数会丢弃旧登录，不能移送凭据。
# 函数用途: 校验服务商表单并更新单份连接配置。
def _save_provider(data: dict, payload: dict) -> None:
    provider_id = validate_provider_id(payload.get("provider_id"))
    value = payload.get("provider")
    if not isinstance(value, dict):
        raise ModelProfileError("服务商配置须为对象。")
    previous = data["providers"].get(provider_id, {})
    if previous and payload.get("editing") is not True:
        raise ModelProfileError("Provider ID 已存在，请进入编辑。")
    draft = {**previous, **value}
    if value.get("auth") or previous.get("auth"):
        from .model_oauth_schema import oauth_config

        requested = dict(value.get("auth") or previous["auth"])
        if payload.get("clear_auth_secret") is True:
            requested["client_secret"] = ""
        elif not requested.get("client_secret"):
            requested["client_secret"] = previous.get("auth", {}).get("client_secret", "")
        config = oauth_config(requested, draft["api_base"])
        if value.get("auth") and set(value["auth"]) - {"mode", "client_id", "device_url", "token_url", "scope", "audience", "client_secret"}:
            raise ModelProfileError("登录状态只能由授权流程写入，不能通过表单填写令牌。")
        old_auth = previous.get("auth", {})
        unchanged = old_auth and previous["api_base"] == draft["api_base"] and oauth_config(old_auth, previous["api_base"]) == config
        draft["auth"] = old_auth if unchanged else config
    draft["api_key"] = "" if payload.get("clear_key") is True else value.get("api_key") or previous.get("api_key", "")
    if payload.get("clear_headers") is True:
        draft["custom_headers"] = {}
    elif value.get("custom_headers") is None:
        draft["custom_headers"] = previous.get("custom_headers", {})
    data["providers"][provider_id] = validate_provider(draft)


# LLM: 删除 provider 不级联删模型，删除选中模型先要求显式换选；子任务丢引用必须明确报错而非换供应商。
# 函数用途: 删除用户明确选中的空服务商或非当前模型。
def _delete(data: dict, operation: str, payload: dict) -> None:
    if operation == "delete_provider":
        key = validate_provider_id(payload.get("provider_id"))
        if any(row["provider_id"] == key for row in data["profiles"].values()):
            raise ModelProfileError("请先移除该服务商下的模型，再删除服务商。")
        data["providers"].pop(key, None)
    else:
        key = _model_id(payload.get("profile_id"))
        if data["selected"] == key:
            raise ModelProfileError("请先选择其他模型，再删除当前模型。")
        data["profiles"].pop(key, None)


# LLM: 保留快捷 add 的幂等接口，立即规范为 provider + model；不建立第二套扁平文件。
# 函数用途: 将旧菜单快捷新增变为一个服务商和一个模型的原子保存。
def _add_flat(data: dict, payload: dict) -> None:
    key = _model_id(payload.get("profile_id"))
    row = validate_model_profile(payload.get("profile"))
    provider_id = "provider-" + key
    provider = validate_provider({**row, "display_name": row["model_name"],
        "custom_headers": row.get("model_custom_headers", {}), "session_header": row.get("model_session_header", "")})
    model = validate_model({**row, "provider_id": provider_id})
    if key in data["profiles"] and (data["profiles"][key] != model or data["providers"].get(provider_id) != provider):
        raise ModelProfileError("这个保存编号已被使用，请重新打开新增模型。")
    data["providers"][provider_id] = provider
    data["profiles"][key] = model


# LLM: operation 是结构化管理命令，所有校验在唯一文件锁内完成；未知动作不写盘。
# 函数用途: 修改当前用户的模型配置内存快照，由调用方一次落盘。
def mutate_profiles(data: dict, operation: str, payload: dict) -> None:
    if operation == "add":
        _add_flat(data, payload)
        return
    if operation == "save_provider":
        _save_provider(data, payload)
        return
    if operation == "save_model":
        key = _model_id(payload.get("profile_id"))
        row = validate_model(payload.get("profile"))
        if row["provider_id"] not in data["providers"]:
            raise ModelProfileError("服务商不存在，请先保存服务商。")
        if key in data["profiles"] and data["profiles"][key] != row and payload.get("editing") is not True:
            raise ModelProfileError("此模型编号已存在，请进入编辑。")
        data["profiles"][key] = row
        return
    if operation in {"delete_provider", "delete_model"}:
        _delete(data, operation, payload)
        return
    if operation == "select":
        _select(data, payload)
        return
    raise ModelProfileError("不支持的模型配置操作。")


# LLM: 显式选择只校验可用性并写 selected；不修改正在执行的 backend/config。
# 函数用途: 选中部署默认或一个已启用的聊天模型。
def _select(data: dict, payload: dict) -> None:
    key = str(payload.get("profile_id") or "")
    if key != "default":
        key = _model_id(key)
        if key not in data["profiles"]:
            raise ModelProfileError("模型配置不存在，请刷新列表。")
        resolved_model(data, key)
    data["selected"] = key
