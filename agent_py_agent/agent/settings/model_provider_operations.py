# LLM: 此模块仅修改锁内内存数据，文件原子写由 model_profiles 唯一入口负责；没有网络或 LLM 副作用。
# 模块用途: 实现服务商和模型的新增、编辑、删除、选择，保护选中模型和秘密字段。
from __future__ import annotations

from uuid import UUID

from .model_provider_schema import (
    ModelProfileError,
    resolved_model,
    validate_base_url,
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


# LLM: 编辑空 key/headers 保留原值；表单不得建立宿主环境引用，新静态密钥或明确清除会丢弃旧引用，OAuth 不能注入令牌。
# 函数用途: 校验服务商表单并更新单份连接配置。
def _save_provider(data: dict, payload: dict) -> None:
    provider_id = validate_provider_id(payload.get("provider_id"))
    value = payload.get("provider")
    if not isinstance(value, dict):
        raise ModelProfileError("服务商配置须为对象。")
    if "api_key_ref" in value:
        raise ModelProfileError("环境凭据引用只能经显式离线迁移建立，不能由模型表单指定。")
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
    draft.pop("api_key_ref", None)
    draft.update(_provider_credentials(previous, {**draft, "api_key": value.get("api_key", "")},
                                       clear=payload.get("clear_key") is True))
    if payload.get("clear_headers") is True:
        draft["custom_headers"] = {}
    elif value.get("custom_headers") is None:
        draft["custom_headers"] = previous.get("custom_headers", {})
    data["providers"][provider_id] = validate_provider(draft)


# LLM: 原凭据只从锁内旧记录取；空输入仅在地址及传输相同才保留，明确清除或新内联值移除旧环境引用。
# 函数用途: 为连接编辑与快捷模型编辑提供同一安全的凭据合并规则。
def _provider_credentials(previous: dict, draft: dict, *, clear: bool = False) -> dict:
    key = draft.get("api_key", "")
    if not isinstance(key, str):
        raise ModelProfileError("密钥格式不合法。")
    if clear or key.strip():
        return {"api_key": "" if clear else key}
    same_endpoint = (previous and (previous.get("transport") or "http") == (draft.get("transport") or "http")
                     and previous["api_base"] == ("" if draft.get("transport") == "offline"
                                                  else validate_base_url(draft.get("api_base"))))
    if same_endpoint:
        return {"api_key": previous.get("api_key", ""),
                **({"api_key_ref": previous["api_key_ref"]} if previous.get("api_key_ref") else {})}
    if previous.get("api_key") or previous.get("api_key_ref"):
        raise ModelProfileError("修改接口地址或传输类型必须重新输入密钥，或明确清除原凭据。")
    return {"api_key": ""}


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


# LLM: 快捷 add 幂等保留用途；显式编辑只更新独立连接，稳定模型/服务商 ID 不变，禁止借编辑改其它模型凭据。
# 函数用途: 将快捷新增或明确编辑变为一个服务商和模型的原子保存，模型用途保持显式。
def _add_flat(data: dict, payload: dict) -> None:
    key = _model_id(payload.get("profile_id"))
    value = payload.get("profile")
    if not isinstance(value, dict):
        raise ModelProfileError("模型配置须为对象。")
    if "api_key_ref" in value:
        raise ModelProfileError("环境凭据引用只能经显式离线迁移建立，不能由模型表单指定。")
    previous = data["profiles"].get(key)
    editing = payload.get("editing") is True
    if editing and previous is None:
        raise ModelProfileError("待编辑模型已不存在。")
    provider_id = previous["provider_id"] if editing else "provider-" + key
    if editing and any(other != key and model["provider_id"] == provider_id for other, model in data["profiles"].items()):
        raise ModelProfileError("此连接被多个模型使用，请在终端管理连接。")
    draft = dict(value)
    if editing:
        original = data["providers"][provider_id]
        draft = {**previous, "api_base": original["api_base"],
                 **({"transport": original["transport"]} if original.get("transport") else {}), **value}
        same_endpoint = ((original.get("transport") or "http") == (draft.get("transport") or "http")
                         and str(draft["api_base"]).rstrip("/") == original["api_base"])
        draft = {"model_custom_headers": original["custom_headers"] if same_endpoint else {},
                 "model_session_header": original["session_header"] if same_endpoint else "", **draft}
        draft.update(_provider_credentials(original, draft))
    row = validate_model_profile(draft)
    provider = validate_provider({**row, "display_name": row["model_name"],
        "capabilities": [row["capability"]],
        "custom_headers": row.get("model_custom_headers", {}), "session_header": row.get("model_session_header", "")})
    model = validate_model({**row, "provider_id": provider_id})
    if not editing and key in data["profiles"] and (data["profiles"][key] != model or data["providers"].get(provider_id) != provider):
        raise ModelProfileError("这个保存编号已被使用，请重新打开新增模型。")
    data["providers"][provider_id] = provider
    data["profiles"][key] = model


# LLM: operation 是结构化管理命令，save_profile 必须显式 editing；所有校验在唯一文件锁内完成，未知动作不写盘。
#   add_models 返回实际新增的模型名（已存在的同连接同名模型被跳过），其余动作返回 None。
# 函数用途: 修改当前用户的模型配置内存快照，由调用方一次落盘。
def mutate_profiles(data: dict, operation: str, payload: dict) -> list[str] | None:
    if operation in {"add", "save_profile"}:
        if operation == "save_profile" and payload.get("editing") is not True:
            raise ModelProfileError("编辑模型需要显式 editing。")
        _add_flat(data, payload)
        return
    if operation == "save_provider":
        _save_provider(data, payload)
        return
    if operation == "save_model":
        key = _model_id(payload.get("profile_id"))
        value = payload.get("profile")
        if not isinstance(value, dict):
            raise ModelProfileError("模型配置须为对象。")
        previous = data["profiles"].get(key)
        if payload.get("editing") is True and previous is None:
            raise ModelProfileError("待编辑模型已不存在。")
        row = validate_model({**previous, **value} if previous and payload.get("editing") is True else value)
        if row["provider_id"] not in data["providers"]:
            raise ModelProfileError("服务商不存在，请先保存服务商。")
        if key in data["profiles"] and data["profiles"][key] != row and payload.get("editing") is not True:
            raise ModelProfileError("此模型编号已存在，请进入编辑。")
        data["profiles"][key] = row
        return
    if operation in {"delete_provider", "delete_model"}:
        _delete(data, operation, payload)
        return
    if operation == "add_models":
        from .model_connections import add_connection_models

        return add_connection_models(data, payload)
    if operation == "select":
        _select(data, payload)
        return
    raise ModelProfileError("不支持的模型配置操作。")


# LLM: 显式选择只校验可用性并写 selected；不修改正在执行的快照，default 表示尚未选择目录模型而非部署连接旁路。
# 函数用途: 保存未来会话默认引用，正式聊天模型必须来自目录或管理员显式共享。
def _select(data: dict, payload: dict) -> None:
    key = str(payload.get("profile_id") or "")
    if key != "default":
        key = _model_id(key)
        if key not in data["profiles"]:
            raise ModelProfileError("模型配置不存在，请刷新列表。")
        resolved_model(data, key)
    data["selected"] = key
