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
from .shared_model_catalog import (
    public_shared_profiles,
    resolve_shared_model,
    set_shared_profile,
    shared_profile_key,
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
        if data["selected"] != "default" and data["selected"] not in data["profiles"] and not shared_profile_key(data["selected"]):
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


# LLM: 目录写入锁内原子进行；select 只改 thread，set_default 只改未来默认，set_shared 只改管理员发布引用。
# 函数用途: 管理会话选择、用户默认和显式共享；网络仅在用户主动 discover/probe 时请求，回执不含密钥。
def execute_model_profile_operation(agent: object, operation: str, payload: dict, *, thread_id: str = "") -> dict:
    path = model_profiles_path(agent.home_paths)
    if thread_id:
        from .thread_model_selection import thread_model_profile_id

        thread_model_profile_id(agent, thread_id)
    if operation == "select":
        from .thread_model_selection import thread_model_profile_id

        if not thread_id:
            raise ModelProfileError("选择模型需要当前会话；修改新会话默认值请使用 set_default。")
        thread_model_profile_id(agent, thread_id, select=str(payload.get("profile_id") or ""))
        operation = "list"
    if operation == "set_shared":
        set_shared_profile(agent, payload.get("profile_id"), payload.get("enabled"))
        operation = "list"
    if operation == "list":
        return _model_selection_projection(agent, read_model_profiles(path), thread_id)
    if operation in {"discover", "probe"}:
        from .model_provider_network import execute_provider_network

        return execute_provider_network(agent, read_model_profiles(path), operation, payload)
    from .model_provider_operations import mutate_profiles

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with locked_json_path(path):
        data = read_model_profiles(path)
        selected = str(payload.get("profile_id") or "")
        if operation == "set_default" and shared_profile_key(selected):
            resolve_shared_model(agent.home_paths, selected)
            data["selected"] = selected
        else:
            mutate_profiles(data, "select" if operation == "set_default" else operation, payload)
        _save_profiles(path, data)
    return _model_selection_projection(agent, data, thread_id)


# LLM: 列表只含私有及管理员显式发布的公开字段；共享不是复制 secrets，默认变化不能投射成旧会话切换。
# 函数用途: 为菜单区分会话、默认与共享选项；普通用户不会看到其他用户的私有模型。
def _model_selection_projection(agent: object, data: dict, thread_id: str) -> dict:
    from ..user_space.approval_mode import is_permission_admin

    result = public_model_profiles(data, agent.config)
    try:
        shared = public_shared_profiles(agent.home_paths)
    except (ModelProfileError, OSError):
        shared = []
        result["warning"] = "共享模型目录暂不可用；仍可选择自己的私有模型或部署默认。"
    result["can_share"] = is_permission_admin(agent.home_paths)
    if result["can_share"]:
        shared_keys = {shared_profile_key(row["id"]) for row in shared}
        for row in result["profiles"]:
            row["shared_enabled"] = row["id"] in shared_keys
    result["profiles"].extend(shared)
    result.update(default_selected=data["selected"], selection_scope="thread" if thread_id else "owner_default",
                  thread_id=thread_id)
    if thread_id:
        from .thread_model_selection import thread_model_profile_id

        result["selected"] = thread_model_profile_id(agent, thread_id)
    result["selection_available"] = any(row["id"] == result["selected"] and row.get("available", True)
                                        for row in result["profiles"])
    if not result["selection_available"]:
        result["warning"] = "本会话选定模型已不可用，请重新选择；系统没有自动切换到其他模型。"
    return result


# LLM: provider/model/secret/protocol 按选择的私有或授权共享引用整组冻结，旧 task overlay 不得拆开该组合。
# 函数用途: 新工作片解析选定模型，已执行快照不改；禁用、删除或撤销共享均明确报错，不偷偷换模型。
def selected_model_config(agent: object, *, profile_id: str | None = None):
    data = read_model_profiles(model_profiles_path(agent.home_paths))
    selected = data["selected"] if profile_id is None else profile_id
    if selected == "default":
        return agent.config
    row = _resolved_profile(agent, data, selected)
    config = replace(agent.config, **row, api_key_env="", model_context_window_explicit=True,
                     model_temperature_explicit="temperature" in row or agent.config.model_temperature_explicit)
    config.max_tokens = min(config.max_tokens, int(row["model_context_window_tokens"]) // 4)
    fields = {*row, "api_key_env", "model_context_window_explicit", "model_temperature_explicit", "max_tokens"}
    config.config_sources = {**config.config_sources, **{key: {
        "source": "owner_model_profile", "priority": 90, "profile_id": selected,
    } for key in fields}}
    return config


# LLM: 只检查单份连接数据，子代理创建前校验无需构造运行 backend/config；共享必须核验目录授权。
# 函数用途: 将可用模型编号解析成连接字段，未知模型明确失败。
def _resolved_profile(agent: object, data: dict, selected: str) -> dict:
    if shared_profile_key(selected):
        return resolve_shared_model(agent.home_paths, selected)
    if selected not in data["profiles"]:
        raise ModelProfileError("任务原模型配置已不存在，不能静默换成其它模型。")
    return resolved_model(data, selected)


# LLM: 显式 model 只按当前 owner 私有及管理员已发布模型精确解析；不接受端点、密钥或未授权 owner 引用。
# 函数用途: 找到子代理可用模型；私有/共享重名必须用编号消歧，未知配置在创建前拒绝。
def resolve_child_model_profile(agent: object, model: object) -> str:
    from ..user_space.approval_mode import is_permission_admin

    if not isinstance(model, str) or not model.strip():
        raise ModelProfileError("子代理 model 请填写 /model 中已新增的模型名称或配置编号；省略则继承父级。")
    data = read_model_profiles(model_profiles_path(agent.home_paths))
    profiles = data["profiles"]
    model = model.strip()
    if model in profiles or shared_profile_key(model):
        _resolved_profile(agent, data, model)
        return model
    available = [{"id": key, "model_name": row["model_name"]} for key, row in profiles.items()]
    available.extend({"id": row["id"], "model_name": row["model_name"]} for row in public_shared_profiles(agent.home_paths)
                     if not (is_permission_admin(agent.home_paths) and shared_profile_key(row["id"]) in profiles))
    matches = [row["id"] for row in available if row["model_name"] == model]
    if len(matches) == 1:
        _resolved_profile(agent, data, matches[0])
        return matches[0]
    reason = "模型名称有多个配置，请使用编号。" if matches else "当前用户尚未新增这个模型，请通过 /model 配置。"
    raise ModelProfileError(reason + " 可用配置：" + json.dumps(available, ensure_ascii=False))


# LLM: 宿主属性先移除伪造值；显式 model 冻结 ID，否则继承父级快照，包括明确的 default，不能重读 owner 默认。
# 函数用途: 在 child 创建前记录选定模型，子孙和恢复引用创建时模型而不随父会话后续切换。
def inherit_model_profile(attrs: dict, agent: object, *, model: object = None) -> None:
    attrs.pop("host_model_profile.v1", None)
    if model is not None:
        attrs["host_model_profile.v1"] = {"profile_id": resolve_child_model_profile(agent, model)}
        return
    sources = getattr(getattr(agent, "config", None), "config_sources", {})
    source = sources.get("model_name", {}) if isinstance(sources, dict) else {}
    selected = source.get("profile_id") if source.get("source") == "owner_model_profile" else "default"
    attrs["host_model_profile.v1"] = {"profile_id": selected or "default"}


# LLM: 任务先经 owner/dispatch 授权；已物化 child thread 是选择权威，创建引用只初始化未物化/旧线程，不读 owner 默认。
# 函数用途: 给重启后的子代理恢复其独立线程模型；父会话后续切换不会覆盖子代理。
def inherited_model_config(agent: object, task: object):
    attrs = getattr(task, "attributes", {}) or {}
    ref = attrs.get("host_model_profile.v1")
    if ref is not None and (not isinstance(ref, dict) or not isinstance(ref.get("profile_id"), str)):
        raise ModelProfileError("子代理模型配置引用损坏。")
    profile_id = ref["profile_id"] if ref is not None else "default"
    store = getattr(agent, "conversation_store", None)
    thread_id = str(getattr(task, "agent_thread_id", "") or "")
    thread = store.load_thread(thread_id) if store is not None and thread_id else None
    if thread is not None:
        from .thread_model_selection import thread_model_config, thread_model_profile_id

        if not thread.model_profile_id:
            thread_model_profile_id(agent, thread_id, select=profile_id)
        return thread_model_config(agent, thread_id)
    return selected_model_config(agent, profile_id=profile_id)
