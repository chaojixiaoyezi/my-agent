# LLM: 界面/工具共用 owner→thread 锁及 CAS；实验许可只由宿主专用参数提交，普通 patch/reset/restore 无授权权，不发网络。
# 模块用途: 在原模型目录和会话存储修改或恢复决策覆盖，按后台或会话作用范围读回有效值及版本。
from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import replace

from ..common.json_io import locked_json_path
from .decision_settings_projection import decision_profile, decision_settings_projection
from .decision_settings_schema import (
    DecisionSettingsAccessError,
    DecisionSettingsConflict,
    decision_field_scopes,
    empty_decision_settings,
    validate_decision_field,
    validate_decision_settings,
)
from .model_profiles import _save_profiles, model_profiles_path, read_model_profiles
from .model_provider_schema import ModelProfileError
from .thread_model_selection import _require_owner


# LLM: 入口上下文必须来自原认证 owner；restore 只提供同事务 set/unset，不接受路径、owner 或 store。
# 函数用途: 在进行任何文件访问前检查可信 owner 上下文和明确请求结构。
def _validate_request(context: object, operation: str, payload: dict, thread_id: str) -> str:
    home = context.home_paths
    if any(not str(getattr(home, field, "") or "").strip() for field in ("owner_provider", "owner_kind", "owner_id")):
        raise DecisionSettingsAccessError("决策设置需要已验证的用户身份。")
    if type(operation) is not str or operation not in {"read", "patch", "reset", "restore", "experiment_authorize", "experiment_revoke"} or type(payload) is not dict:
        raise ModelProfileError("决策设置操作无效。")
    allowed = {"scope"} if operation == "read" else {"scope", "expected_revision"}
    if operation == "patch":
        allowed.add("changes")
    elif operation == "reset":
        allowed.add("fields")
    elif operation == "restore":
        allowed.update(("set", "unset"))
    elif operation == "experiment_revoke":
        allowed.add("authorization_id")
    if set(payload) - allowed:
        raise ModelProfileError("决策设置请求包含未知字段。")
    scope = payload.get("scope", "owner")
    if type(scope) is not str or scope not in {"owner", "thread"} or scope == "thread" and not thread_id:
        raise ModelProfileError("会话临时设置需要当前会话编号。")
    if operation.startswith("experiment_") and scope != "thread":
        raise ModelProfileError("实验授权首片只支持当前会话。")
    return scope


# LLM: CAS 同时比较实际读取的 owner/thread 代次，不接受 bool 或省略字段来绕过版本复查。
# 函数用途: 拒绝基于旧快照的修改，让调用方重新读取最新设置。
def _check_revision(payload: dict, owner: dict, thread: dict) -> None:
    expected = payload.get("expected_revision")
    if type(expected) is not dict or set(expected) != {"owner", "thread"} or any(type(value) is not int or value < 0 for value in expected.values()):
        raise ModelProfileError("修改决策设置需要完整有效的 expected_revision。")
    if expected != {"owner": owner["revision"], "thread": thread["revision"]}:
        raise DecisionSettingsConflict("决策设置已被修改，请重新读取后再提交。")


# LLM: 新写字段使用原范围与模型目录校验；reset/restore 的 unset 可清理旧范围字段，restore 的 set/unset 必须同次提交。
# 函数用途: 从原事务快照生成下一版覆盖；恢复精确前值时只写一次，拒绝交集或空操作。
def _patch_settings(context: object, data: dict, current: dict, operation: str, payload: dict, *, scope: str) -> dict:
    overrides = dict(current["overrides"])
    if operation in {"patch", "restore"}:
        changes = payload.get("changes") if operation == "patch" else payload.get("set")
        removed = payload.get("unset") if operation == "restore" else []
        if (type(changes) is not dict or type(removed) is not list
                or not changes and not removed or len(changes) + len(removed) > len(decision_field_scopes())
                or any(type(key) is not str for key in removed) or len(removed) != len(set(removed))
                or set(changes).intersection(removed)):
            raise ModelProfileError("字段修改需要非空 changes 对象。" if operation == "patch"
                                    else "字段恢复需要非空且互不重叠的 set/unset。")
        for key, value in changes.items():
            normalized = validate_decision_field(key, value, scope=scope)
            if key.endswith("profile_id") and normalized:
                decision_profile(context, data, normalized, require_enabled=False)
            overrides[key] = normalized
        for key in removed:
            if key not in decision_field_scopes():
                raise ModelProfileError("决策设置包含未登记的字段或接入点。")
            overrides.pop(key, None)
    else:
        fields = payload.get("fields")
        if type(fields) is not list or not fields or len(fields) > len(decision_field_scopes()) or any(type(key) is not str for key in fields):
            raise ModelProfileError("恢复继承需要非空 fields 字段路径列表。")
        for key in fields:
            # 恢复继承不限制当前可写范围，否则旧版本遗留的线程后台字段将无法清理。
            if key not in decision_field_scopes():
                raise ModelProfileError("决策设置包含未登记的字段或接入点。")
            overrides.pop(key, None)
    return {**current, "revision": current["revision"] + 1, "overrides": overrides}


# LLM: thread 访问沿原身份检查，缺 owner 的旧记录仍受可信 store 上界；有 owner_home 时不能丢掉宿主归属再比较。
# 函数用途: 读取原线程并拒绝跨 owner 引用，不初始化会话模型或写显示数据。
def _load_thread(context: object, thread_id: str):
    thread = context.conversation_store.threads.load(thread_id)
    if thread is None:
        raise ModelProfileError("当前会话不存在，请重新打开会话。")
    _check_thread_owner(context, thread)
    return thread


# LLM: 与原 thread 模型选择共用身份检查；额外拒绝有持久 owner_home 却缺可信 home 的上下文。
# 函数用途: 在锁内核对最新会话归属，防止外部调用方传错存储或线程。
def _check_thread_owner(context: object, thread: object) -> None:
    try:
        _require_owner(context, thread)
    except ModelProfileError as exc:
        raise DecisionSettingsAccessError("决策设置不能访问其他用户的会话。") from exc
    if getattr(thread, "owner_home", "") and not getattr(context.home_paths, "owner_home_dir", ""):
        raise DecisionSettingsAccessError("决策设置缺少已验证的会话归属。")


# LLM: owner 锁持有期间调用原 thread.update_atomic；授权及撤销与原覆盖共用完整 CAS，不把普通配置操作升级成授权。
# 函数用途: 原子修改会话设置或唯一实验信封，保留 Compact、模型选择和其他线程状态。
def _update_thread(context: object, data: dict, thread_id: str, operation: str, payload: dict, authorization: dict | None) -> dict:
    before = {}

    # LLM: 原 thread 锁内核对身份及两层版本；授权使用整份宿主信封，非授权修改保留原信封且令其旧 revision 失效。
    # 函数用途: 在最新线程快照上提交字段操作或实验许可，保持其他状态不变。
    def update(latest):
        _check_thread_owner(context, latest)
        current = validate_decision_settings(latest.decision_settings)
        _check_revision(payload, data["decision_settings"], current)
        before.update(decision_settings_projection(context, data, latest, scope="thread"))
        if operation.startswith("experiment_"):
            from .decision_experiment import experiment_settings_transition

            updated = experiment_settings_transition(current, operation, payload, authorization, before)
            return replace(latest, decision_settings=updated)
        return replace(latest, decision_settings=_patch_settings(context, data, current, operation, payload, scope="thread"))

    saved = context.conversation_store.threads.update_atomic(thread_id, update)
    result = decision_settings_projection(context, data, saved, scope="thread")
    return {**result, "before": before}


# LLM: 原 owner 锁下按 owner 可写字段执行 CAS/保存/读回；restore 的 set/unset 一起提交，携带 thread 时冻结继承版本。
# 函数用途: 修改或恢复用户长期覆盖后读回正式文件；线程身份不会把本次 owner 写入变成临时覆盖。
def _owner_operation(context: object, path, data: dict, thread: object, operation: str, payload: dict, scope: str) -> dict:
    before = decision_settings_projection(context, data, thread, scope=scope)
    if operation == "read":
        return before
    temporary = thread.decision_settings if thread is not None else empty_decision_settings()
    _check_revision(payload, data["decision_settings"], temporary)
    updated = deepcopy(data)
    updated["decision_settings"] = _patch_settings(context, data, data["decision_settings"], operation, payload, scope="owner")
    _save_profiles(path, updated)
    return {**decision_settings_projection(context, read_model_profiles(path), thread, scope=scope), "before": before}


# LLM: 锁顺序固定 owner→thread，返回前已完成持久读回；不得在文件锁内执行设置通知或取消逻辑。
# 宿主非阻塞读取不取锁：目录与线程文件都经临时文件替换原子写入，单次写事务只改其中一个文件，
# 读到的总是已提交版本；读者之间不再互相挤成 settings_busy，调用方仍在调用前后复核版本。
# 函数用途: 执行一次设置文件事务，供公共入口在锁外发布成功提交通知。
def _execute_transaction(context: object, operation: str, payload: dict, thread_id: str, scope: str, blocking: bool,
                         authorization: dict | None) -> dict:
    path = model_profiles_path(context.home_paths)
    if operation == "read" and not blocking:
        thread = _load_thread(context, thread_id) if thread_id else None
        return _owner_operation(context, path, read_model_profiles(path), thread, operation, payload, scope)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with locked_json_path(path, blocking=blocking):
        data = read_model_profiles(path)
        if scope == "thread" and operation != "read":
            return _update_thread(context, data, thread_id, operation, payload, authorization)
        if thread_id:
            store = context.conversation_store.threads
            with locked_json_path(store.storage.thread_path(thread_id), blocking=blocking):
                thread = _load_thread(context, thread_id)
                return _owner_operation(context, path, data, thread, operation, payload, scope)
        return _owner_operation(context, path, data, None, operation, payload, scope)


# LLM: 授权只接宿主专用参数，不接受 payload 中自报许可；所有写入沿原 CAS/读回/锁外通知，设置本身不调用模型。
# 函数用途: 共用覆盖和实验许可事务；普通工具只开放读取与撤销，保存成功后令旧建议失效。
def execute_decision_settings_operation(context: object, operation: str, payload: dict, *, thread_id: str = "", blocking: bool = True,
                                        host_authorization: dict | None = None) -> dict:
    scope = _validate_request(context, operation, payload, thread_id)
    if (operation == "experiment_authorize") != (host_authorization is not None):
        raise DecisionSettingsAccessError("建立实验许可只接受宿主显式用户控制入口。")
    if type(blocking) is not bool or not blocking and operation != "read":
        raise ModelProfileError("非阻塞选项仅供宿主读取决策设置。")
    result = _execute_transaction(context, operation, payload, thread_id, scope, blocking, host_authorization)
    if operation == "experiment_revoke":
        from .decision_experiment import revoke_original_experiment_budget

        revoke_original_experiment_budget(context, result["experiment_authorization"])
    if operation != "read":
        try:
            from ..conversation.decision_policy import notify_decision_settings_changed

            notify_decision_settings_changed(context, result)
        except Exception:
            logging.getLogger(__name__).warning("决策设置已保存；进程内取消通知失败，应用前仍须重新核对版本。")
    return result
