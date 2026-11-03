# LLM: 只读原canonical责任/native源；不调用会redo恢复/刷新写入的ProcessSessionStore或registry列表，不以created/active字样判busy。
# 模块用途: 为正常停止来源与后继迁移核现有owner、运行责任和跨宿主native的稳定摘要。
from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from ..common.nofollow_fs import (
    _openat_directory,
    _openat_file,
    open_directory_beneath,
    open_readonly_file_beneath,
    read_bytes_beneath,
)
from ..common.opaque_id import validate_opaque_id
from ..common.path_segments import safe_path_segment
from ..common.strict_json import load_strict_json
from ..contracts.tool_approval import ToolApprovalDecision
from ..conversation.input_dependencies import validate_input_dependency_record
from ..conversation.models import ConversationThread, ThreadTaskLink
from ..conversation.session_tasks import SESSION_TASK_STATUSES, SessionTask
from ..conversation.store_io import safe_file_stem
from ..owner_wake_discovery import _STORE_ROOT_PATTERNS
from ..runtime_db.operations import (
    AGENT_RUN_TERMINAL_STATUSES,
    OP_CANCELLED,
    OP_FAILED,
    OP_SUCCEEDED,
)
from ..runtime_db.repository import RuntimeRepository
from ..runtime_db.schema import runtime_db_path
from ..runtime_errors import DataCorruptionError
from ..tooling.process_cleanup_evidence import confirm_cleanup_reference, process_cleanup_reference
from ..tooling.process_registry import _process_instance_terminated
from ..tooling.process_session_records import validate_process_record
from ..tooling.process_session_store import process_session_store_root
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from .command_stream_protocol import command_approval_path
from .control_operation_service import GatewayControlOperationReceipt
from .multiprocess_drain_sources import _generation_source_entry_kind
from .owner_resources import resolve_owner_resource_configuration
from .standalone_migration_errors import StandaloneMigrationRejected
from .standalone_runtime_inventory import (
    _same_database,
    _sqlite_facilities,
    responsibility_digest,
    runtime_responsibility_snapshot,
)


# LLM: 目录遍历只持原root的FD链；失败关闭，调用方负责关闭成功返回的描述符。
# 函数用途: 从原root FD逐层核目录；非目录或链接由typed openat拒绝，不当作目录传给公共opener。
def _directory_descriptor(root, parts):
    descriptor = open_directory_beneath(root, (), require_dir_fd=True)
    try:
        for name in parts:
            child = _openat_directory(descriptor, name)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


# LLM: 名称过滤不得吞掉未知责任，仅精确普通Finder元数据不进入库存。
# 函数用途: 只忽略精确且no-follow为普通文件的Finder元数据，同名链接、目录及其它隐藏叶仍进入检查。
def _directory_names(descriptor):
    return tuple(sorted(name for name in os.listdir(descriptor)
                        if name != ".DS_Store" or not stat.S_ISREG(
                            os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode)))


# LLM: namespace只记原目录身份与完整名称，不跟链接、不截断；最终全snapshot再读比较。
# 函数用途: 取得一个准确目录的稳定无写投影，缺失目录与空目录保持区分。
def _directory(root, parts):
    try:
        descriptor = _directory_descriptor(root, parts)
    except FileNotFoundError:
        return None, ()
    try:
        info = os.fstat(descriptor)
        names = _directory_names(descriptor)
        current = _directory_descriptor(root, parts)
        try:
            after = os.fstat(current)
            if (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino) or _directory_names(descriptor) != names:
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
# 函数用途: 枚举真实规范owner；local id经原resolver且逐字不变，不跟链接、不构造Agent。
def _owners(home, namespaces):
    owners = [resolve_owner_home(home)]
    for parts in (("owners",), ("owners", "local"), ("owners", "providers")):
        value = _directory(home, parts)
        namespaces["/".join(parts)] = value
        if parts == ("owners",) and any(name not in {"local", "providers"} for name in value[1]):
            _reject("owner_namespace_unknown", "/".join(parts))
    for name in namespaces["owners/local"][1]:
        parts = ("owners", "local", name)
        if name not in {"main", ".my-agent-runtime"}:
            identity = OwnerIdentity.local_user(name)
            if identity.owner_id != name:
                _reject("owner_namespace_unknown", "/".join(parts))
            owners.append(resolve_owner_home(home, identity))
        namespaces["/".join(parts)] = _directory(home, parts)
        if namespaces["/".join(parts)][0] is None:
            _reject("owner_home_missing", "/".join(parts))
    for provider in namespaces["owners/providers"][1]:
        parts = ("owners", "providers", provider)
        if safe_path_segment(provider) != provider:
            _reject("owner_namespace_unknown", "/".join(parts))
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
                if identity.provider != provider or identity.owner_id != name:
                    _reject("owner_namespace_unknown", "/".join((*children, name)))
                _directory(home, (*children, name))
                owners.append(resolve_owner_home(home, identity))
    return owners


# LLM: 只返回当前owner的原存储候选，调用方逐条核身份，不授予兼容目录写权限。
# 函数用途: 保留原canonical哈希定位，同时只读兼容已存在的local uid命名空间；两处都须核记录，不迁移或建目录。
def _native_store_roots(owner):
    canonical = process_session_store_root(owner.home_dir, owner.home_dir)
    if owner.identity.provider == "local" and owner.identity.owner_kind == "user":
        literal = canonical.parent / owner.identity.owner_id
        if literal != canonical:
            return canonical, literal
    return (canonical,)


# LLM: 原process_sessions摘要目录须能绑定现存owner；遗失owner不能让旧native藏在未枚举namespace里。
# 函数用途: 核共享native完整集合；local用户哈希与uid两种叶均须绑定同一现存owner并继续核记录。
def _native_namespaces(home, owners, namespaces):
    expected = {}
    for owner in owners:
        for root in _native_store_roots(owner):
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


# 原审批桥决定桶/锁名取 sha256 前24位，宿主命令 owner 桶为完整 sha256；只描述既有布局，不是新注册表。
_APPROVAL_DIGEST = re.compile(r"[0-9a-f]{24}")
_COMMAND_OWNER_DIGEST = re.compile(r"[0-9a-f]{64}")
_APPROVAL_LOCK_SUFFIX = ".json.lock"


# LLM: 布局名不符即固定原因拒绝并给准确ref；不按目录名整体豁免，未消费决定/暂存/未知叶都走这里。
# 函数用途: 要求原审批设施的一层名称属于既有布局。
def _require_approval_layout(parts, name, valid):
    if not valid:
        _reject("processing_auxiliary_unsettled", "/".join((*parts, name)))


# LLM: 每层沿原nofollow FD记录目录身份与完整名称，交给首末/停止摘要比较；链接或非目录由原opener拒绝。
# 函数用途: 列出一个原审批设施目录并登记其稳定投影。
def _approval_directory(root, parts, namespaces):
    namespaces["gateway/" + "/".join(parts)] = value = _directory(root, parts)
    return value[1]


# LLM: 宿主命令编号沿原opaque校验，非法名称不是可解释的历史命令目录。
# 函数用途: 判断命令设施层名称是否为原 host_command_request 编号。
def _host_command_request(name):
    try:
        return validate_opaque_id(name, kind="host_command_request") == name
    except ValueError:
        return False


# LLM: 普通库存只读探测；cleanup仅正常stop持生命周期锁时传入，删除仍须原FD非阻塞独占锁及末核。
# 函数用途: 核空闲空锁、报告超24h无数据文件的孤儿；缺fcntl、活锁、危险类型始终失败关闭。
def _settled_approval_lock(root, parts, files, *, cleanup=False, grace_seconds=24 * 60 * 60):
    ref = "/".join(parts)
    try:
        import fcntl
    except ImportError:
        _reject("processing_approval_lock_unsettled", ref)

    parent = _directory_descriptor(root, parts[:-1])
    try:
        descriptor = _openat_file(parent, parts[-1], os.O_RDONLY, 0o600)
        try:
            try:
                fcntl.flock(descriptor, (fcntl.LOCK_EX if cleanup else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError:
                _reject("processing_approval_lock_unsettled", ref)
            raw = os.read(descriptor, 1)
            info = os.fstat(descriptor)
            current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if (raw or info.st_size or (info.st_dev, info.st_ino, info.st_nlink, info.st_mtime_ns) !=
                    (current.st_dev, current.st_ino, 1, current.st_mtime_ns)):
                _reject("processing_approval_lock_unsettled", ref)
            cleanable = (parts[-1].endswith(".lock") and time.time() - info.st_mtime > grace_seconds
                         and _missing_leaf(parent, parts[-1][:-5]))
            if cleanable and cleanup:
                # 锁一直持到unlink之后；重开原父链并核同inode，防旧父目录被替换后删错路径。
                again = _directory_descriptor(root, parts[:-1])
                try:
                    before_parent, after_parent = os.fstat(parent), os.fstat(again)
                    final = os.stat(parts[-1], dir_fd=again, follow_symlinks=False)
                    if ((before_parent.st_dev, before_parent.st_ino) != (after_parent.st_dev, after_parent.st_ino)
                            or (final.st_dev, final.st_ino, final.st_nlink, final.st_size, final.st_mtime_ns) !=
                            (info.st_dev, info.st_ino, 1, 0, info.st_mtime_ns)):
                        _reject("processing_approval_lock_unsettled", ref)
                    if _missing_leaf(again, parts[-1][:-5]):
                        os.unlink(parts[-1], dir_fd=again)
                        return True
                finally:
                    os.close(again)
            files["gateway/" + ref] = hashlib.sha256(raw).hexdigest()
            return cleanable
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


# LLM: 此探针供孤儿锁判定调用，除了叶不存在外的错误必须向上传播。
# 函数用途: 只把精确叶的FileNotFound当不存在，权限、链接和其它I/O异常均不能证明孤儿。
def _missing_leaf(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return True
    return False


# LLM: 只读原canonical DB；精确op联结原run/attempt/task，同一事务读身份与结算，不恢复或初始化。
# 函数用途: 返回审批所指操作的原始责任行，并把新读取事实及库实例纳入原首末摘要。
def _approval_runtime_row(owner, operation_id, files):
    path = runtime_db_path(owner.home_dir)
    try:
        descriptor = open_readonly_file_beneath(owner.home_dir, (path.name,))
    except FileNotFoundError:
        return None
    try:
        origin = os.fstat(descriptor)
        facilities = _sqlite_facilities(owner.home_dir, path.name, descriptor, False)
        repo = RuntimeRepository(path, read_only=True, existing_only=True)
        with repo._runtime_connection() as conn:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT op.*, ar.run_id, ar.status AS run_status, ar.current_attempt_id, "
                "ar.current_attempt_generation, aa.agent_run_id AS attempt_run_id, "
                "aa.attempt_generation AS recorded_generation, aa.status AS attempt_status, aa.ended_at, "
                "ca.agent_run_id AS current_attempt_run_id, ca.status AS current_attempt_status, "
                "ca.attempt_generation AS current_recorded_generation, ca.ended_at AS current_ended_at, "
                "tr.task_run_id, tr.task_id, tr.status AS task_run_status, tr.closed_at, t.owner_id, "
                "(SELECT COUNT(*) FROM resource_locks WHERE attempt_id IN "
                "(SELECT attempt_id FROM agent_attempts WHERE agent_run_id=op.agent_run_id)) AS lock_count "
                "FROM tool_operations op LEFT JOIN agent_runs ar ON ar.agent_run_id=op.agent_run_id "
                "LEFT JOIN agent_attempts aa ON aa.attempt_id=op.attempt_id "
                "LEFT JOIN agent_attempts ca ON ca.attempt_id=ar.current_attempt_id "
                "LEFT JOIN task_runs tr ON tr.task_run_id=ar.task_run_id "
                "LEFT JOIN tasks t ON t.task_id=tr.task_id WHERE op.operation_id=?", (operation_id,)
            ).fetchone()
            value = dict(row) if row is not None else None
            _same_database(owner.home_dir, path.name, origin)
        _same_database(owner.home_dir, path.name, origin)
        if _sqlite_facilities(owner.home_dir, path.name, descriptor, False) != facilities:
            _reject("runtime_read_facilities_changed", path.name)
        # 不以SQLite/WAL字节作业务CAS；只把实际读取的规范行和原库身份加入既有摘要。
        key = "approval-runtime/" + owner.owner_id + "/" + hashlib.sha256(operation_id.encode()).hexdigest()
        files[key] = responsibility_digest({"database": (origin.st_dev, origin.st_ino), "row": value})
        return value
    finally:
        os.close(descriptor)


# LLM: 历史操作核自身attempt；当前链另核终态和身份，不用current执行权fence误拦已结算历史。
# 函数用途: 校验原操作及其精确运行链确已结算，binding逐字段匹配规范身份。
def _approval_operation_settled(value, binding, owner):
    if value is None:
        return False
    terminal = AGENT_RUN_TERMINAL_STATUSES
    if (value["status"] not in {OP_SUCCEEDED, OP_FAILED, OP_CANCELLED}
            or value["run_status"] not in terminal or value["attempt_status"] not in terminal
            or value["current_attempt_status"] not in terminal
            or value["task_run_status"] not in terminal or value["lock_count"] != 0
            or value["owner_id"] != owner.owner_id or not value["task_id"]
            or value["run_id"] != binding["run_id"] or value["operation_type"] != binding["tool_name"]
            or value["attempt_run_id"] != value["agent_run_id"]
            or value["current_attempt_run_id"] != value["agent_run_id"]
            or value["attempt_generation"] != value["recorded_generation"]
            or value["current_attempt_generation"] != value["current_recorded_generation"]):
        return False
    # 换代保留旧操作身份，但不能接受零代、未来代或当前链缺失的证据。
    if (type(value["attempt_generation"]) is not int or type(value["current_attempt_generation"]) is not int
            or not 0 < value["attempt_generation"] <= value["current_attempt_generation"]):
        return False
    for key in ("settled_at", "ended_at", "current_ended_at", "closed_at"):
        stamp = value[key]
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0:
            return False
    # managed_operation原payload拥有args/idempotency身份；坏JSON和缺key不可降级为空对象。
    payload = load_strict_json(value["outcome_json"])
    return (isinstance(payload, dict) and payload.get("schema") == "managed_operation.v1"
            and payload.get("args_hash") == binding["args_hash"]
            and payload.get("idempotency_key") == binding["idempotency_key"]
            and payload.get("task_id") == value["task_id"])


# LLM: owner只来自本次原目录枚举；相同run/op跨owner歧义拒绝，宿主命令还须核原owner哈希桶。
# 函数用途: 以规范结算事实证明无终态字段的真实writer决定已无消费责任，不删除或重新批准。
def _approval_runtime_settled(root, parts, row, owners, files):
    binding = row["binding"]
    matches = []
    for owner in owners:
        value = _approval_runtime_row(owner, binding["operation_id"], files)
        if value is not None:
            matches.append((owner, value))
    if len(matches) != 1:
        return False
    owner, value = matches[0]
    if ".commands" in parts:
        expected = command_approval_path(SimpleNamespace(processing=root / "requests" / "processing"),
                                         owner.identity, row["request_id"])
        if parts[:-2] != (*expected.parent.relative_to(root).parts, ".approvals"):
            return False
    return _approval_operation_settled(value, binding, owner)


# LLM: 显式终态兼容保持；无state/status的真实writer必须另有精确运行结算证明，approved本身不是结算。
# 函数用途: 识别已消费/已完成审批，缺证据或冲突状态始终保持processing阻塞。
def _completed_approval(root, parts, files, *, owners=()):
    row = _json(root, parts, files)
    valid = isinstance(row, dict) and row.get("schema_version") == "tool_approval_decision.v1"
    states = [row[key] for key in ("state", "status") if key in row] if valid else []
    if states:
        valid = all(isinstance(value, str) and value in {"completed", "consumed"} for value in states)
    if valid:
        binding = row.get("binding")
        valid = (isinstance(binding, dict) and all(isinstance(binding.get(key), str) and binding[key]
                 for key in ("tool_name", "run_id", "operation_id", "idempotency_key", "args_hash")))
    if valid:
        try:
            # codec有缺decision默认值，所以必须先显式核字段；不用feedback/label作机器判断。
            valid = (isinstance(row.get("decision"), str) and isinstance(row.get("permission_id"), str)
                     and isinstance(row.get("request_id"), str) and bool(row["request_id"]))
            decision = ToolApprovalDecision.from_mapping(row)
            valid = (valid and hashlib.sha256(row["request_id"].encode()).hexdigest()[:24] == parts[-2]
                     and hashlib.sha256(decision.permission_id.encode()).hexdigest()[:24] + ".json" == parts[-1])
        except (ValueError, TypeError, KeyError):
            valid = False
    if valid and not states:
        valid = _approval_runtime_settled(root, parts, row, owners, files)
    if not valid:
        _reject("processing_auxiliary_unsettled", "/".join(parts))


# LLM: 原 .approvals/<请求桶>/<许可桶>.json.lock 是原桥消费决定后保留的锁；其它名称/类型保持阻塞。
# 函数用途: 核显式终态或有原库结算证明的历史及空锁；未消费、未知仍阻塞，stop可清理老孤儿。
def _approval_decisions(root, parts, namespaces, files, *, cleanup=False, report=None, owners=()):
    for bucket in _approval_directory(root, parts, namespaces):
        _require_approval_layout(parts, bucket, _APPROVAL_DIGEST.fullmatch(bucket))
        for name in _approval_directory(root, (*parts, bucket), namespaces):
            leaf = (*parts, bucket, name)
            if name.endswith(".json") and _APPROVAL_DIGEST.fullmatch(name[:-5]):
                _completed_approval(root, leaf, files, owners=owners)
            else:
                lock = name.endswith(_APPROVAL_LOCK_SUFFIX) and _APPROVAL_DIGEST.fullmatch(name[:-len(_APPROVAL_LOCK_SUFFIX)])
                _require_approval_layout((*parts, bucket), name, lock)
                if _settled_approval_lock(root, leaf, files, cleanup=cleanup) and report is not None:
                    report["cleanable_locks"].append("/".join(leaf))


# LLM: 原 .commands/<owner sha256>/<命令编号>/ 只承载原桥 .approvals；从不写 approval.chunks.jsonl，出现即未知。
# 函数用途: 核宿主命令的原审批布局并传递同一stop清理开关，不接受其它命令产物。
def _host_command_approvals(root, parts, namespaces, files, *, cleanup=False, report=None, owners=()):
    for owner in _approval_directory(root, parts, namespaces):
        _require_approval_layout(parts, owner, _COMMAND_OWNER_DIGEST.fullmatch(owner))
        for request_id in _approval_directory(root, (*parts, owner), namespaces):
            _require_approval_layout((*parts, owner), request_id, _host_command_request(request_id))
            command = (*parts, owner, request_id)
            for name in _approval_directory(root, command, namespaces):
                _require_approval_layout(command, name, name == ".approvals")
                _approval_decisions(root, (*command, name), namespaces, files, cleanup=cleanup, report=report, owners=owners)


# LLM: 只读分类已知旧终态；不改写回执、不赋重放权，供库存按版本说明。
# 函数用途: 只兼容明确v1/无版本的旧completed控制回执，固定ID/结果形状并拒dispatch；损坏v2不走旁路。
def _legacy_completed_control(row, name):
    return (isinstance(row, dict) and (row.get("schema_version") is None
            or row.get("schema_version") == "gateway_control_operation.v1")
            and row.get("state") == "completed" and row.get("dispatch") is None
            and isinstance(row.get("operation_id"), str)
            and re.fullmatch(r"gwctl-msg-[0-9a-f]{32}", row["operation_id"]) is not None
            and row["operation_id"] + ".json" == name and isinstance(row.get("result"), dict)
            and ("error" not in row or isinstance(row["error"], dict)))


# LLM: 原processing/control来源按原codec/locator解释；兼容只作只读分类，不移动到archive或授重放权。
# 函数用途: 保留完整责任摘要并返回版本/可清理锁报告，未知或在途形态仍阻塞；cleanup仅供正常stop。
def _gateway_sources(paths, namespaces, files, *, cleanup=False, owners=()):
    if cleanup and not _normal_stop_cleanup(paths):
        _reject("standalone_stop_cleanup_context_required", "requests/processing")
    report = {"cleanable_locks": [], "legacy_completed": []}
    for parts in (("requests", "processing"), ("control_operations",)):
        namespaces["gateway/" + "/".join(parts)] = _directory(paths.root, parts)
        for name in namespaces["gateway/" + "/".join(parts)][1]:
            kind = _generation_source_entry_kind(paths.root, parts, name)
            if kind == "directory":
                facility = (_approval_decisions if name == ".approvals" else _host_command_approvals)
                facility(paths.root, (*parts, name), namespaces, files, cleanup=cleanup, report=report, owners=owners)
                continue
            if kind == "file":
                if name.endswith(".lock"):
                    leaf = (*parts, name)
                    if _settled_approval_lock(paths.root, leaf, files, cleanup=cleanup):
                        report["cleanable_locks"].append("/".join(leaf))
                    continue
                raw = read_bytes_beneath(paths.root, (*parts, name), max_bytes=4 * 1024 * 1024, require_dir_fd=True)
                files["gateway/" + "/".join((*parts, name))] = hashlib.sha256(raw).hexdigest()
                continue
            if parts[-1] == "processing":
                _reject("processing_request_present", "/".join((*parts, name)))
            ref = "/".join((*parts, name))
            try:
                row = _json(paths.root, (*parts, name), files)
            except (ValueError, TypeError):
                _reject("control_operation_unsettled", ref)
            if not isinstance(row, dict):
                _reject("control_operation_unsettled", ref)
            try:
                receipt = GatewayControlOperationReceipt.from_dict(row)
            except DataCorruptionError:
                if not _legacy_completed_control(row, name):
                    _reject("control_operation_unsettled", ref)
                report["legacy_completed"].append({"ref": ref, "classification": "legacy_completed",
                    "schema_version": row.get("schema_version") or "unversioned"})
                continue
            if receipt.dispatch is not None or receipt.operation_id + ".json" != name or receipt.state != "completed":
                _reject("control_operation_unsettled", ref)
    return report


# LLM: 删除孤儿锁前须有原正常stop的真实持锁上下文；不能从预检参数推定权限。
# 函数用途: 在不改stop调用方的边界内核实际原生命周期锁及本进程正常终止事实，预检标志本身不授删除权。
def _normal_stop_cleanup(paths):
    from .standalone_lifecycle import _HELD
    from .standalone_stop import NORMAL_STOP_KINDS

    for lifecycle in tuple(_HELD):
        if lifecycle.path.parent != paths.root:
            continue
        lifecycle.require_held()
        raw = read_bytes_beneath(paths.root, ("gateway_state.json",), max_bytes=4 * 1024 * 1024,
                                 require_dir_fd=True)
        state = load_strict_json(raw) if raw is not None else None
        return (isinstance(state, dict) and state.get("status") == "stopped" and state.get("exit_code") == 0
                and state.get("termination_kind") in NORMAL_STOP_KINDS
                and isinstance(state.get("process_identity"), dict)
                and state["process_identity"].get("pid") == os.getpid()
                and not state.get("c_run_id") and state.get("worker_count", 0) == 0)
    return False


# LLM: 只用原纯codec/cleanup引用及registry自己的birth观察，绝不list/load/refresh/consume；原宿主在stop采样时尚存，launcher仅后继核。
# 函数用途: 核一owner准确namespace内的native完整退出事实，保留记录；兼容叶也使用同一owner/退出校验。
def _native_sources(home, owner, runtime, namespaces, files, check_launcher, *, root=None):
    root = root if root is not None else process_session_store_root(owner.home_dir, owner.home_dir)
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


# LLM: 首末采样比较整个返回值；只在原正常stop持锁上下文先清老孤儿，随后重采，迁移/预检不删除。
# 函数用途: 汇总Count0原来源及版本说明；不写DB/redo/日志，不建立新业务账或移动历史文件。
def standalone_inventory(home: Path, config, paths, *, check_launcher=True):
    namespaces, files, databases = {}, {}, {}
    owners = _owners(home, namespaces)
    if not check_launcher and _normal_stop_cleanup(paths):
        _gateway_sources(paths, {}, {}, cleanup=True, owners=owners)
    _native_namespaces(home, owners, namespaces)
    report = _gateway_sources(paths, namespaces, files, owners=owners)
    for owner in owners:
        parts = tuple(owner.home_dir.relative_to(home).parts)
        namespaces["/".join(parts)] = _directory(home, parts)
        if namespaces["/".join(parts)][0] is None:
            _reject("owner_home_missing", owner.owner_id)
        runtime = runtime_responsibility_snapshot(owner)
        databases[owner.owner_id] = runtime["digest"]
        for root in _native_store_roots(owner):
            _native_sources(home, owner, runtime, namespaces, files, check_launcher, root=root)
        for root in _conversation_roots(home, owner, config, namespaces):
            _conversation_sources(home, owner, root, runtime, namespaces, files)
    return {"namespaces": namespaces, "files": files, "databases": databases, "diagnostics": report,
            "owners": [asdict(owner.identity) for owner in owners]}
