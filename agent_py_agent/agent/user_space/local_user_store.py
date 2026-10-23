# LLM: local_users为账号状态唯一权威；读取不物化目录，坏文件不回退为不存在或enabled。
# 模块用途: 保存普通本地账号、原始操作绑定与发布回执。
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    ensure_private_dir,
    open_directory_beneath,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from ..plugin_host.errors import HostAPIError
from ..plugin_host.owner_context import reject_constant, unique_object, validate_local_user_id
from .owner_resolver import OwnerIdentity, _owner_home_result


# LLM: 只保存公开账号字段；配额值仍以owner quota.json为准，运行状态未知不能伪造False。
# 类用途: 提供不可变的普通账号状态投影。
@dataclass(frozen=True)
class LocalUser:
    user_id: str
    status: str
    revision: int
    display_name: str
    created_at: str
    updated_at: str
    quota_revision: int
    has_running_work: bool | None = None

    # LLM: 账号身份总是普通local/user，不接受插件提供provider或kind。
    # 函数用途: 构造与注册编号一一对应的宿主身份。
    @property
    def owner(self):
        return OwnerIdentity("local", "user", self.user_id)

    # LLM: 不输出宿主根目录、操作来源或内部回执。
    # 函数用途: 返回用户接口允许展示的状态字段。
    def to_dict(self):
        result = asdict(self)
        result["owner"] = asdict(self.owner)
        return result


# LLM: 领域层只投影已核验JSON；禁止构造参数展开未知字段。
# 函数用途: 严格解析账号状态，损坏时由调用方转成安全领域错误。
def parse_local_user(value, expected_user_id):
    required = {"user_id", "status", "revision", "display_name", "created_at", "updated_at", "quota_revision"}
    if type(value) is not dict or not required <= set(value):
        raise ValueError("invalid account")
    if value["user_id"] != expected_user_id:
        raise ValueError("account identity changed")
    if value["status"] not in {"provisioning", "provision_failed", "enabled", "disabled"}:
        raise ValueError("invalid account status")
    for field in ("revision", "quota_revision"):
        if type(value[field]) is not int or value[field] < 1:
            raise ValueError("invalid account revision")
    for field in ("display_name", "created_at", "updated_at"):
        if type(value[field]) is not str:
            raise ValueError("invalid account text")
    return LocalUser(value["user_id"], value["status"], value["revision"], value["display_name"],
                     value["created_at"], value["updated_at"], value["quota_revision"])


# LLM: 固定可信宿主根，账号写入由生命周期服务持短锁完成；只读不获取会创建文件的锁。
# 类用途: 提供无链接、有界、原子账号存储和操作索引。
class LocalUserStore:
    # LLM: 只规范化可信宿主输入，不产生文件或推断角色。
    # 函数用途: 绑定账号存储根目录。
    def __init__(self, root):
        self.root = Path(root).resolve()

    # LLM: 与任务约定布局一致，复用完整OwnerHomeResult字段工厂，不调用ensure_owner_home。
    # 函数用途: 冷投影普通本地用户的独立目录。
    def owner_home(self, user_id):
        uid = validate_local_user_id(user_id)
        home = self.root / "owners" / "local" / uid
        result = _owner_home_result(self.root, OwnerIdentity("local", "user", uid), home)
        return replace(result, owner_id=f"local/{uid}")

    # LLM: 准确编号先校验；损坏、越界、硬链接均关闭而非视为缺失。
    # 函数用途: 读取当前账号公开状态。
    def get(self, user_id):
        record = self.read_record(user_id)
        return parse_local_user(record["user"], user_id) if record is not None else None

    # LLM: 不扫描全盘，只读管理/创建各至多100项的并集，结果排序且无分页泄漏。
    # 函数用途: 列出授权管理范围内已登记的账号。
    def list_registered(self, user_ids):
        if len(user_ids) > 200:
            raise HostAPIError("INVALID_SCHEMA")
        ids = tuple(sorted({validate_local_user_id(uid) for uid in user_ids}))
        return tuple(user for uid in ids if (user := self.get(uid)) is not None)

    # LLM: 文件根固定，单次读取最多2MiB，账户身份与schema双重核验。
    # 函数用途: 读取生命周期内部权威记录。
    def read_record(self, user_id):
        uid = validate_local_user_id(user_id)
        record = self.read_json(("local_users", f"{uid}.json"))
        if record is None:
            return None
        try:
            if record.get("schema") != "local-user.v1":
                raise ValueError("invalid schema")
            user = parse_local_user(record["user"], uid)
            if type(record["provision"]) is not dict or type(record["operations"]) is not dict:
                raise ValueError("invalid receipt")
            if user.status in {"enabled", "disabled"}:
                provision = record["provision"]
                receipt = record["operations"].get(provision.get("operation_id"))
                if provision.get("phase") != "committed" or receipt is None:
                    raise ValueError("uncommitted account")
                if parse_local_user(receipt, uid).status != "enabled":
                    raise ValueError("invalid provisioning receipt")
        except (ValueError, KeyError, TypeError) as exc:
            raise HostAPIError("STORE_UNAVAILABLE", 503) from exc
        return record

    # LLM: 仅允许服务构造的相对组件；重复字段、非有限数及过深JSON均视为损坏。
    # 函数用途: 读取账号或不可变操作索引JSON。
    def read_json(self, parts):
        try:
            data = read_bytes_beneath(self.root, parts, max_bytes=2097152, require_dir_fd=True)
            if data is None:
                return None
            value = json.loads(data, object_pairs_hook=unique_object, parse_constant=reject_constant)
            if type(value) is not dict:
                raise ValueError("invalid record")
            return value
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            raise HostAPIError("STORE_UNAVAILABLE", 503) from exc

    # LLM: 调用方必须先持操作及用户锁；只做原子替换和耐久同步，不另取反向锁。
    # 函数用途: 写入一份完整记录，拒绝大于读回上限的数据。
    def write_json(self, parts, value):
        try:
            data = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
            if len(data) > 2097152:
                raise ValueError("record too large")
            write_bytes_atomic_beneath(self.root, parts, data)
        except (OSError, ValueError, TypeError) as exc:
            raise HostAPIError("STORE_UNAVAILABLE", 503) from exc

    # LLM: 与读路径同一UID，账号发布和终态回执在一份原子文件提交。
    # 函数用途: 保存一个用户的生命周期记录。
    def save(self, user_id, record):
        uid = validate_local_user_id(user_id)
        self.write_json(("local_users", f"{uid}.json"), record)

    # LLM: 操作锁在用户锁外，二者均在quota/JSON锁外；不在锁内等待模型、批准或进程。
    # 函数用途: 用现有跨线程跨进程锁保护短用户事务。
    @contextmanager
    def locked(self, parts):
        try:
            ensure_private_dir(self.root)
            parent = self.root.joinpath(*parts[:-1])
            ensure_private_dir(parent, canonical_root=self.root)
            before = self.directory_identity(parts[:-1])
            with locked_json_path(self.root.joinpath(*parts)):
                if self.directory_identity(parts[:-1]) != before:
                    raise HostAPIError("STORE_UNAVAILABLE", 503)
                yield
        except OSError as exc:
            raise HostAPIError("STORE_UNAVAILABLE", 503) from exc

    # LLM: 以严格dir_fd打开完整链，返回真实目录dev/inode；不跟随链接或创建目录。
    # 函数用途: 绑定用户物化目录身份，检测删除重建或调包。
    def directory_identity(self, parts):
        descriptor = open_directory_beneath(self.root, parts, require_dir_fd=True)
        try:
            info = os.fstat(descriptor)
            return [info.st_dev, info.st_ino]
        finally:
            os.close(descriptor)

    # LLM: 注册表enabled还需原目录身份和独立维护状态共同允许。
    # 函数用途: 在执行边界读取当前有效用户并核对修订。
    def require_enabled(self, user_id, revision=None):
        record = self.read_record(user_id)
        if record is None:
            raise HostAPIError("USER_NOT_FOUND", 404)
        user = parse_local_user(record["user"], user_id)
        if user.status != "enabled":
            raise HostAPIError("USER_DISABLED", 403)
        if revision is not None and revision != user.revision:
            raise HostAPIError("USER_REVISION_CHANGED", 409)
        try:
            identity = self.directory_identity(("owners", "local", user_id))
            if identity != record["provision"]["home_identity"]:
                raise ValueError("directory changed")
            status = self.read_json(("owners", "local", user_id, "owner_status.json"))
            if status is not None and status.get("status") != "active":
                raise HostAPIError("USER_DISABLED", 403)
        except HostAPIError:
            raise
        except (OSError, ValueError, KeyError) as exc:
            raise HostAPIError("STORE_UNAVAILABLE", 503) from exc
        return user
