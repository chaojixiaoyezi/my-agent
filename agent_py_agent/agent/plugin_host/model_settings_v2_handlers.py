# LLM: 已授权宿主调用后使用；唯一模型目录沿原锁/原保存函数，不联网，不公开API/OAuth秘密。
# 模块用途: 提供模型服务商管理和线程设置的共享校验，不创建第二份模型配置。
from __future__ import annotations

import re
from dataclasses import replace
from uuid import UUID, uuid4

from ..common.json_io import locked_json_path
from ..settings import model_profiles as catalog
from ..settings.model_provider_schema import (
    ModelProfileError,
    validate_model,
    validate_provider,
    validate_provider_id,
)
from ..settings.reasoning_effort import REASONING_LEVELS, normalize_reasoning_level
from ..user_space.approval_mode import is_permission_admin
from .errors import HostAPIError


# LLM: 仅规范化偏好，不改变宿主权限上限；owner默认和线程设置共用管理员闸。
# 函数用途: 校验三档权限模式及其旧别名。
def permission_mode(value, home_paths):
    mode = {"read-only": "ask", "workspace-write": "auto", "danger-full-access": "full-access"}.get(value, value) if isinstance(value, str) else None
    if mode not in {"ask", "auto", "full-access"}:
        raise HostAPIError("INVALID_SCHEMA")
    if mode == "full-access" and not is_permission_admin(home_paths):
        raise HostAPIError("CAPABILITY_DENIED", 403)
    return mode


# LLM: 只增强领域层create校验，不改A0授权/动作目录；HTTP接线仍由W5/W6负责。
# 函数用途: 把可选线程偏好加入创建载荷的闭合字段集合。
def creation_schema(schema):
    schema["properties"].update({"model_profile_id": {"type": "string", "maxLength": 4096},
        "reasoning_effort": {"type": "string", "enum": list(REASONING_LEVELS)},
        "permission_mode": {"type": "string", "enum": ["ask", "auto", "full-access", "read-only", "workspace-write", "danger-full-access"]},
        **{key: {"type": "array", "maxItems": 100, "items": {"type": "string", "maxLength": 128}} for key in ("preferred_pack_ids", "pack_ids")}})
    return schema


# LLM: 仅校验会话偏好，不执行授权升级；完整宿主权限门仍在运行入口，非管理员不可保存Full Access。
# 函数用途: 生成模型、档位、权限和双名能力包字段的一次性线程替换。
def configured_thread(thread, params, home_paths):
    changes, state = {}, dict(thread.metadata.get("plugin_host_v2", {}))
    effort = params.get("reasoning_effort")
    if effort is not None:
        normalized = normalize_reasoning_level(effort)
        if not normalized:
            raise HostAPIError("INVALID_SCHEMA")
        changes["reasoning_effort"] = normalized
    mode = params.get("permission_mode")
    if mode is not None:
        state["permission_mode"] = permission_mode(mode, home_paths)
    packs, alias = params.get("preferred_pack_ids"), params.get("pack_ids")
    if packs is not None and alias is not None and packs != alias:
        raise HostAPIError("INVALID_SCHEMA")
    packs = packs if packs is not None else alias
    if packs is not None:
        if not isinstance(packs, list) or len(packs) > 100 or any(not isinstance(p, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", p) for p in packs):
            raise HostAPIError("INVALID_SCHEMA")
        state["preferred_pack_ids"] = list(dict.fromkeys(packs))
    profile = params.get("model_profile_id")
    if profile is not None:
        if not isinstance(profile, str):
            raise HostAPIError("INVALID_MODEL_PROFILE")
        data = catalog.read_model_profiles(catalog.model_profiles_path(home_paths))
        candidates = [profile] if profile in data["profiles"] else [key for key, row in data["profiles"].items() if row["model_name"] == profile]
        if profile != "default" and (len(candidates) != 1 or data["profiles"][candidates[0]]["capability"] != "agentic"):
            raise HostAPIError("INVALID_MODEL_PROFILE")
        changes.update(model_profile_id="default" if profile == "default" else candidates[0],
            model_selection_revision=thread.model_selection_revision + 1, model_selection_source="explicit",
            model_selection_last_explicit_revision=thread.model_selection_revision + 1)
    state.setdefault("revision", 1)
    return replace(thread, **changes, metadata={**thread.metadata, "plugin_host_v2": state})


# LLM: 只投影展示字段，不返回凭据或源元数据；摘要与设置读取共用双名能力包键。
# 函数用途: 提供线程设置的无副作用公共部分。
def settings_fields(thread):
    state = thread.metadata.get("plugin_host_v2", {})
    packs = list(state.get("preferred_pack_ids", []))
    return {"model_profile_id": thread.model_profile_id, "reasoning_effort": thread.reasoning_effort,
            "permission_mode": state.get("permission_mode", "ask"), "preferred_pack_ids": packs,
            "pack_ids": list(packs), "compact_generation": thread.compact_generation}


# LLM: 接口仅接收宿主提供的目标home_paths；A0授权与动作路由由W5/W6提供，绝不从payload选owner。
# 类用途: 以原模型目录执行脱敏列表、原子保存及显式级联删除。
class ModelSettingsV2Handlers:
    # LLM: 不创建配置或读凭据，路径只取宿主已核owner。
    # 函数用途: 绑定本owner模型目录。
    def __init__(self, home_paths):
        self.path = catalog.model_profiles_path(home_paths)

    # LLM: 白名单投影排除auth/access_token；静态认证头同样不回显秘密，空结果不代表其它owner无配置。
    # 函数用途: 按显式用途过滤服务商及其模型，并返回当前选择和八档推理强度。
    def list_providers(self, params=None):
        capability = (params or {}).get("capability_filter", "agentic")
        if capability not in {"all", "agentic", "embedding", "decision"}:
            raise HostAPIError("INVALID_SCHEMA")
        data, providers = catalog.read_model_profiles(self.path), []
        for pid, provider in data["providers"].items():
            if capability != "all" and capability not in provider["capabilities"]:
                continue
            models = [{"profile_id": key, "id": key, **row} for key, row in data["profiles"].items()
                      if row["provider_id"] == pid and (capability == "all" or row["capability"] == capability)]
            secrets = (provider["api_key"], *(provider.get("auth", {}).get(key, "") for key in ("access_token", "refresh_token", "client_secret")))
            headers = {key: "***" if value and (re.search(r"auth|token|key|secret|credential", key, re.I)
                       or any(secret and secret in value for secret in secrets)) else value
                       for key, value in provider["custom_headers"].items()}
            providers.append({"provider_id": pid, "id": pid, **{key: provider[key] for key in
                ("display_name", "api_base", "enabled", "capabilities", "session_header")},
                "custom_headers": headers, "has_key": bool(provider["api_key"]),
                "api_key_masked": "***" if provider["api_key"] else "", "models": models})
        return {"providers": providers, "selected_profile_id": data["selected"], "reasoning_efforts": list(REASONING_LEVELS)}

    # LLM: 所有校验及upsert共原目录锁；空/脱敏key保留旧值，跨provider模型ID拒绝，失败不保存。
    # 函数用途: 保存服务商和模型列表，保持既有模型稳定ID并轮换目录代次。
    def save_provider(self, params):
        try:
            pid = validate_provider_id(params.get("provider_id"))
            models = params.get("models")
            if not isinstance(models, list) or len(models) > 500:
                raise HostAPIError("INVALID_SCHEMA")
            with locked_json_path(self.path):
                data = catalog.read_model_profiles(self.path)
                previous = data["providers"].get(pid, {})
                draft = {**previous, **{k: params[k] for k in ("display_name", "api_base", "api_key", "session_header", "custom_headers", "capabilities", "enabled") if k in params}}
                if params.get("api_key") in (None, "", "***"):
                    draft["api_key"] = previous.get("api_key", "")
                draft["custom_headers"] = {k: previous.get("custom_headers", {}).get(k, "") if v == "***" else v
                    for k, v in draft.get("custom_headers", {}).items()} if isinstance(draft.get("custom_headers", {}), dict) else draft["custom_headers"]
                data["providers"][pid] = validate_provider(draft)
                seen = set()
                for model in models:
                    row = validate_model({**model, "provider_id": pid}) if isinstance(model, dict) else validate_model(None)
                    key = model.get("profile_id", model.get("id")) or next((key for key, old in data["profiles"].items()
                        if old["provider_id"] == pid and old["model_name"] == row["model_name"]), str(uuid4()))
                    key = str(UUID(key))
                    if key in seen or key in data["profiles"] and data["profiles"][key]["provider_id"] != pid:
                        raise HostAPIError("INVALID_SCHEMA")
                    seen.add(key)
                    data["profiles"][key] = row
                catalog._save_profiles(self.path, data)
        except (ModelProfileError, TypeError, ValueError, AttributeError) as error:
            if isinstance(error, HostAPIError):
                raise
            raise HostAPIError("INVALID_SCHEMA") from None
        return self.list_providers({"capability_filter": "all"})

    # LLM: 删除明确目标只影响本目录；服务商级联其profiles，当前选择被删才回default，不迁移凭据。
    # 函数用途: 原子删除单个模型或其整个服务商，并返回脱敏新目录。
    def delete_provider_or_profile(self, params):
        profile, provider = params.get("profile_id"), params.get("provider_id")
        if not profile and not provider:
            raise HostAPIError("INVALID_SCHEMA")
        try:
            key = str(UUID(profile)) if profile else validate_provider_id(provider)
        except (TypeError, ValueError, AttributeError):
            raise HostAPIError("INVALID_SCHEMA") from None
        with locked_json_path(self.path):
            data = catalog.read_model_profiles(self.path)
            if profile:
                if provider and (provider not in data["providers"] or data["profiles"].get(key, {}).get("provider_id") != provider):
                    raise HostAPIError("RESOURCE_NOT_FOUND", 404)
                data["profiles"].pop(key, None)
            else:
                data["profiles"] = {k: row for k, row in data["profiles"].items() if row["provider_id"] != key}
                data["providers"].pop(key, None)
            if data["selected"] != "default" and data["selected"] not in data["profiles"] and not data["selected"].startswith("shared:"):
                data["selected"] = "default"
            catalog._save_profiles(self.path, data)
        return self.list_providers({"capability_filter": "all"})
