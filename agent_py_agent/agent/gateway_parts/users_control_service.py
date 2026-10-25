# LLM: 管理入口只认已认证 local/main，沿原 LocalUserStore/Lifecycle；兼容旧操作对象与 c1 原生密码接口，不建立第二账号库。
# 模块用途: 为 Gateway、IM、direct 和管理员代理提供同源用户管理，回执只输出公开字段。
from __future__ import annotations

import inspect
import uuid

from ..conversation.control_commands import ConversationControlResult
from ..plugin_host.errors import HostAPIError
from ..session_lock.passwords import hash_password, validate_password_policy
from ..user_space.approval_mode import is_permission_admin
from ..user_space.local_user_lifecycle import LocalUserLifecycle, LocalUserOperation, now_utc
from ..user_space.local_user_store import LocalUserStore, parse_local_user, validate_local_user_id
from ..user_space.owner_admin_controls import (
    _validate_change,
    list_local_user_ids,
    read_owner_admin_controls,
    set_owner_admin_controls,
)
from ..user_space.owner_resolver import OwnerIdentity, home_paths_with_owner

_PUBLIC_FIELDS = frozenset({"user_id", "status", "revision", "display_name", "created_at", "updated_at",
                            "quota_revision", "has_running_work", "has_password", "owner"})
_USER_ACTIONS = frozenset({"create_user", "list_users", "get_user", "set_user_password", "enable_user", "disable_user"})


# LLM: 仅在 create 已成功返回后构造；后续写入失败不代表账号未创建，不删除账号或猜测失败步骤是否已经提交。
# 类用途: 明确交代账号已创建、初始化阶段未确认及不重复创建的补完动作。
class LocalUserInitializationIncomplete(HostAPIError):
    # LLM: 公开内容只含已校验 UID、固定阶段及动作名，不保留底层异常、密码或散列。
    # 函数用途: 保存部分创建的安全恢复信息。
    def __init__(self, uid, stage, pending_actions):
        super().__init__("USER_INITIALIZATION_INCOMPLETE", 409, outcome="unknown",
                         message="账号已创建，但初始化尚未确认；请按回执补完，不要重复创建。")
        self.public_details = {"account_created": True, "owner_id": f"local/{uid}",
            "initialization_stage": stage, "stage_outcome": "unconfirmed", "pending_actions": list(pending_actions)}


# LLM: owner_id 只能是同一普通本地 UID 的显式别名；同时提供 user_id 时必须一致，不允许目标偷换。
# 函数用途: 将管理参数归一为一个经过严格校验的本地用户名。
def local_user_id(params):
    refs = [params[key] for key in ("user_id", "owner_id") if key in params]
    ids = []
    for ref in refs:
        if type(ref) is not str:
            raise ValueError("invalid user reference")
        parts = ref.strip().split("/")
        if parts[:2] == ["local", "users"] and len(parts) == 3:
            ref = parts[2]
        elif parts[:1] == ["local"] and len(parts) == 2:
            ref = parts[1]
        elif len(parts) != 1:
            raise ValueError("invalid user reference")
        ids.append(validate_local_user_id(ref.strip()))
    if not ids or len(set(ids)) != 1:
        raise ValueError("conflicting user reference")
    return ids[0]


# LLM: 从同一次权威记录构建白名单公开视图；public_view 存在时复用，不允许 hash 或内部回执进入输出。
# 函数用途: 展示普通账号状态、密码是否设置和规范 Owner ID。
def local_user_view(store, uid):
    record = store.read_record(uid)
    if record is None:
        raise HostAPIError("USER_NOT_FOUND", 404)
    user = parse_local_user(record["user"], uid)
    public = user.public_view() if hasattr(user, "public_view") else user.to_dict()
    view = {key: value for key, value in public.items() if key in _PUBLIC_FIELDS}
    view.update(owner_id=f"local/{uid}", has_password=bool(public.get("has_password", record["user"].get("password_hash"))))
    return view


# LLM: 只按实际签名适配 operation 或 c1 的幂等键/actor 参数；不捕获 TypeError 后重做副作用。
# 函数用途: 将同一管理员操作交给当前版本的原生生命周期方法。
def _lifecycle_call(lifecycle, action, uid, arguments=None):
    method = getattr(lifecycle, action)
    parameters = inspect.signature(method).parameters
    key = uuid.uuid4().hex
    options = {"operation": LocalUserOperation("admin-controls", "0" * 64, key), "idempotency_key": key,
               "actor_owner": OwnerIdentity.local_main(), **(arguments or {})}
    if "quotas" in parameters and "quota" in options:
        options["quotas"] = options.pop("quota")
    return method(uid, **{name: value for name, value in options.items() if name in parameters})


# LLM: 优先复用 c1 原生 set_password；旧版仅在原用户锁内 CAS 修订与原子保存 hash，慢哈希在锁外。
# 函数用途: 设置登录密码，不保存或返回密码明文。
def set_local_user_password(lifecycle, uid, password):
    if type(password) is not str:
        raise ValueError("password required")
    validate_password_policy(password)
    user = lifecycle.store.get(uid)
    if user is None:
        raise HostAPIError("USER_NOT_FOUND", 404)
    if hasattr(lifecycle, "set_password"):
        return _lifecycle_call(lifecycle, "set_password", uid, {"password": password, "expected_revision": user.revision})
    hashed = hash_password(password)
    store = lifecycle.store
    with store.locked(("local_users", f"{uid}.json")):
        record = store.read_record(uid)
        if record is None or record["user"]["revision"] != user.revision:
            raise HostAPIError("REVISION_CONFLICT", 409)
        record["user"].update(password_hash=hashed, revision=user.revision + 1, updated_at=now_utc())
        store.save(uid, record)


_DEFAULT_SET_LOCAL_USER_PASSWORD = set_local_user_password


# LLM: 创建前校验密码、展示名、配额和控制；旧接口后续改密失败明确报告已创建及补完动作，不盲目回滚并发数据。
# 函数用途: 用原事务创建用户，并按当前生命周期版本设置初始密码。
def _create_user(lifecycle, uid, params):
    from ..user_space.owner_quota import LocalUserQuotaLimits
    password = params.get("password")
    if password is not None:
        if type(password) is not str:
            raise ValueError("invalid password")
        validate_password_policy(password)
    display = params.get("display_name", "")
    if type(display) is not str or len(display) > 80:
        raise ValueError("invalid display name")
    quota = params.get("quotas")
    LocalUserQuotaLimits.parse(quota)
    arguments = {"display_name": display, "quota": quota}
    native_password = (
        "password" in inspect.signature(lifecycle.create).parameters
        and set_local_user_password is _DEFAULT_SET_LOCAL_USER_PASSWORD
    )
    if native_password:
        arguments["password"] = password
    _lifecycle_call(lifecycle, "create", uid, arguments)
    if password is not None and not native_password:
        try:
            set_local_user_password(lifecycle, uid, password)
        except (ValueError, OSError) as exc:
            pending = ["set_user_password"] + (["set_controls"] if params.get("controls") is not None else [])
            raise LocalUserInitializationIncomplete(uid, "password", pending) from exc


# LLM: store 是账号唯一权威；构造器只在声明支持时接收 gateway_root，让合入 c1 后使用原生任务/唤醒撤销。
# 函数用途: 创建兼容当前基线和 c1 的生命周期服务。
def _lifecycle(store, gateway_root):
    options = {"gateway_root": gateway_root} if "gateway_root" in inspect.signature(LocalUserLifecycle).parameters else {}
    return LocalUserLifecycle(store, **options)


# LLM: 再核可信管理员门及原全局治理拒门；账号与 policy 保持原权威，创建后控制失败返回部分完成，不冒充无副作用。
# 函数用途: 为斜杠命令与管理员工具执行同一用户管理动作，返回不含秘密的结构化结果。
def manage_local_users(home, action, params, *, gateway_root=None):
    if not is_permission_admin(home):
        raise PermissionError("administrator required")
    if action not in _USER_ACTIONS:
        raise ValueError("invalid user action")
    if action not in {"get_user", "list_users"}:
        from ..common.global_write_guard import require_global_writer
        require_global_writer("owner_admin_controls")
    store = LocalUserStore(home.root)
    if action == "list_users":
        users = []
        for uid in list_local_user_ids(home):
            target = home_paths_with_owner(home, store.owner_home(uid))
            users.append({**local_user_view(store, uid), "controls": read_owner_admin_controls(target)})
        return {"ok": True, "action": action, "users": users}
    uid = local_user_id(params)
    target = home_paths_with_owner(home, store.owner_home(uid))
    controls = params.get("controls")
    if controls is not None:
        _validate_change(home, target, controls)
    lifecycle = _lifecycle(store, gateway_root)
    if action == "create_user":
        _create_user(lifecycle, uid, params)
        if controls is not None:
            try:
                set_owner_admin_controls(home, target, controls, actor="local/main")
            except (ValueError, OSError) as exc:
                raise LocalUserInitializationIncomplete(uid, "controls", ["set_controls"]) from exc
    elif action == "set_user_password":
        set_local_user_password(lifecycle, uid, params.get("password"))
    elif action in {"disable_user", "enable_user"}:
        _change_user_status(lifecycle, home, target, uid, action, params)
    return {"ok": True, "action": action, "owner_id": target.owner_id, "user": local_user_view(store, uid),
            "controls": read_owner_admin_controls(target)}


# LLM: 启停始终提交读取到的准确修订，原生 lifecycle 负责撤销；启用只清管理员 disabled，不顺带放开其它开关。
# 函数用途: 修改账号启停状态并保留管理员其它控制。
def _change_user_status(lifecycle, home, target, uid, action, params):
    user = lifecycle.store.get(uid)
    if user is None:
        raise HostAPIError("USER_NOT_FOUND", 404)
    arguments = {"expected_revision": user.revision}
    if action == "disable_user":
        arguments["reason"] = params.get("reason", "")
    _lifecycle_call(lifecycle, "disable" if action == "disable_user" else "enable", uid, arguments)
    if action == "enable_user" and read_owner_admin_controls(target).get("disabled"):
        set_owner_admin_controls(home, target, {"disabled": False}, actor="local/main")


# LLM: Gateway 权限只取已认证 scope，不借 base agent 的管理员身份；入口不加载目标用户 Agent 或模型。
# 函数用途: 处理 Gateway/IM 用户管理命令。
def execute_users_control(agent, paths, command, scope):
    from .control_service import resolve_gateway_scope_owner
    if resolve_gateway_scope_owner(agent, scope) != OwnerIdentity.local_main():
        return _admin_required()
    return _execute_users(agent.home_paths, command, paths.root if paths else None)


# LLM: direct 只核宿主真实 home，不读取 Gateway 凭据或信任模型参数。
# 函数用途: 本地终端直接执行用户管理。
def execute_users_direct(agent, command):
    home = getattr(agent, "home_paths", None)
    if not is_permission_admin(home):
        return _admin_required()
    return _execute_users(home, command, None)


# LLM: 拒绝在任何读写之前返回稳定错误码，不含用户输入。
# 函数用途: 统一非管理员命令回执。
def _admin_required():
    return ConversationControlResult("users", False, "只有本机管理员能管理本地用户。", error_code="COMMAND_ADMIN_REQUIRED")


# LLM: 错误文案不拼接原异常和参数；部分创建必须返回补完信息；成功只用公开视图，停用不承诺资源已物理退出。
# 函数用途: 将共享事务结果转换成中文命令回执。
def _execute_users(home, command, gateway_root):
    if not command.valid:
        return ConversationControlResult("users", False, command.usage)
    mapping = {"list": "list_users", "info": "get_user", "create": "create_user", "password": "set_user_password",
               "enable": "enable_user", "disable": "disable_user"}
    parts = command.value.split(maxsplit=3)
    params = {"user_id": command.name}
    if command.operation in {"create", "password"}:
        params["password"] = parts[2] if len(parts) >= 3 else None
    if command.operation == "create":
        params["display_name"] = parts[3] if len(parts) == 4 else ""
    if command.operation == "disable":
        params["reason"] = " ".join(parts[2:])
    try:
        report = manage_local_users(home, mapping[command.operation], params, gateway_root=gateway_root)
    except LocalUserInitializationIncomplete as exc:
        return ConversationControlResult("users", False, exc.message, error_code=exc.code, details=exc.public_details)
    except HostAPIError as exc:
        return ConversationControlResult("users", False, "用户管理未完成，请核对用户名、状态或存储。", error_code=exc.code)
    except (ValueError, KeyError):
        return ConversationControlResult("users", False, "用户参数无效；密码至少8位，须含大小写字母和数字。", error_code="TOOL_INVALID_ARGUMENTS")
    except (OSError, PermissionError):
        return ConversationControlResult("users", False, "用户管理未完成；请重新查询当前状态，勿盲目重试写入。", error_code="TOOL_PERSISTENCE_FAILED")
    if command.operation == "list":
        lines = [f"{row['user_id']} · {row['status']} · {row['owner_id']}" for row in report["users"]]
        return ConversationControlResult("users", True, "本地普通用户：\n" + ("\n".join(lines) or "暂无账号。"), details={"users": report["users"]})
    user = report["user"]
    message = f"用户 {user['user_id']} · {user['status']} · {user['owner_id']} · 密码{'已设置' if user['has_password'] else '未设置'}。"
    if command.operation == "disable":
        message += "账号已停用；活动资源是否停止需另行核对。"
    return ConversationControlResult("users", True, message, details={"user": user, "controls": report["controls"]})
