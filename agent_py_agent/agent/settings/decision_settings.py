# LLM: 界面/工具共用 owner→thread 锁顺序及 CAS；只修改原配置覆盖，不发网络请求、不创建 Agent、不重置阶段预算。
# 模块用途: 在原模型目录和原会话存储读取、字段修改或恢复继承，并读回有效值及版本。
from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import replace

from ..common.json_io import locked_json_path
from .decision_settings_projection import decision_profile, decision_settings_projection
from .decision_settings_schema import (
    DecisionSettingsAccessError,
    DecisionSettingsConflict,
    empty_decision_settings,
    validate_decision_field,
    validate_decision_settings,
)
from .model_profiles import _save_profiles, model_profiles_path, read_model_profiles
from .model_provider_schema import ModelProfileError
from .thread_model_selection import _require_owner


# LLM: 入口上下文必须来自原认证 owner；payload 不接受路径、owner 或 store，更不能创建默认 Agent 补身份。
# 函数用途: 在进行任何文件访问前检查可信 owner 上下文和明确请求结构。
def _validate_request(context: object, operation: str, payload: dict, thread_id: str) -> str:
    home = context.home_paths
    if any(not str(getattr(home, field, "") or "").strip() for field in ("owner_provider", "owner_kind", "owner_id")):
        raise DecisionSettingsAccessError("决策设置需要已验证的用户身份。")
    if type(operation) is not str or operation not in {"read", "patch", "reset"} or type(payload) is not dict:
        raise ModelProfileError("决策设置操作无效。")
    allowed = {"scope"} if operation == "read" else {"scope", "expected_revision", "changes" if operation == "patch" else "fields"}
    if set(payload) - allowed:
        raise ModelProfileError("决策设置请求包含未知字段。")
    scope = payload.get("scope", "owner")
    if type(scope) is not str or scope not in {"owner", "thread"} or scope == "thread" and not thread_id:
        raise ModelProfileError("会话临时设置需要当前会话编号。")
    return scope


# LLM: CAS 同时比较实际读取的 owner/thread 代次，不接受 bool 或省略字段来绕过版本复查。
# 函数用途: 拒绝基于旧快照的修改，让调用方重新读取最新设置。
def _check_revision(payload: dict, owner: dict, thread: dict) -> None:
    expected = payload.get("expected_revision")
    if type(expected) is not dict or set(expected) != {"owner", "thread"} or any(type(value) is not int or value < 0 for value in expected.values()):
        raise ModelProfileError("修改决策设置需要完整有效的 expected_revision。")
    if expected != {"owner": owner["revision"], "thread": thread["revision"]}:
        raise DecisionSettingsConflict("决策设置已被修改，请重新读取后再提交。")


# LLM: 只检查本次写入的 profile 引用，不让已有失效服务阻止关闭；恢复继承通过删除覆盖实现。
# 函数用途: 生成下一版本字段覆盖，不修改默认值或调用方传入对象。
def _patch_settings(context: object, data: dict, current: dict, operation: str, payload: dict) -> dict:
    overrides = dict(current["overrides"])
    if operation == "patch":
        changes = payload.get("changes")
        if type(changes) is not dict or not changes or len(changes) > 20:
            raise ModelProfileError("字段修改需要非空 changes 对象。")
        for key, value in changes.items():
            normalized = validate_decision_field(key, value)
            if key.endswith("profile_id") and normalized:
                decision_profile(context, data, normalized, require_enabled=False)
            overrides[key] = normalized
    else:
        fields = payload.get("fields")
        if type(fields) is not list or not fields or len(fields) > 20 or any(type(key) is not str for key in fields):
            raise ModelProfileError("恢复继承需要非空 fields 字段路径列表。")
        for key in fields:
            # 用同一字段白名单检查路径；删除允许当前没有覆盖的已登记字段。
            sample = False if key == "enabled" else "off" if key.endswith(".mode") else "" if key.endswith("profile_id") else 1.0
            validate_decision_field(key, sample)
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


# LLM: owner 锁持有期间调用原 thread.update_atomic；回执由锁内成功提交值产生，不受后续窗口写入污染。
# 函数用途: 原子修改会话临时覆盖，保留并发 Compact、模型选择及其他线程字段。
def _update_thread(context: object, data: dict, thread_id: str, operation: str, payload: dict) -> dict:
    before = {}

    # LLM: 原 thread 锁内核对身份及两层版本；仅替换 decision_settings，逻辑时钟和执行状态不在此修改。
    # 函数用途: 把字段 patch/reset 应用到最新线程快照。
    def update(latest):
        _check_thread_owner(context, latest)
        current = validate_decision_settings(latest.decision_settings)
        _check_revision(payload, data["decision_settings"], current)
        before.update(decision_settings_projection(context, data, latest, scope="thread"))
        return replace(latest, decision_settings=_patch_settings(context, data, current, operation, payload))

    saved = context.conversation_store.threads.update_atomic(thread_id, update)
    result = decision_settings_projection(context, data, saved, scope="thread")
    return {**result, "before": before}


# LLM: 原 owner 锁下完成 CAS/字段修改/原子保存/读回；若携带 thread，上层同时持有原 thread 锁以冻结继承版本。
# 函数用途: 修改长期覆盖后读回正式文件，不复制默认值、密钥或整份用户配置到回执。
def _owner_operation(context: object, path, data: dict, thread: object, operation: str, payload: dict, scope: str) -> dict:
    before = decision_settings_projection(context, data, thread, scope=scope)
    if operation == "read":
        return before
    temporary = thread.decision_settings if thread is not None else empty_decision_settings()
    _check_revision(payload, data["decision_settings"], temporary)
    updated = deepcopy(data)
    updated["decision_settings"] = _patch_settings(context, data, data["decision_settings"], operation, payload)
    _save_profiles(path, updated)
    return {**decision_settings_projection(context, read_model_profiles(path), thread, scope=scope), "before": before}


# LLM: 锁顺序固定 owner→thread，返回前已完成持久读回；不得在文件锁内执行设置通知或取消逻辑。
# 函数用途: 执行一次设置文件事务，供公共入口在锁外发布成功提交通知。
def _execute_transaction(context: object, operation: str, payload: dict, thread_id: str, scope: str, blocking: bool) -> dict:
    path = model_profiles_path(context.home_paths)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with locked_json_path(path, blocking=blocking):
        data = read_model_profiles(path)
        if scope == "thread" and operation != "read":
            return _update_thread(context, data, thread_id, operation, payload)
        if thread_id:
            store = context.conversation_store.threads
            with locked_json_path(store.storage.thread_path(thread_id), blocking=blocking):
                thread = _load_thread(context, thread_id)
                return _owner_operation(context, path, data, thread, operation, payload, scope)
        return _owner_operation(context, path, data, None, operation, payload, scope)


# LLM: 宿主可选非阻塞读取，两把原锁共用blocking；模型payload不能控制。保存后锁外通知失败不冒充未保存。
# 函数用途: 共用 read/patch/reset 服务，读回后使本进程失效的旧决策及时取消，不创建网络请求或 Agent。
def execute_decision_settings_operation(context: object, operation: str, payload: dict, *, thread_id: str = "", blocking: bool = True) -> dict:
    scope = _validate_request(context, operation, payload, thread_id)
    if type(blocking) is not bool or not blocking and operation != "read":
        raise ModelProfileError("非阻塞选项仅供宿主读取决策设置。")
    result = _execute_transaction(context, operation, payload, thread_id, scope, blocking)
    if operation != "read":
        try:
            from ..conversation.decision_policy import notify_decision_settings_changed

            notify_decision_settings_changed(context, result)
        except Exception:
            logging.getLogger(__name__).warning("决策设置已保存；进程内取消通知失败，应用前仍须重新核对版本。")
    return result
