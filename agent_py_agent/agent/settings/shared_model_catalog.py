# LLM: 共享目录只保存管理员显式发布的模型引用，密钥仍只在管理员私有 provider 文件；可信 owner 决定发布权限。
# 模块用途: 让多个用户选择管理员开放的模型，不复制密钥、不共享用户会话或私有模型，也不自动发布已有配置。

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from ..common.json_io import locked_json_path, write_json_file_atomic_unlocked
from ..user_space.approval_mode import is_permission_admin
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from .model_provider_schema import ModelProfileError, resolved_model

_SCHEMA = "shared_model_catalog.v1"


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


# LLM: 损坏目录不能静默视为空表并覆盖；缺失目录表示从未发布共享模型。
# 函数用途: 读取已发布的模型编号集合，不解析聊天正文。
def _read_catalog(home: object) -> set[str]:
    path = shared_catalog_path(home)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["schema"] != _SCHEMA or not isinstance(data["profiles"], list):
            raise ValueError("schema")
        keys = {shared_profile_key(value) for value in data["profiles"]}
        if "" in keys:
            raise ValueError("id")
        return keys
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelProfileError("共享模型目录损坏，未覆盖已有配置。") from exc


# LLM: 只允许读取固定 local/main 管理员配置，来源不是客户端输入；返回值属于服务端秘密，不得直接回传。
# 函数用途: 复用管理员保存的那份模型连接配置，不另存密钥副本。
def _admin_profiles(home: object) -> dict:
    from .model_profiles import model_profiles_path, read_model_profiles

    owner = resolve_owner_home(home.root, OwnerIdentity.local_main())
    identity = SimpleNamespace(config_dir=home.config_dir, owner_provider=owner.identity.provider,
                               owner_kind=owner.identity.owner_kind, owner_id=owner.owner_id)
    return read_model_profiles(model_profiles_path(identity))


# LLM: 每次执行核验发布、用途及可用性；OAuth 不跨用户共享，决策配置不可通过共享绕过生成用途守卫。
# 函数用途: 将已授权共享引用解析为指定用途的连接，默认只接受聊天模型；撤销或删除后明确失败。
def resolve_shared_model(home: object, profile_id: str, *, capability: str = "agentic") -> dict:
    key = shared_profile_key(profile_id)
    if not key or key not in _read_catalog(home):
        raise ModelProfileError("共享模型未开放或已撤销，请在 /model 重新选择。")
    data = _admin_profiles(home)
    if key not in data["profiles"]:
        raise ModelProfileError("共享模型原配置已删除，请在 /model 重新选择。")
    if data["providers"][data["profiles"][key]["provider_id"]].get("auth"):
        raise ModelProfileError("订阅/OAuth 登录仅供所属用户使用，不能跨用户共享。")
    return resolved_model(data, key, capability=capability)


# LLM: 仅投影显式发布的公开模型字段；不泄漏管理员其他模型、API Key、自定义头或私有文件位置。
# 函数用途: 给用户模型选择框补充管理员共享选项，已删除来源不再展示。
def public_shared_profiles(home: object) -> list[dict]:
    keys = _read_catalog(home)
    if not keys:
        return []
    from .model_profiles import public_model_profiles

    data = _admin_profiles(home)
    selected = {key: row for key, row in data["profiles"].items() if key in keys}
    public = public_model_profiles({**data, "profiles": selected}, SimpleNamespace())
    return [{**row, "id": "shared:" + row["id"], "shared": True} for row in public["profiles"] if row["id"] in selected]


# LLM: 发布与撤销只接受管理员本人编号；按原模型用途验证，发布 decision 不授予生成能力，OAuth 不共享。
# 函数用途: 开放或关闭 API Key 模型共享引用，聊天和决策用途仍由各自消费者复核，不改变会话选择。
def set_shared_profile(agent: object, profile_id: object, enabled: object) -> None:
    if not is_permission_admin(agent.home_paths):
        raise ModelProfileError("只有管理员可以发布或撤销共享模型。")
    if not isinstance(enabled, bool):
        raise ModelProfileError("共享开关必须为布尔值。")
    try:
        key = str(UUID(str(profile_id)))
    except ValueError as exc:
        raise ModelProfileError("请选择管理员自己保存的模型编号。") from exc
    if enabled:
        data = _admin_profiles(agent.home_paths)
        if key not in data["profiles"]:
            raise ModelProfileError("管理员模型配置不存在。")
        if data["providers"][data["profiles"][key]["provider_id"]].get("auth"):
            raise ModelProfileError("订阅/OAuth 登录仅供所属用户使用，不能跨用户共享。")
        resolved_model(data, key, capability=data["profiles"][key]["capability"])
    path = shared_catalog_path(agent.home_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked_json_path(path):
        keys = _read_catalog(agent.home_paths)
        keys.add(key) if enabled else keys.discard(key)
        write_json_file_atomic_unlocked(path, {"schema": _SCHEMA, "profiles": ["shared:" + value for value in sorted(keys)]})
