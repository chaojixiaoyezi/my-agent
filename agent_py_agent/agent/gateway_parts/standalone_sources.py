# LLM: Count0冷启动只拒绝已见MP或未知原事实，不消费closure、不补身份、不构造scope；同锁内两次原源读取不能替代迁移证明。
# 模块用途: 防止把公开YAML数量改为0绕过原多进程接班边界。
from __future__ import annotations

import re

from ..common.nofollow_fs import list_names_beneath, read_bytes_beneath
from ..common.strict_json import load_strict_json
from .multiprocess_generation_metadata import generation_metadata_parts
from .multiprocess_records import _identifier, deserialize_record
from .standalone_lifecycle import StandaloneLifecycleUnavailable


# LLM: 分类仅输出固定码，不输出原载荷或读错正文；合法MP和未知来源均不允许单进程恢复。
# 类用途: 让公开入口准确区分不支持逆迁移与原事实无法确认。
class StandaloneSourceUnproven(StandaloneLifecycleUnavailable):
    error_code = "GATEWAY_STANDALONE_SOURCE_UNPROVEN"


# LLM: 即便MP记录声称closed也只拒绝逆迁移，不把该JSON提升为Count0准入凭据。
# 类用途: 表示现有home有规范MP事实，需要明确的受支持迁移流程。
class ReverseMigrationUnsupported(StandaloneSourceUnproven):
    error_code = "GATEWAY_REVERSE_MIGRATION_UNSUPPORTED"


# LLM: 仅在原生命周期锁内、Agent/凭据/恢复前调用；坏格式/危险路径不转换为空来源。
# 函数用途: 保留新home和原Count0状态，拒绝MP、混代和不可确认的旧来源。
def require_standalone_sources(paths, lifecycle) -> None:
    lifecycle.require_held()
    try:
        before = _source_snapshot(paths.root)
        if _source_snapshot(paths.root) != before:
            raise StandaloneSourceUnproven("原启动来源在读取期间变化。")
    except StandaloneSourceUnproven:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise StandaloneSourceUnproven("原启动来源不可确认。") from exc
    lifecycle.require_held()


# LLM: 复用原nofollow列名；portable分支缺目录可抛FileNotFound，仅该准确缺失可作空，危险路径仍上抛。
# 函数用途: 读取规范目录的完整直接名称，不创建目录或忽略未知叶。
def _names(root, parts):
    try:
        return list_names_beneath(root, parts)
    except FileNotFoundError:
        return []


# LLM: 既有Count0没有MP代次字段；原C marker无独立schema，仍核同一identifier/PID/count字段后只拒绝。
# 函数用途: 校验原state类型与拓扑字段，保留没有MP身份的真实旧单进程投影。
def _state(raw):
    if raw is None:
        return
    value = load_strict_json(raw)
    if type(value) is not dict:
        raise ValueError("state_not_object")
    if "c_run_id" in value:
        _identifier(value["c_run_id"], "c_run_id")
        if type(value.get("pid")) is not int or value["pid"] <= 0:
            raise ValueError("state_pid_invalid")
        if type(value.get("worker_count")) is not int or value["worker_count"] <= 0:
            raise ValueError("state_count_invalid")
        raise ReverseMigrationUnsupported("现有多进程代次不能降为单进程。")
    count = value.get("worker_count", 0)
    if type(count) is not int or count != 0:
        raise ValueError("state_topology_unconfirmed")
    if (type(value.get("pid")) is not int or value["pid"] <= 0
            or value.get("status") not in {"starting", "running", "stopped", "failed", "interrupted", "http_server_error", "killed"}):
        raise ValueError("standalone_state_unconfirmed")


# LLM: 规范记录完整走唯一MP codec及路径绑定，合法记录也只产生拒绝；锁文件仅读原nofollow形状，不推断producer结束。
# 函数用途: 检查worker目录真实叶，允许准确空设施，未知名称或记录拒绝。
def _worker_sources(root):
    names = _names(root, ("workers",))
    observed = []
    for name in names:
        parts = ("workers", name)
        if name in {"executors", "generations", "takeover_evidence"}:
            _require_empty_worker_directory(root, parts, name)
            observed.append((name, ()))
            continue
        raw = read_bytes_beneath(root, parts, max_bytes=1024 * 1024)
        if name == "registry.json.lock":
            observed.append((name, raw))
            continue
        row = deserialize_record(raw)
        if name == "registry.json" and row["schema_version"] == "multiprocess_worker_registry.v1":
            raise ReverseMigrationUnsupported("现有worker登记不能降为单进程。")
        match = re.fullmatch(r"w(0|[1-9][0-9]*)\.json", name)
        if (match and row["schema_version"] == "multiprocess_worker_heartbeat.v1"
                and row["worker"] == int(match[1])):
            raise ReverseMigrationUnsupported("现有worker角色不能降为单进程。")
        raise ValueError("worker_source_unknown")
    return names, observed


# LLM: 只拆分原目录分支，不放宽来源门；完整codec、文件名绑定与原异常保持，修改须复测空目录/旧代/未知叶。
# 函数用途: 核验可为空的worker设施目录，遇到历史或未知责任时按原协议拒绝，不写文件。
def _require_empty_worker_directory(root, parts, name):
    children = _names(root, parts)
    if not children:
        return
    if name != "generations":
        raise StandaloneSourceUnproven("存在未核实的多进程责任来源。")
    for child in children:
        row = deserialize_record(read_bytes_beneath(root, (*parts, child), max_bytes=4 * 1024 * 1024))
        if (row["schema_version"] not in {"multiprocess_c_generation.v1", "multiprocess_c_generation.v2"}
                or generation_metadata_parts(row["c_run_id"])[-1] != child):
            raise ValueError("generation_binding_invalid")
    raise ReverseMigrationUnsupported("现有多进程历史不能降为单进程。")


# LLM: 这里只读准确state/worker/P3源，非整home审计；不删marker或读取业务prompt，返回原字节用于首末比较。
# 函数用途: 捕获单进程启动前的有限原来源快照。
def _source_snapshot(root):
    raw = read_bytes_beneath(root, ("gateway_state.json",), max_bytes=1024 * 1024)
    _state(raw)
    workers = _worker_sources(root)
    admission = _names(root, ("admission", "reservations"))
    for name in admission:
        row = deserialize_record(read_bytes_beneath(root, ("admission", "reservations", name), max_bytes=16384))
        if (row["schema_version"] not in {"multiprocess_admission_reservation.v1", "multiprocess_admission_reservation.v2"}
                or row["request_id"] + ".json" != name):
            raise ValueError("admission_binding_invalid")
        raise ReverseMigrationUnsupported("现有多进程准入责任不能降为单进程。")
    return raw, workers, admission
