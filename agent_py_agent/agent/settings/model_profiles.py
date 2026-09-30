# LLM: 原目录保存同时轮换持久代次；pending 采用沿原锁复核，普通读不迁移落盘，密钥不进快照或公开投影。
# 原选择读取的可选上下文捕获仅复用此次开关事实，退出清理；不能移动既有模型冻结点或成为配置缓存。
# 模块用途: 管理主/子模型与决策入口，为自动增强提供可跨重启核对的原目录版本，不另建配置或请求路径。

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
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
    initial_profile_key,
    public_shared_profiles,
    resolve_shared_model,
    set_initial_profile,
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
#   默认行的窗口按 context_window_or_default 展示：部署配置留空时显示兜底 128000，不把 None 交给 TUI/IM 列表。
# 函数用途: 提供模型和服务商的脱敏列表；决策配置可管理，但不会进入普通聊天的可选集合。
def public_model_profiles(data: dict, config: object) -> dict:
    from .defaults import context_window_or_default
    from .model_oauth_schema import has_credential

    default = {key: getattr(config, key, "") for key in ("model_backend", "model_name")}
    default["model_context_window_tokens"] = context_window_or_default(config)
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


# LLM: 只给经本入口的 decision_read（本地与 Gateway TUI 的决策菜单）附加近 24 小时的点位诊断；模型侧（含飞书等 IM 对话）
#   看 audit_records topic=decision 的同一份 point_diagnostics。begin_decision_stage 与 user_config 的设置读取不走这里，
#   决策热路径不多读文件。诊断只读 decision_reach_counts，读不到 effective 视图时原样返回。
# 函数用途: 在决策设置读取结果上附加每个点位“最近检查几次、调用几次、为什么没触发”。
def _with_point_diagnostics(agent: object, result: dict) -> dict:
    if not isinstance(result, dict) or not isinstance(result.get("effective"), dict):
        return result
    from ..conversation.decision_reach_counts import (
        decision_point_diagnostics,
        decision_reach_summary,
    )
    from .decision_settings_schema import POINTS

    reach = decision_reach_summary(getattr(agent, "home_paths", None), since=time.time() - 86400)
    modes = {point: row.get("effective_mode") for point, row in (result["effective"].get("points") or {}).items()}
    return {**result, "point_diagnostics": decision_point_diagnostics(POINTS, modes, reach), "diagnostics_window_hours": 24}


# LLM: 目录写入锁内原子进行；select 只改 thread，set_default 只改未来默认，set_shared 只改管理员发布引用，
#   set_initial 只改共享目录里的「其他用户初始模型」；add_models 按连接一次加多个模型（见 model_connections）。
# 函数用途: 管理模型与决策设置；决策操作不初始化生成选择，网络只在显式认证/目录/连接测试时发生，回执不含令牌。
def execute_model_profile_operation(agent: object, operation: str, payload: dict, *, thread_id: str = "") -> dict:
    decision_operations = {"decision_read": "read", "decision_patch": "patch", "decision_reset": "reset"}
    if operation in decision_operations:
        from .decision_settings import execute_decision_settings_operation

        result = execute_decision_settings_operation(agent, decision_operations[operation], payload.get("decision", {}), thread_id=thread_id)
        return _with_point_diagnostics(agent, result) if operation == "decision_read" else result
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
    if operation == "set_initial":
        set_initial_profile(agent, payload.get("profile_id"))
        operation = "list"
    if operation == "list":
        return _model_selection_projection(agent, read_model_profiles(path), thread_id)
    if operation in {"auth_start", "auth_poll", "auth_status", "auth_parameters", "auth_cancel", "auth_logout",
                     "auth_browser_start", "auth_browser_complete"}:
        from .model_oauth import execute_oauth

        return execute_oauth(agent, operation, payload)
    if operation in {"discover", "probe"}:
        from .model_provider_network import execute_provider_network

        return execute_provider_network(agent, read_model_profiles(path), operation, payload)
    from .model_provider_operations import mutate_profiles

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    added = None
    with locked_json_path(path):
        data = read_model_profiles(path)
        selected = str(payload.get("profile_id") or "")
        if operation == "set_default" and shared_profile_key(selected):
            resolve_shared_model(agent.home_paths, selected)
            data["selected"] = selected
        else:
            added = mutate_profiles(data, "select" if operation == "set_default" else operation, payload)
        _save_profiles(path, data)
    result = _model_selection_projection(agent, data, thread_id)
    return {**result, "added_models": added} if operation == "add_models" else result


# LLM: 列表只含私有及管理员显式发布的公开字段；共享不是复制 secrets，默认变化不能投射成旧会话切换。
# 函数用途: 为菜单区分会话、默认与共享选项；普通用户不会看到其他用户的私有模型。
def _model_selection_projection(agent: object, data: dict, thread_id: str) -> dict:
    from ..user_space.approval_mode import is_permission_admin

    result = public_model_profiles(data, agent.config)
    try:
        shared = public_shared_profiles(agent.home_paths)
        initial = initial_profile_key(agent.home_paths)
    except (ModelProfileError, OSError):
        shared, initial = [], ""
        result["warning"] = "共享模型目录暂不可用；仍可选择自己的私有模型或显式部署配置。"
    result["can_share"] = is_permission_admin(agent.home_paths)
    result["initial_profile"] = initial
    if result["can_share"]:
        shared_keys = {shared_profile_key(row["id"]) for row in shared}
        for row in result["profiles"]:
            row["shared_enabled"] = row["id"] in shared_keys
            row["initial_for_others"] = "shared:" + row["id"] == initial
    elif initial:
        _use_initial_default(result, shared, initial)
    result["profiles"].extend(shared)
    result.update(default_selected=data["selected"], selection_scope="thread" if thread_id else "owner_default",
                  thread_id=thread_id)
    if thread_id:
        from .thread_model_selection import thread_model_profile_id

        result["selected"] = thread_model_profile_id(agent, thread_id)
    result["selection_available"] = any(row["id"] == result["selected"] and row.get("available", True)
                                        for row in result["profiles"])
    if not result["selection_available"]:
        result["warning"] = ("管理员指定的初始模型暂不可用，请在 /model 另选模型；系统没有自动切换到其他模型。"
                             if result["selected"] == "default" and initial and not result["can_share"]
                             else "尚未配置模型，请先通过 /model 新增并选择模型。" if result["selected"] == "default"
                             else "本会话选定模型已不可用，请重新选择；系统没有自动切换到其他模型。")
    return result


# LLM: 只给普通用户：default 行换成管理员初始模型的公开字段（default_source=admin_initial），可用性沿共享行；
#   原模型已删除（共享行不再展示）时显示为不可用，与运行时明确报错一致。部署默认行此时不再展示给普通用户。
# 函数用途: 让普通用户在 /model 和 IM 的模型列表里看到"默认"实际会用哪个模型。
def _use_initial_default(result: dict, shared: list[dict], initial: str) -> None:
    row = next((row for row in shared if row["id"] == initial), None)
    default = {key: value for key, value in (row or {}).items() if key != "shared"} or {
        "model_name": "管理员指定的初始模型（原配置已删除）", "model_backend": "", "model_context_window_tokens": 0,
        "available": False, "available_for": []}
    default.update(id="default", default_source="admin_initial", api_base="（管理员指定的初始模型）")
    result["profiles"] = [default, *(row for row in result["profiles"] if row["id"] != "default")]


# LLM: provider/model/secret/protocol 仍按原引用整组冻结；可选宿主捕获仅复制本次已读公开选择与决策设置，不另读目录。
#   捕获记录的是选择引用本身（default 仍记 default）；普通用户的 default 在管理员指定初始模型时解析为该共享引用。
# 函数用途: 新工作片解析选定模型并复用其开关事实；禁用、删除或撤销共享均明确报错，不偷偷换模型。
def selected_model_config(agent: object, *, profile_id: str | None = None):
    data = read_model_profiles(model_profiles_path(agent.home_paths))
    selected = data["selected"] if profile_id is None else profile_id
    captured = _SELECTED_MODEL_READ.get()
    if captured is not None and not captured:
        captured.append(SelectedModelRead(selected, deepcopy(data["decision_settings"])))
    if selected == "default":
        selected = _default_for_owner(agent)
        if selected == "default":
            return agent.config
    row = _resolved_profile(agent, data, selected)
    # 模型档案的窗口必填，写进 model_context_window_tokens 即显式容量；档案带温度就发送，没带沿用部署的 temperature（空 = 不发）。
    config = replace(agent.config, **row, api_key_env="")
    # 输出上限不在这里夹取（后端工厂按 effective_max_output_tokens 统一处理）；max_tokens 仍按模型档案优先级冻结，
    # 任务级覆盖配置改不动它，与原行为一致。
    fields = {*row, "api_key_env", "max_tokens"}
    config.config_sources = {**config.config_sources, **{key: {
        "source": "owner_model_profile", "priority": 90, "profile_id": selected,
    } for key in fields}}
    return config


# LLM: 管理员（local/main）的 default 始终是部署配置；普通用户在管理员指定初始模型时得到该共享引用。
#   撤销共享会同次清除初始模型（回到部署配置）；原模型被删时由共享解析明确报错。共享目录损坏同样报错，不猜。只读。
# 函数用途: 返回某个用户的"默认模型"实际对应的引用：管理员初始模型，或 default（部署配置）。
def _default_for_owner(agent: object) -> str:
    from ..user_space.approval_mode import is_permission_admin

    if is_permission_admin(agent.home_paths):
        return "default"
    return initial_profile_key(agent.home_paths) or "default"


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
#   09-30 用户要求：显式指定时，管理员可选自己目录里的任意模型；普通用户只能选自己添加的模型（管理员共享的引用
#   和按名匹配都不算），想用管理员开放的模型就省略 model 继承父级（父级会话本来就能用）。权限只看宿主 owner 身份。
# 函数用途: 找到子代理的生成模型；普通用户只认自己的模型，决策、未知或越权配置在创建前拒绝。
def resolve_child_model_profile(agent: object, model: object) -> str:
    from ..user_space.approval_mode import is_permission_admin

    if not isinstance(model, str) or not model.strip():
        raise ModelProfileError("子代理 model 请填写 /model 中已新增的模型名称或配置编号；省略则继承父级。")
    data = read_model_profiles(model_profiles_path(agent.home_paths))
    profiles = data["profiles"]
    model = model.strip()
    admin = is_permission_admin(agent.home_paths)
    if shared_profile_key(model) and not admin:
        raise ModelProfileError("普通用户派子代理只能指定自己添加的模型；想用管理员开放的模型，请省略 model（子代理继承当前会话的模型）。")
    if model in profiles or shared_profile_key(model):
        _resolved_profile(agent, data, model)
        return model
    available = [{"id": key, "model_name": row["model_name"]} for key, row in profiles.items() if row["capability"] == "agentic"]
    if admin:
        available.extend({"id": row["id"], "model_name": row["model_name"]} for row in public_shared_profiles(agent.home_paths)
                         if row.get("capability") == "agentic" and shared_profile_key(row["id"]) not in profiles)
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
