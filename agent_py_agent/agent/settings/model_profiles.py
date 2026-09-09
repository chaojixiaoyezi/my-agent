# LLM: 本模块是 owner 私有模型配置的唯一文件源；子代理仅可显式引用已保存配置，密钥不进入公开投影。
# 模块用途: 保存用户模型配置，解析主/子代理的模型选择；派工不能新增配置或改变主代理选择。

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from ..common.json_io import locked_json_path
from .model_provider_schema import (
    SCHEMA,
    ModelProfileError,
    migrate_v1,
    resolved_model,
    validate_model,
    validate_model_profile,
    validate_provider,
    validate_provider_id,
)


# LLM: 路径只由可信 home/owner 身份决定，不接受客户端指定路径或从模型名拼文件名。
# 函数用途: 返回当前用户配置文件的位置，不创建目录。
def model_profiles_path(home_paths: object) -> Path:
    identity = [str(getattr(home_paths, key, "")) for key in ("owner_provider", "owner_kind", "owner_id")]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()
    return Path(home_paths.config_dir) / "model-profiles" / f"{digest}.json"


# LLM: 只认显式 schema，v1 迁移不落盘；损坏/悬空引用必须拒绝，不覆盖已有秘密。
# 函数用途: 读取当前用户的唯一 provider/model 配置并检查引用完整性。
def read_model_profiles(path: Path) -> dict:
    if not path.exists():
        return {"schema": SCHEMA, "selected": "default", "providers": {}, "profiles": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["schema"] == "owner_model_profiles.v1":
            data = migrate_v1(data)
        if data["schema"] != SCHEMA or not isinstance(data["profiles"], dict) or not isinstance(data["providers"], dict):
            raise ModelProfileError("模型配置结构无效。")
        data["providers"] = {validate_provider_id(key): validate_provider(row) for key, row in data["providers"].items()}
        for profile_id, row in data["profiles"].items():
            UUID(profile_id)
            data["profiles"][profile_id] = validate_model(row)
            if row["provider_id"] not in data["providers"]:
                raise ModelProfileError("模型引用的服务商不存在。")
        if data["selected"] != "default" and data["selected"] not in data["profiles"]:
            raise ModelProfileError("模型配置选择无效。")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ModelProfileError("模型配置文件损坏，未覆盖已有配置。") from exc
    return data


# LLM: 临时文件在写入第一个 secret 字节之前即为 0600；同目录 replace 保证断电/并发不会留下半份配置。
# 函数用途: 原子保存私有模型配置，失败时保留原文件并清理临时文件。
def _save_profiles(path: Path, data: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".models-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


# LLM: model/provider 列表只含白名单字段，密钥和任意自定义头值不公开；默认来自真实部署快照。
# 函数用途: 提供可选择的模型和可管理的服务商投影，不泄漏私有字段。
def public_model_profiles(data: dict, config: object) -> dict:
    default = {key: getattr(config, key, "") for key in (
        "model_backend", "model_name", "model_context_window_tokens",
    )}
    rows = [{"id": "default", **default, "api_base": "（使用部署配置）", "has_key": True}]
    for profile_id, row in data["profiles"].items():
        provider = data["providers"][row["provider_id"]]
        rows.append({"id": profile_id, **row, "api_base": provider["api_base"],
                     "provider_name": provider["display_name"], "has_key": bool(provider["api_key"]),
                     "available": bool(row["enabled"] and provider["enabled"] and provider["api_key"]
                                       and row["capability"] == "agentic" and "agentic" in provider["capabilities"])})
    providers = [{"id": key, **{field: row[field] for field in (
        "display_name", "api_base", "enabled", "capabilities", "session_header")},
        "has_key": bool(row["api_key"]), "header_names": sorted(row["custom_headers"])} for key, row in data["providers"].items()]
    return {"ok": True, "selected": data["selected"], "profiles": rows, "providers": providers}


# LLM: 配置写入锁内原子进行；网络操作只允许显式 discover/probe，且不持有配置锁或执行工具。
# 函数用途: 处理模型菜单的管理、选择与主动连接检查，返回脱敏回执。
def execute_model_profile_operation(agent: object, operation: str, payload: dict) -> dict:
    path = model_profiles_path(agent.home_paths)
    if operation == "list":
        return public_model_profiles(read_model_profiles(path), agent.config)
    if operation in {"discover", "probe"}:
        from .model_provider_network import execute_provider_network

        return execute_provider_network(agent, read_model_profiles(path), operation, payload)
    from .model_provider_operations import mutate_profiles

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with locked_json_path(path):
        data = read_model_profiles(path)
        mutate_profiles(data, operation, payload)
        _save_profiles(path, data)
    return public_model_profiles(data, agent.config)


# LLM: provider+model 在单次文件快照内解析，头/密钥/协议一起冻结；旧 task overlay 不得拆开该组合。
# 函数用途: 新工作片使用用户选择，已有执行不被修改，禁用或删除的原模型不自动替换。
def selected_model_config(agent: object, *, profile_id: str | None = None):
    data = read_model_profiles(model_profiles_path(agent.home_paths))
    selected = data["selected"] if profile_id is None else profile_id
    if selected == "default":
        return agent.config
    if selected not in data["profiles"]:
        raise ModelProfileError("任务原模型配置已不存在，不能静默换成其它模型。")
    row = resolved_model(data, selected)
    config = replace(agent.config, **row, api_key_env="", model_context_window_explicit=True,
                     model_temperature_explicit="temperature" in row or agent.config.model_temperature_explicit)
    config.max_tokens = min(config.max_tokens, int(row["model_context_window_tokens"]) // 4)
    fields = {*row, "api_key_env", "model_context_window_explicit", "model_temperature_explicit", "max_tokens"}
    config.config_sources = {**config.config_sources, **{key: {
        "source": "owner_model_profile", "priority": 90, "profile_id": selected,
    } for key in fields}}
    return config


# LLM: 显式 model 只按当前 owner 保存的 ID/唯一模型名精确解析；不接受端点、密钥或跨 owner 引用。
# 函数用途: 找到子代理要使用的已有配置；重名需用编号消歧，未知配置在创建前拒绝。
def resolve_child_model_profile(agent: object, model: object) -> str:
    if not isinstance(model, str) or not model.strip():
        raise ModelProfileError("子代理 model 请填写 /model 中已新增的模型名称或配置编号；省略则继承父级。")
    data = read_model_profiles(model_profiles_path(agent.home_paths))
    profiles = data["profiles"]
    model = model.strip()
    if model in profiles:
        resolved_model(data, model)
        return model
    matches = [key for key, row in profiles.items() if row["model_name"] == model]
    if len(matches) == 1:
        resolved_model(data, matches[0])
        return matches[0]
    available = [{"id": key, "model_name": row["model_name"]} for key, row in profiles.items()]
    reason = "模型名称有多个配置，请使用编号。" if matches else "当前用户尚未新增这个模型，请通过 /model 配置。"
    raise ModelProfileError(reason + " 可用配置：" + json.dumps(available, ensure_ascii=False))


# LLM: 宿主属性先移除伪造值；显式 model 解析后冻结 ID，否则继承父级当前快照，不改用户选择或运行配置。
# 函数用途: 在 child 创建前记录选定模型，子孙和恢复都走同一私有配置引用。
def inherit_model_profile(attrs: dict, agent: object, *, model: object = None) -> None:
    attrs.pop("host_model_profile.v1", None)
    if model is not None:
        attrs["host_model_profile.v1"] = {"profile_id": resolve_child_model_profile(agent, model)}
        return
    sources = getattr(getattr(agent, "config", None), "config_sources", {})
    source = sources.get("model_name", {}) if isinstance(sources, dict) else {}
    if source.get("source") == "owner_model_profile" and source.get("profile_id"):
        attrs["host_model_profile.v1"] = {"profile_id": source["profile_id"]}


# LLM: 任务经过 owner/dispatch 授权后才能读此引用；不存在或损坏时失败，不借用其它用户配置。
# 函数用途: 给重启后的子代理恢复它创建时的模型，不采用用户后来选择的新模型。
def inherited_model_config(agent: object, task: object):
    attrs = getattr(task, "attributes", {}) or {}
    ref = attrs.get("host_model_profile.v1")
    if ref is None:
        return agent.config
    if not isinstance(ref, dict) or not isinstance(ref.get("profile_id"), str):
        raise ModelProfileError("子代理模型配置引用损坏。")
    return selected_model_config(agent, profile_id=ref["profile_id"])
