# LLM: 共享目录只存显式发布引用及随机代次；来源目录先于发布目录加原锁，凭据始终只在管理员私有文件。
# 模块用途: 管理共享模型的发布和撤销版本，支持旧建议复核，不复制密钥或自动开放已有配置。

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from ..common.json_io import locked_json_path, write_json_file_atomic_unlocked
from ..user_space.approval_mode import is_permission_admin
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from .model_provider_schema import ModelProfileError, resolved_model, validate_catalog_generation

_SCHEMA = "shared_model_catalog.v2"


# LLM: 共享 ID 必须显式使用 namespace，UUID 之外不接受路径、URL 或其他 owner 身份。
# 函数用途: 校验共享模型引用，返回管理员原模型编号；普通模型返回空值。
def shared_profile_key(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("shared:"):
        return ""
    try:
        key = str(UUID(value.removeprefix("shared:")))
    except ValueError as exc:
        raise ModelProfileError("共享模型编号无效。") from exc
    if value != "shared:" + key:
        raise ModelProfileError("共享模型编号无效。")
    return key


# LLM: 唯一目录定位于可信 config_dir，不读取请求提供的路径；目录本身不含连接凭证。
# 函数用途: 定位管理员共享开关文件。
def shared_catalog_path(home: object) -> Path:
    return Path(home.config_dir) / "shared-model-profiles.json"


# LLM: v1 只在内存迁移为未知代次；v2 必须有随机代次，损坏不能洗为空表或覆盖，普通读取无写入。
# 函数用途: 读取原共享引用与发布版本，缺失目录表示从未发布。
def _read_catalog(home: object) -> dict:
    path = shared_catalog_path(home)
    if not path.exists():
        return {"schema": _SCHEMA, "profiles": [], "catalog_generation": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["schema"] == "shared_model_catalog.v1":
            if "catalog_generation" in data:
                raise ValueError("legacy generation")
            data = {**data, "schema": _SCHEMA, "catalog_generation": None}
        elif data["schema"] == _SCHEMA:
            validate_catalog_generation(data["catalog_generation"])
        else:
            raise ValueError("schema")
        if not isinstance(data["profiles"], list):
            raise ValueError("schema")
        keys = {shared_profile_key(value) for value in data["profiles"]}
        if "" in keys:
            raise ValueError("id")
        return data
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelProfileError("共享模型目录损坏，未覆盖已有配置。") from exc


# LLM: 管理员来源始终是可信根下固定 local/main，不能由快照或请求传入路径；与普通解析和版本 guard 共用。
# 函数用途: 定位原管理员模型文件，保证共享解析、初始化和采用复核使用同一把锁。
def _admin_profiles_path(home: object) -> Path:
    from .model_profiles import model_profiles_path

    owner = resolve_owner_home(home.root, OwnerIdentity.local_main())
    identity = SimpleNamespace(config_dir=home.config_dir, owner_provider=owner.identity.provider,
                               owner_kind=owner.identity.owner_kind, owner_id=owner.owner_id)
    return model_profiles_path(identity)


# LLM: 返回原私有秘密数据，只能由服务端解析或版本复核消费；不能直接公开整个对象。
# 函数用途: 读取共享目录唯一管理员来源，不创建第二份连接配置。
def _admin_profiles(home: object) -> dict:
    from .model_profiles import read_model_profiles

    return read_model_profiles(_admin_profiles_path(home))


# LLM: 授权和 OAuth 共享禁令只有这一读取路径；代次未知不改变原普通解析的授权语义。
# 函数用途: 同时读取原发布事实和原模型来源，供普通解析及持久版本快照复用。
def _shared_source(home: object, profile_id: str) -> tuple[dict, dict]:
    catalog = _read_catalog(home)
    key = shared_profile_key(profile_id)
    if not key or profile_id not in catalog["profiles"]:
        raise ModelProfileError("共享模型未开放或已撤销，请在 /model 重新选择。")
    data = _admin_profiles(home)
    if key not in data["profiles"]:
        raise ModelProfileError("共享模型原配置已删除，请在 /model 重新选择。")
    if data["providers"][data["profiles"][key]["provider_id"]].get("auth"):
        raise ModelProfileError("订阅/OAuth 登录仅供所属用户使用，不能跨用户共享。")
    return data, catalog


# LLM: 只在持有原共享目录锁的事务调用；发布内容与随机代次同次 replace，失败不能提前改内存版本。
# 函数用途: 原子保存共享引用并轮换发布版本，复用原文件不保存凭据。
def _save_catalog(home: object, catalog: dict) -> None:
    saved = {"schema": _SCHEMA, "profiles": catalog["profiles"], "catalog_generation": uuid4().hex}
    write_json_file_atomic_unlocked(shared_catalog_path(home), saved)
    catalog.update(saved)


# LLM: 发布及 OAuth 边界复用 _shared_source，离线设置可显式跳过启用检查；不因读取而初始化版本。
# 函数用途: 解析已授权共享连接，普通执行与版本快照保持同一来源及用途检查。
def resolve_shared_model(home: object, profile_id: str, *, capability: str = "agentic", require_enabled: bool = True) -> dict:
    data, _ = _shared_source(home, profile_id)
    return resolved_model(data, shared_profile_key(profile_id), capability=capability, require_enabled=require_enabled)


# LLM: 公开投影不带代次、其他私有模型或凭据；旧发布目录只读，不触发初始化或改变原 available 语义。
# 函数用途: 给模型选择框补充已开放选项，已删除来源不再展示。
def public_shared_profiles(home: object) -> list[dict]:
    keys = {shared_profile_key(value) for value in _read_catalog(home)["profiles"]}
    if not keys:
        return []
    from .model_profiles import public_model_profiles

    data = _admin_profiles(home)
    selected = {key: row for key, row in data["profiles"].items() if key in keys}
    public = public_model_profiles({**data, "profiles": selected}, SimpleNamespace())
    return [{**row, "id": "shared:" + row["id"], "shared": True} for row in public["profiles"] if row["id"] in selected]


# LLM: 原来源→发布锁顺序与 generation guard 一致；显式发布迁移旧来源，撤销不依赖损坏/已删来源且每次换代。
# 函数用途: 原子发布或撤销 API Key 模型并使旧建议失效，不改变会话选择，也不共享 OAuth。
def set_shared_profile(agent: object, profile_id: object, enabled: object) -> None:
    from .model_profiles import _save_profiles

    if not is_permission_admin(agent.home_paths):
        raise ModelProfileError("只有管理员可以发布或撤销共享模型。")
    if not isinstance(enabled, bool):
        raise ModelProfileError("共享开关必须为布尔值。")
    try:
        key = str(UUID(str(profile_id)))
    except ValueError as exc:
        raise ModelProfileError("请选择管理员自己保存的模型编号。") from exc
    path = shared_catalog_path(agent.home_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked_json_path(_admin_profiles_path(agent.home_paths)), locked_json_path(path):
        if enabled:
            data = _admin_profiles(agent.home_paths)
            if key not in data["profiles"]:
                raise ModelProfileError("管理员模型配置不存在。")
            if data["providers"][data["profiles"][key]["provider_id"]].get("auth"):
                raise ModelProfileError("订阅/OAuth 登录仅供所属用户使用，不能跨用户共享。")
            resolved_model(data, key, capability=data["profiles"][key]["capability"])
            if data["catalog_generation"] is None:
                _save_profiles(_admin_profiles_path(agent.home_paths), data)
        catalog = _read_catalog(agent.home_paths)
        keys = {shared_profile_key(value) for value in catalog["profiles"]}
        keys.add(key) if enabled else keys.discard(key)
        catalog["profiles"] = ["shared:" + value for value in sorted(keys)]
        _save_catalog(agent.home_paths, catalog)
