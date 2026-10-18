# LLM: P2只读核实与结构化诊断分离，unknown仍优先；后续组合根绑定持锁C，不发信号/接P3。
# 模块用途: 输出五字段屏障和保留活成员的独立明细，不修改P1账本。
import copy
import hashlib
import json
import math
import sys
import time

from ..common.nofollow_fs import read_bytes_beneath, write_bytes_atomic_beneath
from .multiprocess_records import MultiprocessRecordError, record_digest, validate_record
from .multiprocess_takeover_ledger import _validate_scope, read_ledger_scope
from .multiprocess_takeover_probe import (
    UnstableProcessSnapshot,
    observe_processes,
    valid_process_table,
)
from .multiprocess_takeover_recheck import _verified_result
from .multiprocess_takeover_tree import evaluate_tree, observed_live_members


# LLM: 受信组合根传预算，坏预算先拒绝；回执印记仅本进程复核使用，JSON证据不授C权限。
# 函数用途: 按调用协议预算核实全部旧执行者，未知/活着隔离，不更改用户配置。
def verify_takeover(ledger, scope: dict, probe=None, budget=None):
    budget = _takeover_budget(budget)
    started = time.monotonic()
    detail = _collect_evidence(ledger, _checked_scope(ledger, scope), probe or observe_processes, budget)
    unresolved = sorted({row["reservation_id"] for row in detail["instances"] if row["status"] != "stopped"})
    if detail["failures"]:
        unresolved.append("ledger_unconfirmed")
    content = json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    name = hashlib.sha256(content).hexdigest() + ".json"
    parts = ("workers", "takeover_evidence", name)
    try:
        _persist_evidence(ledger.root, parts, content)
    except (OSError, ValueError):
        unresolved.append("evidence_unavailable")
    barrier = {"schema_version": "multiprocess_takeover_barrier.v1", "c_run_id": ledger.partition.c_run_id,
               "stopped": not unresolved, "ledger_ref": "/".join(parts), "unresolved_instances": sorted(set(unresolved))}
    proof = _proof(detail, barrier)
    error = None if barrier["stopped"] else {"error_code": "OWNER_PARTITION_QUARANTINED", "reason": "execution_tree_not_stopped"}
    return _verified_result({"barrier": barrier, "proof": proof, "error": error}, started, time.monotonic())


# LLM: 坏范围不复制到JSON证据或参与集合运算；空规范范围仍会被P1/P0校验拒绝，不能降级成空覆盖。
# 函数用途: 保留完整有效范围，非法输入以空范围进入明确unknown路径。
def _checked_scope(ledger, scope) -> dict:
    if type(scope) is not dict:
        return {}
    try:
        _validate_scope(ledger, scope)
        return copy.deepcopy(scope)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return {}


# LLM: 前后canonical重读，采样及最终求值共用显式接受期限；连续两次有效一致才stopped，预算不改证明。
# 函数用途: 有界核实并记录实际预算和全部观察，不改P1或硬抢占内核调用。
def _collect_evidence(ledger, scope: dict, probe, budget=None) -> dict:
    budget = _takeover_budget(budget)
    before = read_ledger_scope(ledger, scope)
    deadline = time.monotonic() + budget["deadline_seconds"]
    tables = _bounded_observations(probe, before["records"], budget, deadline)
    after = read_ledger_scope(ledger, scope)
    failures = sorted(set(before["failures"] + after["failures"] + [table["failure"] for table in tables if table["failure"]]))
    if before != after:
        failures.append("executor_ledger_changed")
    if any((table["platform"], table["host_id"]) != (tables[0]["platform"], tables[0]["host_id"]) for table in tables):
        failures.append("platform_observation_changed")
    records = {row["reservation_id"]: row for row in before["records"] + after["records"]}
    instances = [_verify_record(row, tables) for row in records.values()]
    if time.monotonic() >= deadline:
        failures.append("observation_deadline_exceeded")
    missing = set(scope.get("executor_reservation_ids", ())) - set(records)
    instances.extend(_missing_record(identifier) for identifier in sorted(missing))
    platform = tables[0]["platform"]
    method = {"linux": "proc_pgid_sid", "darwin": "sysctl_pgid_sid"}.get(platform, "unavailable")
    if any(table["failure"] for table in tables):
        method = "unavailable"
    return {"schema_version": "multiprocess_takeover_evidence.v1", "c_run_id": ledger.partition.c_run_id,
            "covered_scope": scope, "platform": platform, "method": method,
            "instances": sorted(instances, key=lambda row: row["reservation_id"]), "failures": failures,
            "observation_budget": budget,
            "observation_attempts": [{"attempted": not table.get("synthetic", False), "failure": table["failure"],
                                      "disappeared": table.get("disappeared", [])} for table in tables],
            "record_digests": {key: record_digest(value) for key, value in sorted(records.items())},
            "residual_risks": ["historical_session_escape_unobservable"] if method != "unavailable" else []}


# LLM: 消失/权限/身份改变失败不抹，任次已见live锁存；缺第二次观测显式哨兵，纯出生未知仍可积累完整事实。
# 函数用途: 有界读取并求值，禁止live被后续unknown及末两张stopped洗掉。
def _bounded_observations(probe, records: list, budget=None, deadline=None) -> list:
    budget = _takeover_budget(budget)
    deadline = time.monotonic() + budget["deadline_seconds"] if deadline is None else deadline
    tables, previous, live_seen = [], None, False
    for _ in range(budget["max_attempts"]):
        if time.monotonic() >= deadline:
            break
        current = _observe(probe)
        if time.monotonic() >= deadline:
            current = {**current, "failure": "observation_deadline_exceeded"}
        tables.append(current)
        evaluations = [evaluate_tree(row, current) for row in records]
        # 同表/同树未知可令整体三态unknown，确定活成员仍足以否定stopped；诊断只作拒绝，不作授权。
        live_seen = live_seen or any(row['status'] == 'live' for row in evaluations) or bool(observed_live_members(evaluations))
        signature = _observation_signature(records, current, evaluations)
        if time.monotonic() >= deadline:
            current["failure"] = "observation_deadline_exceeded"
            signature = None
        if len(tables) >= 2 and (live_seen or _stop_sampling(tables, signature, previous)):
            break
        previous = signature
    if len(tables) < 2:
        reason = "observation_deadline_exceeded" if time.monotonic() >= deadline else "observation_attempt_budget_exhausted"
        tables.append({"platform": sys.platform, "host_id": "", "rows": {}, "failure": reason, "synthetic": True})
    return tables


# LLM: 参数属受信组合根的单次协议，不是用户配置；严格有限正数、拒bool/坏schema，纯验证无I/O。
# 函数用途: 冻结本次观察预算，None保持原五次/一秒默认，非法输入抛ValueError。
def _takeover_budget(budget) -> dict:
    if budget is None:
        return {"schema_version": "multiprocess_takeover_budget.v1", "max_attempts": 5, "deadline_seconds": 1.0}
    if (type(budget) is not dict or set(budget) != {"schema_version", "max_attempts", "deadline_seconds"}
            or budget["schema_version"] != "multiprocess_takeover_budget.v1"):
        raise ValueError("takeover_budget_invalid")
    attempts, seconds = budget["max_attempts"], budget["deadline_seconds"]
    if type(attempts) is not int or attempts < 1 or type(seconds) not in {int, float}:
        raise ValueError("takeover_budget_invalid")
    try:
        seconds = float(seconds)
    except OverflowError as exc:
        raise ValueError("takeover_budget_invalid") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("takeover_budget_invalid")
    return {**budget, "deadline_seconds": seconds}


# LLM: 相关消失/权限/身份改变等失败不抹去；任次live提前隔离，完整未知可继续但不算连续有效。
# 函数用途: 判断已有足够拒绝或一致证据，避免无意义重读。
def _stop_sampling(tables: list, signature, previous) -> bool:
    return (any(table["failure"] for table in tables) or signature is not None and signature == previous
            or signature is not None and "live" in signature)


# LLM: 比较整个P1结论向量；unknown不作一致签发。采样器可复用同张表已求值结果，以独立锁存live且不重复求值。
# 函数用途: 把全部有效三态转成连续签名，保留逃逸失败；不读写文件。
def _observation_signature(records: list, table: dict, evaluations=None):
    if evaluations is None:
        evaluations = [evaluate_tree(row, table) for row in records]
    if any(row["reason"] == "descendant_scope_escape" for row in evaluations):
        table["failure"] = "descendant_scope_escape"
    signature = tuple(row["status"] for row in evaluations)
    return None if table["failure"] or "unknown" in signature else signature


# LLM: 受信注入与原生探针的消失标记同样保留为enumeration_unstable；任何异常空表都非成功，不解析文案。
# 函数用途: 包装一次只读观测，保留不一致原因；不吞控制者中断，不发信号。
def _observe(probe) -> dict:
    try:
        table = probe()
        if valid_process_table(table):
            return copy.deepcopy(table)
    except UnstableProcessSnapshot:
        return {"platform": sys.platform, "host_id": "", "rows": {}, "failure": "enumeration_unstable"}
    except Exception:
        pass
    return {"platform": sys.platform, "host_id": "", "rows": {}, "failure": "probe_fact_unavailable"}


# LLM: unknown仍优先；最后两次有效一致才stopped，独立汇集全部活成员诊断，不借诊断改证明。
# 函数用途: 归并结论及完整性标记，避免选unknown时丢掉其它观察中的活成员。
def _verify_record(record: dict, tables: list) -> dict:
    observations = [evaluate_tree(record, table) for table in tables]
    recent = observations[-2:]
    unknown = next((row for row in recent if row["status"] == "unknown"), None)
    live = next((row for row in recent if row["status"] == "live"), None)
    selected = unknown or live or recent[-1]
    return {**selected, "observations": observations,
            "enumeration_complete": all(row.get("enumeration_complete", False) for row in observations),
            "observed_live_members": observed_live_members(observations)}


# LLM: 有明确引用但读不到完整record仍是一个未核实执行者，不删除引用或伪造死亡回执。
# 函数用途: 为账本缺口保留逐条unknown明细。
def _missing_record(identifier: str) -> dict:
    return {"reservation_id": identifier, "status": "unknown", "reason": "executor_record_unavailable",
            "evidence_kinds": ["p1_canonical_read_failure"], "instances": [], "observations": []}


# LLM: 只写内容寻址核实证据，P1记录不动；不同旧事实不可覆盖，写后按同一路径字节读回再交付。
# 函数用途: 将独立明细持久化到ledger_ref，任何失败都不产生stopped许可。
def _persist_evidence(root, parts: tuple, content: bytes) -> None:
    existing = read_bytes_beneath(root, parts, max_bytes=len(content), require_dir_fd=True)
    if existing is not None and existing != content:
        raise ValueError("takeover_evidence_conflict")
    if existing is None:
        write_bytes_atomic_beneath(root, parts, content)
    if read_bytes_beneath(root, parts, max_bytes=len(content), require_dir_fd=True) != content:
        raise ValueError("takeover_evidence_readback_failed")


# LLM: P0范围证明只是可运输核实结果，绝非当前持锁C授权；缺证据/写失败必为unknown，不调用P3。
# 函数用途: 输出兼容P0/P3的v2证明或在范围非法时返回None。
def _proof(detail: dict, barrier: dict):
    status = "stopped" if barrier["stopped"] else "live"
    if (detail["failures"] or "evidence_unavailable" in barrier["unresolved_instances"]
            or any(row["status"] == "unknown" for row in detail["instances"])):
        status = "unknown"
    payload = {"schema_version": "multiprocess_takeover_proof.v2", "c_run_id": barrier["c_run_id"],
               "status": status, "platform": detail["platform"], "method": detail["method"],
               "covered_scope": detail["covered_scope"], "unresolved_instances": barrier["unresolved_instances"],
               "residual_risks": detail["residual_risks"]}
    try:
        return validate_record(payload)
    except MultiprocessRecordError:
        return None
