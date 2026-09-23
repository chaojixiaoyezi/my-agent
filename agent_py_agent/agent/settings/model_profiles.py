# LLM: 原目录保存同时轮换持久代次；pending 采用沿原锁复核，普通读不迁移落盘，密钥不进快照或公开投影。
# 原选择读取的可选上下文捕获仅复用此次开关事实，退出清理；不能移动既有模型冻结点或成为配置缓存。
# 模块用途: 管理主/子模型与决策入口，为自动增强提供可跨重启核对的原目录版本，不另建配置或请求路径。

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID, uuid4

from ..common.json_io import locked_json_path
from .model_provider_schema import (
    SCHEMA,
    ModelProfileError,
    ModelProfileGeneration,
    migrate_v1,
    migrate_v2,
    migrate_v3,
    migrate_v4,
    resolved_model,
    validate_catalog_generation,
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


# LLM: 只捕获原选择路径已读取的脱敏设置，不读第二次目录；上下文退出清理，不能作为授权或长期缓存。
# 类用途: 为宿主同一工作片提供原模型冻结时的选择编号及决策开关。
@dataclass(frozen=True)
class SelectedModelRead:
    profile_id: str
    decision_settings: dict


_SELECTED_MODEL_READ: ContextVar[list[SelectedModelRead] | None] = ContextVar("selected_model_read", default=None)


# LLM: 仅原 selected_model_config 的首次读取填充当前作用域；不改变解析顺序、网络或文件读写，嵌套退出还原。
# 函数用途: 让 Gateway 在原模型冻结期间复用已读设置，关闭增强时不为检查开关额外读盘。
@contextmanager
def capture_selected_model_read():
    captured: list[SelectedModelRead] = []
    token = _SELECTED_MODEL_READ.set(captured)
    try:
        yield captured
    finally:
        _SELECTED_MODEL_READ.reset(token)


# LLM: 路径只由可信 home/owner 身份决定，不接受客户端指定路径或从模型名拼文件名。
# 函数用途: 返回当前用户配置文件的位置，不创建目录。
def model_profiles_path(home_paths: object) -> Path:
    identity = [str(getattr(home_paths, key, "")) for key in ("owner_provider", "owner_kind", "owner_id")]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()
    return Path(home_paths.config_dir) / "model-profiles" / f"{digest}.json"


# LLM: 旧 schema 只经对应显式迁移，新版必须已有有效代次；未知版本不能用默认迁移猜测。
# 函数用途: 归一原目录版本且保持只读，把版本分派与模型内容校验分开。
def _migrated_profile_data(data: dict) -> dict:
    migrations = {"owner_model_profiles.v1": migrate_v1, "owner_model_profiles.v2": migrate_v2,
                  "owner_model_profiles.v3": migrate_v3, "owner_model_profiles.v4": migrate_v4}
    migration = migrations.get(data["schema"])
    if migration is not None:
        return migration(data)
    if data["schema"] != SCHEMA:
        raise ModelProfileError("模型配置结构无效。")
    validate_catalog_generation(data["catalog_generation"])
    return data


# LLM: v1—v4 只读迁移的代次明确未知，v5 必须有真实随机代次；未知/损坏不覆盖，不能从读取伪造旧事件。
# 函数用途: 读取原模型目录并校验用途、引用与持久版本，普通读取不会写文件。
def read_model_profiles(path: Path) -> dict:
    from .decision_settings_schema import empty_decision_settings, validate_decision_settings

    if not path.exists():
        return {"schema": SCHEMA, "selected": "default", "providers": {}, "profiles": {},
                "decision_settings": empty_decision_settings(), "catalog_generation": None}
    try:
        data = _migrated_profile_data(json.loads(path.read_text(encoding="utf-8")))
        if not isinstance(data["profiles"], dict) or not isinstance(data["providers"], dict):
            raise ModelProfileError("模型配置结构无效。")
        data["decision_settings"] = validate_decision_settings(data["decision_settings"])
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


# LLM: 必须持原目录锁；所有管理/OAuth/设置写入与随机代次同次 replace，0600 临时文件及失败旧版本保持。
# 函数用途: 原子保存唯一私有目录，成功后才更新调用方内存版本，使旧 pending 失效。
def _save_profiles(path: Path, data: dict) -> None:
    saved = {**data, "schema": SCHEMA, "catalog_generation": uuid4().hex}
    fd, name = tempfile.mkstemp(prefix=".models-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(saved, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        data.update(saved)
    finally:
        Path(name).unlink(missing_ok=True)


# LLM: 锁路径只来自可信宿主；统一当前 owner→管理员→发布目录，重复路径去重，不接受快照提供路径。
# 函数用途: 给准备和最终采用返回同一原目录锁序；后续线程 CAS 必须在这些锁之后。
def _generation_paths(agent: object, profile_id: str) -> tuple[Path, ...]:
    from .shared_model_catalog import _admin_profiles_path, shared_catalog_path

    home = agent.home_paths
    if any(not isinstance(getattr(home, key, None), str) or not getattr(home, key).strip()
           for key in ("owner_provider", "owner_kind", "owner_id")):
        raise ModelProfileError("模型目录版本需要已验证的用户身份。")
    paths = [model_profiles_path(home)]
    if shared_profile_key(profile_id):
        paths.extend((_admin_profiles_path(home), shared_catalog_path(home)))
    return tuple(dict.fromkeys(path.resolve() for path in paths))


# LLM: 复用原线程/OS 锁，不新增锁表；先配置后调用方原线程锁，锁内禁止网络或重入配置/OAuth 管理。
# 函数用途: 在异常和非阻塞竞争时正确释放已持目录锁，让可选增强及时保留原方案。
@contextmanager
def _locked_model_catalogs(agent: object, profile_id: str, *, blocking: bool):
    with ExitStack() as stack:
        for path in _generation_paths(agent, profile_id):
            stack.enter_context(locked_json_path(path, blocking=blocking))
        yield


# LLM: 初始化只能在已持有全部原目录锁且有权修改来源时调用；代次和解析读取同一来源，秘密不进入返回值。
# 函数用途: 读取或初始化一个已授权模型的原目录版本，共享同时需要发布和管理员来源代次。
def _profile_generation(agent: object, profile_id: str, *, initialize: bool) -> ModelProfileGeneration | None:
    from .shared_model_catalog import _admin_profiles_path, _save_catalog, _shared_source

    paths = _generation_paths(agent, profile_id)
    shared_key = shared_profile_key(profile_id)
    if shared_key:
        data, catalog = _shared_source(agent.home_paths, profile_id)
        resolved_model(data, shared_key)
        path = _admin_profiles_path(agent.home_paths)
    else:
        path = paths[0]
        data, catalog = read_model_profiles(path), None
        _resolved_profile(agent, data, profile_id)
    if initialize:
        if data["catalog_generation"] is None:
            _save_profiles(path, data)
        if catalog is not None and catalog["catalog_generation"] is None:
            _save_catalog(agent.home_paths, catalog)
    generation = data["catalog_generation"]
    shared_generation = catalog["catalog_generation"] if catalog is not None else None
    if generation is None or catalog is not None and shared_generation is None:
        return None
    authority = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    return ModelProfileGeneration(profile_id, authority, generation, shared_generation)


# LLM: 普通 read/off 不写；只有宿主已确认增强启用才传 initialize，旧 shared 非管理员未知须 retain；先快照后解析配置再 guard。
# 函数用途: 返回可持久化的模型版本，必要时自动迁移本 owner 旧目录；默认部署模型无可证明目录版本。
def model_profile_generation(agent: object, profile_id: str, *, initialize: bool = False) -> ModelProfileGeneration | None:
    from ..user_space.approval_mode import is_permission_admin

    if profile_id == "default":
        return None
    if not initialize or shared_profile_key(profile_id) and not is_permission_admin(agent.home_paths):
        return _profile_generation(agent, profile_id, initialize=False)
    with _locked_model_catalogs(agent, profile_id, blocking=False):
        return _profile_generation(agent, profile_id, initialize=True)


# LLM: True 区间持所有原配置锁到调用方 CAS 完成；失效/旧未知为 False，竞争非阻塞抛出；不得锁内探针或重入 settings。
# 函数用途: 防止准备后的配置、凭据、OAuth 或共享撤销变化被旧建议覆盖，不自行采用模型或发送请求。
@contextmanager
def model_profile_generation_guard(agent: object, expected: ModelProfileGeneration | None, *, blocking: bool = False):
    if expected is None:
        yield False
        return
    if not isinstance(expected, ModelProfileGeneration):
        raise ModelProfileError("模型目录版本需要原类型快照。")
    with _locked_model_catalogs(agent, expected.profile_id, blocking=blocking):
        try:
            current = _profile_generation(agent, expected.profile_id, initialize=False)
        except (ModelProfileError, OSError):
            current = None
        yield current == expected


# LLM: available 保持聊天可选语义，available_for 显式声明用途；OAuth 不暴露 token，不能误选 decision。
# 函数用途: 提供模型和服务商的脱敏列表；决策配置可管理，但不会进入普通聊天的可选集合。
def public_model_profiles(data: dict, config: object) -> dict:
    from .model_oauth_schema import has_credential

    default = {key: getattr(config, key, "") for key in (
        "model_backend", "model_name", "model_context_window_tokens",
    )}
    configured = bool(default["model_backend"] and default["model_name"] and getattr(config, "api_base", ""))
    rows = []
    if configured or default["model_backend"] == "echo":
        rows.append({"id": "default", **default, "api_base": "（使用显式部署配置）",
                     "model_name": default["model_name"] or "echo（离线调试）",
                     "has_key": bool(getattr(config, "api_key", "")), "available": True, "available_for": ["agentic"]})
    for profile_id, row in data["profiles"].items():
        provider = data["providers"][row["provider_id"]]
        available = bool(row["enabled"] and provider["enabled"] and has_credential(provider)
                         and row["capability"] in provider["capabilities"])
        rows.append({"id": profile_id, **row, "api_base": provider["api_base"],
                     "provider_name": provider["display_name"], "has_key": bool(provider["api_key"]),
                     "auth_mode": provider.get("auth", {}).get("mode", "api_key"),
                     "available": available and row["capability"] == "agentic",
                     "available_for": [row["capability"]] if available else []})
    providers = [{"id": key, **{field: row[field] for field in (
        "display_name", "api_base", "enabled", "capabilities", "session_header")},
        "has_key": bool(row["api_key"]), "header_names": sorted(row["custom_headers"]),
        "auth_mode": row.get("auth", {}).get("mode", "api_key"), "signed_in": bool(row.get("auth") and has_credential(row))}
        for key, row in data["providers"].items()]
    return {"ok": True, "selected": data["selected"], "profiles": rows, "providers": providers}


# LLM: 目录写入锁内原子进行；select 只改 thread，set_default 只改未来默认，set_shared 只改管理员发布引用。
# 函数用途: 管理模型与决策设置；决策操作不初始化生成选择，网络只在显式认证/目录/连接测试时发生，回执不含令牌。
def execute_model_profile_operation(agent: object, operation: str, payload: dict, *, thread_id: str = "") -> dict:
    decision_operations = {"decision_read": "read", "decision_patch": "patch", "decision_reset": "reset"}
    if operation in decision_operations:
        from .decision_settings import execute_decision_settings_operation

        return execute_decision_settings_operation(agent, decision_operations[operation], payload.get("decision", {}), thread_id=thread_id)
    if operation == "decision_models":
        from .decision_settings import execute_decision_settings_operation

        execute_decision_settings_operation(agent, "read", payload.get("decision", {}), thread_id=thread_id)
        view = _model_selection_projection(agent, read_model_profiles(model_profiles_path(agent.home_paths)), "")
        return {"ok": True, "scope": "owner", "profiles": [row for row in view["profiles"] if row.get("capability") == "decision"],
                **({"warning": view["warning"]} if view.get("warning") else {})}
    if operation == "decision_probe":
        from .decision_probe import probe_decision_model

        return probe_decision_model(agent, payload, thread_id=thread_id)
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
    if operation in {"auth_start", "auth_poll", "auth_status", "auth_parameters", "auth_cancel", "auth_logout"}:
        from .model_oauth import execute_oauth

        return execute_oauth(agent, operation, payload)
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
        result["warning"] = "共享模型目录暂不可用；仍可选择自己的私有模型或显式部署配置。"
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
        result["warning"] = ("尚未配置模型，请先通过 /model 新增并选择模型。"
                             if result["selected"] == "default"
                             else "本会话选定模型已不可用，请重新选择；系统没有自动切换到其他模型。")
    return result


# LLM: provider/model/secret/protocol 仍按原引用整组冻结；可选宿主捕获仅复制本次已读公开选择与决策设置，不另读目录。
# 函数用途: 新工作片解析选定模型并复用其开关事实；禁用、删除或撤销共享均明确报错，不偷偷换模型。
def selected_model_config(agent: object, *, profile_id: str | None = None):
    data = read_model_profiles(model_profiles_path(agent.home_paths))
    selected = data["selected"] if profile_id is None else profile_id
    captured = _SELECTED_MODEL_READ.get()
    if captured is not None and not captured:
        captured.append(SelectedModelRead(selected, deepcopy(data["decision_settings"])))
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


# LLM: 解析本 owner 或已授权共享的指定用途；OAuth 引用路径由可信 home 生成，不接受客户端指定。
# 函数用途: 将模型编号解析成连接字段，默认只供生成消费者；决策用途必须显式传入。
def _resolved_profile(agent: object, data: dict, selected: str, *, capability: str = "agentic") -> dict:
    if shared_profile_key(selected):
        return resolve_shared_model(agent.home_paths, selected, capability=capability)
    if selected not in data["profiles"]:
        raise ModelProfileError("任务原模型配置已不存在，不能静默换成其它模型。")
    row = resolved_model(data, selected, capability=capability)
    if row.get("model_auth_ref"):
        row["model_auth_ref"]["path"] = str(model_profiles_path(agent.home_paths))
    return row


# LLM: 子代理 model 只解析有权引用的 agentic 用途；decision 同名不制造歧义，显式错误编号不能换选。
# 函数用途: 找到子代理的生成模型；私有/共享重名用编号消歧，决策或未知配置在创建前拒绝。
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
    available = [{"id": key, "model_name": row["model_name"]} for key, row in profiles.items() if row["capability"] == "agentic"]
    available.extend({"id": row["id"], "model_name": row["model_name"]} for row in public_shared_profiles(agent.home_paths)
                     if row.get("capability") == "agentic" and not (is_permission_admin(agent.home_paths) and shared_profile_key(row["id"]) in profiles))
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
    thread = store.threads.load(thread_id) if store is not None and thread_id else None
    if thread is not None:
        from .thread_model_selection import thread_model_config, thread_model_profile_id

        if not thread.model_profile_id:
            thread_model_profile_id(agent, thread_id, select=profile_id)
        return thread_model_config(agent, thread_id)
    return selected_model_config(agent, profile_id=profile_id)
