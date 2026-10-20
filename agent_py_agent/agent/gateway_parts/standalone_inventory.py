# LLM: 只读原canonical责任/native源；不调用会redo恢复/刷新写入的ProcessSessionStore或registry列表，不以created/active字样判busy。
# 模块用途: 为正常停止来源与后继迁移核现有owner、运行责任和跨宿主native的稳定摘要。
from __future__ import annotations

import hashlib
import os
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from ..common.nofollow_fs import open_directory_beneath, read_bytes_beneath
from ..common.strict_json import load_strict_json
from ..conversation.input_dependencies import validate_input_dependency_record
from ..conversation.models import ConversationThread, ThreadTaskLink
from ..conversation.session_tasks import SESSION_TASK_STATUSES, SessionTask
from ..conversation.store_io import safe_file_stem
from ..owner_wake_discovery import _STORE_ROOT_PATTERNS
from ..tooling.process_cleanup_evidence import confirm_cleanup_reference, process_cleanup_reference
from ..tooling.process_registry import _process_instance_terminated
from ..tooling.process_session_records import validate_process_record
from ..tooling.process_session_store import process_session_store_root
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from .control_operation_service import GatewayControlOperationReceipt
from .multiprocess_drain_sources import _generation_source_entry_kind
from .owner_resources import resolve_owner_resource_configuration
from .standalone_migration_errors import StandaloneMigrationRejected
from .standalone_runtime_inventory import responsibility_digest, runtime_responsibility_snapshot


# LLM: namespace只记原目录身份与完整名称，不跟链接、不截断；最终全snapshot再读比较。
# 函数用途: 取得一个准确目录的稳定无写投影，缺失目录与空目录保持区分。
def _directory(root, parts):
    try:
        descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    except FileNotFoundError:
        return None, ()
    try:
        info = os.fstat(descriptor)
        names = tuple(sorted(os.listdir(descriptor)))
        current = open_directory_beneath(root, parts, require_dir_fd=True)
        try:
            after = os.fstat(current)
            if (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino) or tuple(sorted(os.listdir(descriptor))) != names:
                raise ValueError("standalone_namespace_changed")
        finally:
            os.close(current)
        return (info.st_dev, info.st_ino), names
    finally:
        os.close(descriptor)


# LLM: 固定原因与原ref用于行动，不回显载荷、命令、密钥或prompt。
# 函数用途: 让原Count0恢复器先收口准确未结责任。
def _reject(reason, ref):
    raise StandaloneMigrationRejected(reason, "保持数量0，通过原任务或进程会话入口恢复/停止该来源，再正常停止。", ref)


# LLM: 所有owner来自原目录/OwnerIdentity resolver；只排除确切受管runtime设施，不忽略任意隐藏目录。
# 函数用途: 完整枚举当前规范owner，不构造Agent或依靠进程内缓存。
def _owners(home, namespaces):
    owners = [resolve_owner_home(home)]
    for parts in (("owners",), ("owners", "local"), ("owners", "providers")):
        value = _directory(home, parts)
        namespaces["/".join(parts)] = value
        allowed = {"local", "providers"} if len(parts) == 1 else {"main", ".my-agent-runtime"}
        if parts != ("owners", "providers") and any(name not in allowed for name in value[1]):
            _reject("owner_namespace_unknown", "/".join(parts))
    for provider in namespaces["owners/providers"][1]:
        parts = ("owners", "providers", provider)
        namespaces["/".join(parts)] = _directory(home, parts)
        for bucket in namespaces["/".join(parts)][1]:
            if bucket not in {"users", "groups"}:
                _reject("owner_bucket_unknown", "/".join((*parts, bucket)))
            children = (*parts, bucket)
            namespaces["/".join(children)] = _directory(home, children)
            for name in namespaces["/".join(children)][1]:
                if name == ".my-agent-runtime":
                    _directory(home, (*children, name))
                    continue
                identity = (OwnerIdentity.provider_user(provider, name) if bucket == "users"
                            else OwnerIdentity.provider_group(provider, name))
                owners.append(resolve_owner_home(home, identity))
    return owners


# LLM: 原process_sessions摘要目录须能绑定现存owner；遗失owner不能让旧native藏在未枚举namespace里。
# 函数用途: 核共享native目录的完整直接集合，不调用Store恢复或删除孤儿记录。
def _native_namespaces(home, owners, namespaces):
    expected = {}
    for owner in owners:
        root = process_session_store_root(owner.home_dir, owner.home_dir)
        expected.setdefault(root.parent, set()).add(root.name)
    for ref, (_identity, names) in tuple(namespaces.items()):
        if ".my-agent-runtime" in names and (ref == "owners/local" or ref.count("/") == 3):
            expected.setdefault(home / ref / ".my-agent-runtime" / "process_sessions", set())
    for root, allowed in expected.items():
        parts = tuple(root.relative_to(home).parts)
        namespaces["/".join(parts)] = value = _directory(home, parts)
        if any(name not in allowed for name in value[1]):
            _reject("native_owner_namespace_unbound", "/".join(parts))


# LLM: 完整raw读取后立即记摘要；超限/坏UTF8/重复键/危险路径不转空值。
# 函数用途: 读取原JSON责任并绑定准确ref，不在摘要中保存正文。
def _json(root, parts, files):
    raw = read_bytes_beneath(root, parts, max_bytes=4 * 1024 * 1024, require_dir_fd=True)
    if raw is None:
        raise ValueError("standalone_source_disappeared")
    files["/".join(parts)] = hashlib.sha256(raw).hexdigest()
    return load_strict_json(raw)


# LLM: 原processing/control来源仅按原codec/locator解释；route/dispatch若属MP不能借Count0迁移洗掉。
# 函数用途: 确认旧Gateway没有正在处理的请求或未结控制操作，pending请求仍原样留给原恢复链。
def _gateway_sources(paths, namespaces, files):
    for parts in (("requests", "processing"), ("control_operations",)):
        namespaces["gateway/" + "/".join(parts)] = _directory(paths.root, parts)
        for name in namespaces["gateway/" + "/".join(parts)][1]:
            kind = _generation_source_entry_kind(paths.root, parts, name)
            if kind == "directory":
                nested = _directory(paths.root, (*parts, name))
                if nested[1]:
                    _reject("processing_auxiliary_unsettled", "/".join((*parts, name)))
                continue
            if kind == "file":
                raw = read_bytes_beneath(paths.root, (*parts, name), max_bytes=4 * 1024 * 1024, require_dir_fd=True)
                files["gateway/" + "/".join((*parts, name))] = hashlib.sha256(raw).hexdigest()
                continue
            row = _json(paths.root, (*parts, name), files)
            if parts[-1] == "processing":
                _reject("processing_request_present", "/".join((*parts, name)))
            receipt = GatewayControlOperationReceipt.from_dict(row)
            if receipt.dispatch is not None or receipt.operation_id + ".json" != name or receipt.state != "completed":
                _reject("control_operation_unsettled", "/".join((*parts, name)))


# LLM: 只用原纯codec/cleanup引用及registry自己的birth观察，绝不list/load/refresh/consume；原宿主在stop采样时尚存，launcher仅后继核。
# 函数用途: 核一owner已存在native完整退出事实，保留所有历史记录。
def _native_sources(home, owner, runtime, namespaces, files, check_launcher):
    root = process_session_store_root(owner.home_dir, owner.home_dir)
    parts = tuple(root.relative_to(home).parts)
    namespace = _directory(home, parts)
    namespaces["/".join(parts)] = namespace
    for name in namespace[1]:
        if name == ".process-sessions.redo.json":
            _reject("native_redo_requires_recovery", "/".join((*parts, name)))
        if name == ".process-sessions.lock":
            read_bytes_beneath(home, (*parts, name), max_bytes=16384, require_dir_fd=True)
            continue
        row = validate_process_record(_json(home, (*parts, name), files))
        if (row["session_id"] + ".json" != name or row["access_scope"]["owner_home"] != str(owner.home_dir)
                or row["access_scope"]["owner_id"] != owner.owner_id):
            _reject("native_owner_or_filename_conflict", "/".join((*parts, name)))
        reference = process_cleanup_reference(row)
        confirm_cleanup_reference(reference, row)
        if row.get("child_pid") and not reference["cleanup"]["instances"]:
            _reject("native_tree_receipt_missing", row["session_id"])
        keys = ("launcher", "pid", "child") if check_launcher else ("pid", "child")
        for key in keys:
            pid_key, birth_key = (("pid", "pid_birth_token") if key == "pid" else
                                  (key + "_pid", key + "_pid_birth_token" if key == "child" else "launcher_birth_token"))
            if row.get(pid_key) and not _process_instance_terminated(row[pid_key], row[birth_key]):
                _reject("native_instance_alive_or_unknown", row["session_id"])
        scope = row.get("execution_scope", {})
        if scope.get("attempt_id"):
            expected = runtime["executions"].get(scope["attempt_id"])
            if expected is None or expected[:2] != (scope["run_id"], scope["root_task_id"]):
                _reject("native_runtime_ref_conflict", row["session_id"])


# LLM: 路径复用原owner/config resolver，不创建scope或Agent；当前与既有规范workspace store全部进入稳定源。
# 函数用途: 找到owner原会话目录，保留历史与未来输入位置。
def _conversation_roots(home, owner, config, namespaces):
    _scoped, _config, paths = resolve_owner_resource_configuration(SimpleNamespace(home_root=home, config=config), owner.identity)
    roots = {paths["conversation_workspace"]}
    owner_parts = tuple(owner.home_dir.relative_to(home).parts)
    for pattern in _STORE_ROOT_PATTERNS:
        candidates = [owner_parts]
        for segment in pattern.split("/"):
            if segment == "*":
                expanded = []
                for parts in candidates:
                    namespaces["/".join(parts)] = _directory(home, parts)
                    expanded.extend((*parts, name) for name in namespaces["/".join(parts)][1])
                candidates = expanded
            else:
                candidates = [(*parts, segment) for parts in candidates]
        roots.update(home.joinpath(*parts) for parts in candidates)
    return sorted(roots)


# LLM: 只确认原输入协议与准确owner/root/run/thread refs，不改变pending/armed/期限或取得执行权。
# 函数用途: 将输入归属核对和目录遍历分开，保留历史与未来意图的完整检查。
def _input_reference(row, owner, runtime, references, name):
    links, threads = references
    value = validate_input_dependency_record(row, owner.identity, row.get("input_id"))
    if hashlib.sha256(value["input_id"].encode()).hexdigest() + ".json" != name:
        _reject("input_filename_conflict", name)
    if value["status"] != "pending":
        return
    scope = value["scope"]
    task = runtime["tasks"].get(scope["root_task_id"])
    link = links.get(scope["root_task_id"])
    if (task is None or scope["thread_id"] not in threads or link is None
            or link.status != "active" or value["input_id"] not in link.input_dependency_refs):
        _reject("input_runtime_ref_missing", value["input_id"])
    if not any(execution[:2] == (scope["run_id"], scope["root_task_id"])
               for execution in runtime["executions"].values()):
        _reject("input_run_ref_missing", value["input_id"])


# LLM: 逐类调用原codec与文件ID绑定；独立早返回不改变类型/refs门，不将未知记录当空。
# 函数用途: 核目录中的一个规范会话责任，让遍历与协议分开。
def _conversation_record(kind, row, name, owner, runtime, references):
    links, threads = references
    if kind == "session_tasks/inputs":
        _input_reference(row, owner, runtime, references, name)
        return
    if kind == "tasks":
        value = ThreadTaskLink.from_dict(row)
        links[value.task_id] = value
        if safe_file_stem(value.task_id) + ".json" != name:
            _reject("task_filename_conflict", name)
        return
    if kind == "threads":
        value = ConversationThread.from_dict(row)
        threads.add(value.thread_id)
        if safe_file_stem(value.thread_id) + ".json" != name:
            _reject("thread_filename_conflict", name)
        return
    value = SessionTask.from_dict(row)
    if value.status not in SESSION_TASK_STATUSES or safe_file_stem(value.task_id) + ".json" != name:
        _reject("session_task_source_invalid", name)


# LLM: 输入只核原协议和owner/root/thread refs；pending/active是持久意图，不因此判物理busy或更改期限。
# 函数用途: 只读当前会话责任，确保未来输入与原历史不被迁移丢弃。
def _conversation_sources(home, owner, root, runtime, namespaces, files):
    base = tuple(root.relative_to(home).parts)
    links, threads = {}, set()
    for kind in ("tasks", "session_tasks", "threads", "session_tasks/inputs"):
        parts = (*base, *kind.split("/"))
        namespace = _directory(home, parts)
        namespaces["/".join(parts)] = namespace
        for name in namespace[1]:
            if kind == "session_tasks" and name in {"inputs", "dedupe", "body"}:
                _directory(home, (*parts, name))
                continue
            if name.endswith(".lock"):
                read_bytes_beneath(home, (*parts, name), max_bytes=16384, require_dir_fd=True)
                continue
            row = _json(home, (*parts, name), files)
            _conversation_record(kind, row, name, owner, runtime, (links, threads))


# LLM: 仅真实持锁Count0正常stop writer调用，先建立原SQLite读取设施；不把这一准备采样当责任证据，迁移后继不调用。
# 函数用途: 让原ro/existing连接可在最终双读前准备自己的WAL/SHM，错误仍由停止来源记录为blocker。
def prepare_standalone_read_facilities(home):
    for owner in _owners(home, {}):
        runtime_responsibility_snapshot(owner, prepare_read_facilities=True)


# LLM: 首末采样由调用方比较整个返回值；仅规范责任行摘要，不写DB/redo/日志或取执行权。
# 函数用途: 汇总Count0迁移所需的当前原来源，不建立新业务账或历史出生观察器。
def standalone_inventory(home: Path, config, paths, *, check_launcher=True):
    namespaces, files, databases = {}, {}, {}
    owners = _owners(home, namespaces)
    _native_namespaces(home, owners, namespaces)
    _gateway_sources(paths, namespaces, files)
    for owner in owners:
        parts = tuple(owner.home_dir.relative_to(home).parts)
        namespaces["/".join(parts)] = _directory(home, parts)
        if namespaces["/".join(parts)][0] is None:
            _reject("owner_home_missing", owner.owner_id)
        runtime = runtime_responsibility_snapshot(owner)
        databases[owner.owner_id] = runtime["digest"]
        _native_sources(home, owner, runtime, namespaces, files, check_launcher)
        for root in _conversation_roots(home, owner, config, namespaces):
            _conversation_sources(home, owner, root, runtime, namespaces, files)
    return {"namespaces": namespaces, "files": files, "databases": databases,
            "owners": [asdict(owner.identity) for owner in owners]}
