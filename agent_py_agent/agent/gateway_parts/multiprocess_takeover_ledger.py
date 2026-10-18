# LLM: P2只读包装P1 canonical读接口，不建立登记源、不创建锁或删除未知记录；先冻结测试签名。
# 模块用途: 核对完整目录、旧代owner/角色/分区及明确执行者引用。
import hashlib
import os
import re

from ..common.nofollow_fs import list_names_beneath, open_readonly_file_beneath, read_bytes_beneath
from ..user_space.owner_resolver import OwnerIdentity
from .multiprocess_executor_ledger import ExecutorLedger, fcntl
from .multiprocess_records import MultiprocessRecordError, deserialize_record, validate_record
from .multiprocess_route import WorkerPartition, owner_digest


# LLM: 只读规范源，任何缺口不能当空账；不创建路径。
# 函数用途: 返回范围内原记录及结构化核实缺口。
def read_ledger_scope(ledger, scope: dict):
    try:
        _validate_scope(ledger, scope)
        records = _read_namespaces(ledger, scope)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return {"records": [], "failures": ["executor_ledger_unreadable"]}
    selected = [row for row in records if row["c_run_id"] == scope["c_run_id"]
                and row["owner_sha"] in scope["owner_shas"]]
    actual = {row["reservation_id"] for row in selected}
    failures = [] if actual == set(scope["executor_reservation_ids"]) else ["executor_coverage_mismatch"]
    return {"records": sorted(selected, key=lambda row: row["reservation_id"]), "failures": failures}


# LLM: 复用P0 v2范围校验；C/W按规范owner区分，不因P1的物理hash桶而把C冒充W。
# 函数用途: 核对旧代、范围及账本根，不进行I/O或取得新的业务权限。
def _validate_scope(ledger, scope: dict) -> None:
    validate_record({"schema_version": "multiprocess_takeover_proof.v2", "c_run_id": scope["c_run_id"],
                     "status": "unknown", "platform": "win32", "method": "unavailable", "covered_scope": scope,
                     "unresolved_instances": [], "residual_risks": []})
    if (not ledger.root.is_absolute() or ledger.partition.c_run_id != scope["c_run_id"]
            or ledger.partition.count != scope["worker_count"] or fcntl is None):
        raise MultiprocessRecordError("ledger_scope_conflict")
    local_sha = owner_digest(OwnerIdentity.local_main())
    if scope["role"] == "C":
        valid = scope["owner_shas"] == [local_sha]
    else:
        valid = all(sha != local_sha and int(sha, 16) % scope["worker_count"] == scope["worker_index"]
                    for sha in scope["owner_shas"])
    if not valid:
        raise MultiprocessRecordError("owner_role_partition_conflict")


# LLM: 枚举同一P1目录，不从调用方ID清单直接逐项读而漏掉未列入的原记录；陌生目录保守拒绝。
# 函数用途: 只读检查全部现存规范桶；目录不存在不能作为空账证明。
def _read_namespaces(ledger, scope: dict) -> list:
    names = list_names_beneath(ledger.root, ("workers", "executors"))
    if not names or any(re.fullmatch(r"w(0|[1-9][0-9]*)", name) is None for name in names):
        raise MultiprocessRecordError("executor_namespace_unconfirmed")
    rows = []
    for name in names:
        index = int(name[1:])
        partition = WorkerPartition(index, scope["worker_count"], scope["c_run_id"])
        rows.extend(_read_partition(ExecutorLedger(ledger.root, partition), scope))
    ids = [row["reservation_id"] for row in rows]
    if len(ids) != len(set(ids)) or list_names_beneath(ledger.root, ("workers", "executors")) != names:
        raise MultiprocessRecordError("executor_namespace_changed")
    return rows


# LLM: 只打开现有P1 ledger.lock为只读并尝试共享锁，不创建锁、不修改记录；锁忙也不等待或猜空。
# 函数用途: 在同一个短只读锁区间发现文件并经原P1接口取回完整记录。
def _read_partition(ledger, scope: dict) -> list:
    parts = ("workers", "executors", f"w{ledger.partition.index}")
    descriptor = open_readonly_file_beneath(ledger.root, (*parts, "ledger.lock"))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        names = list_names_beneath(ledger.root, parts)
        records = [_read_record(ledger, name, scope) for name in names if name != "ledger.lock"]
        if list_names_beneath(ledger.root, parts) != names:
            raise MultiprocessRecordError("executor_partition_changed")
        return records
    finally:
        os.close(descriptor)


# LLM: 摘要文件名不可逆，先从有界canonical字节发现ID，再必须用P1 read核验同一完整记录；不缓存成新权威。
# 函数用途: 拒绝链接、坏名、残缺、错桶及读回漂移，原P1身份/FSM校验不放宽。
def _read_record(ledger, name: str, scope: dict) -> dict:
    if re.fullmatch(r"[0-9a-f]{64}\.json", name) is None:
        raise MultiprocessRecordError("executor_filename_invalid")
    parts = ("workers", "executors", f"w{ledger.partition.index}", name)
    raw = read_bytes_beneath(ledger.root, parts, max_bytes=16384, require_dir_fd=True)
    candidate = deserialize_record(raw)
    reservation_id = candidate.get("reservation_id")
    if type(reservation_id) is not str or hashlib.sha256(reservation_id.encode()).hexdigest() + ".json" != name:
        raise MultiprocessRecordError("executor_filename_binding_conflict")
    record = ledger.read(reservation_id)
    if record != candidate:
        raise MultiprocessRecordError("executor_readback_changed")
    if record["c_run_id"] == scope["c_run_id"] and int(record["owner_sha"], 16) % scope["worker_count"] != ledger.partition.index:
        raise MultiprocessRecordError("executor_partition_binding_conflict")
    return record
