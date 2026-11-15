# LLM: 仅清理已验证 v2/v3/v4 session，任务或激活由调用方选定；launcher 仅是存活证据，不再按范围重扫。
# 模块用途: 原 Store 提交停止意图后，按冻结 host/child 出生身份清理并保存结果，未确认保持未知。
from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import asdict, dataclass

from .process_registry import (
    ProcessSessionAuthorityError,
    ProcessTerminationReceipt,
    _process_instance_terminated,
    _same_process,
    capture_process_birth_token,
    serialize_cleanup_receipt,
    terminate_process_tree,
)
from .process_session_commit import ProcessSessionCommitPendingError
from .process_session_records import (
    MANAGED_PROCESS_SESSION_SCHEMAS,
    PROCESS_TERMINAL_STATUSES,
    merge_process_record,
)
from .process_session_store import ProcessSessionStore


# LLM: confirmed 只说明冻结的已知实例清理；业务文件或外部副作用不因此回滚，不可据此自动重放命令。
#   authority_missing=True 表示第一个事务里权威记录已不存在：一个信号都没发、什么都没写，调用方按“权威读不出”处理，
#   不能说成“停止过、没确认”。
# 类用途: 向启动失败和显式停止入口返回真实清理结果及最新持久记录。
@dataclass(frozen=True)
class ProcessSessionCleanup:
    record: dict[str, object]
    confirmed: bool
    terminations: tuple[ProcessTerminationReceipt, ...] = ()
    authority_missing: bool = False


# LLM: 已提交意图与已经发过信号均不可降为未发生；回执只包含原 session，不重新选择资源。
# 类用途: 向控制/工具入口报告清理过程中的持久化故障及已知终止结果。
class ProcessSessionCleanupError(RuntimeError):
    # LLM: 异常不包含原始记录正文；pending commit 的固定 ID 和实际终止回执继续向上传递。
    # 函数用途: 保存故障阶段、原记录和已确认副作用。
    def __init__(
        self,
        cause: Exception,
        record: dict[str, object],
        receipts: tuple[ProcessTerminationReceipt, ...],
        committed: bool,
    ) -> None:
        super().__init__("managed process cleanup outcome unresolved")
        self.record = record
        pending = isinstance(cause, ProcessSessionCommitPendingError)
        pending_record = (
            next(
                (
                    row
                    for row in cause.receipt.records
                    if row["session_id"] == record["session_id"] and row["stop_requested"]
                ),
                None,
            )
            if pending
            else None
        )
        self.report = {
            "error_type": type(cause).__name__,
            "committed": committed or pending_record is not None,
            "recovery_required": pending,
            "session_ids": [record["session_id"]],
            "termination_receipts": [asdict(receipt) for receipt in receipts],
        }
        if isinstance(cause, ProcessSessionAuthorityError):
            self.report["authority_error"] = dict(cause.report)
        if pending:
            self.report["recovery_transaction_id"] = cause.receipt.transaction_id
            if pending_record is not None:
                self.report["transaction_id"] = cause.receipt.transaction_id
                self.record = pending_record


# LLM: 固定 v2/v3/v4 同一句柄，先落停止意图再发信号；完整 session 清理写 termination.cleanup，原命令终态及 child 回执不重写。
# 记录已是 unknown（上一次停止未按期确认）时，重试若按出生身份证明两级实例都不存在，即确认清理并转 killed；首次停止仍要求终态或本次回执。
# host 读到停止意图会自己清理 child、写回已确认回执并退出，与本函数锁外采集并发：见 _host_exit_settled_receipts。
# 函数用途: 停止准确资源并持久保存完整退出确认，自然结束的命令也能留下清理证据；重试能结清实例已消失的旧未知记录。
@dataclass(frozen=True)
class _PriorCleanupObligations:
    has_cleanup: bool
    unresolved_instances: tuple[dict[str, object], ...]
    bare_unresolved_pids: tuple[int, ...]
    tree_incomplete: bool
    retained_receipts: tuple[dict[str, object], ...]


def _normalize_unresolved_instance(item: object) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None
    pid, birth = item.get("pid"), item.get("birth_token")
    if type(pid) is not int or pid <= 0 or not isinstance(birth, str) or not birth.strip():
        return None
    return {"pid": pid, "birth_token": birth}


# 函数用途: 从单个清理回执中提取 (pid, birth_token) 复合键集合，防止操作系统 PID 复用导致旧实例误消账。
def _receipt_unresolved_identity_keys(receipt_obj: dict[str, object]) -> set[tuple[int, str | None]]:
    identities: set[tuple[int, str | None]] = set()
    inst_pids: set[int] = set()
    for raw_inst in receipt_obj.get("unresolved_instances") or ():
        normalized = _normalize_unresolved_instance(raw_inst)
        if normalized is None:
            continue
        pid = int(normalized["pid"])
        identities.add((pid, str(normalized["birth_token"])))
        inst_pids.add(pid)
    for raw_pid in receipt_obj.get("unresolved_pids") or ():
        if type(raw_pid) is int and raw_pid > 0 and raw_pid not in inst_pids:
            identities.add((raw_pid, None))
    return identities


def _extract_prior_obligations(
    record: dict[str, object],
    *,
    include_top_level: bool = True,
    include_top_level_bare: bool = False,
) -> _PriorCleanupObligations:
    termination = record.get("termination")
    if not isinstance(termination, dict):
        return _PriorCleanupObligations(False, (), (), False, ())

    instances_by_key: dict[tuple[int, str], dict[str, object]] = {}
    bare_pids: list[int] = []
    bare_pid_set: set[int] = set()
    retained_receipts: list[dict[str, object]] = []
    tree_incomplete = False

    def _collect_instance(entry: object) -> dict[str, object] | None:
        normalized = _normalize_unresolved_instance(entry)
        if normalized is not None:
            key = (int(normalized["pid"]), str(normalized["birth_token"]))
            instances_by_key[key] = normalized
        return normalized

    def _collect_receipt(receipt_obj: dict[str, object], *, allow_bare: bool = True) -> None:
        nonlocal tree_incomplete
        if receipt_obj.get("tree_observation_complete") is False and receipt_obj.get("confirmed") is not True:
            tree_incomplete = True
        inst_pids: set[int] = set()
        for entry in receipt_obj.get("unresolved_instances") or ():
            normalized = _collect_instance(entry)
            if normalized is not None:
                inst_pids.add(int(normalized["pid"]))
        bare_in_receipt = False
        if allow_bare:
            for raw_pid in receipt_obj.get("unresolved_pids") or ():
                if type(raw_pid) is int and raw_pid > 0 and raw_pid not in inst_pids:
                    bare_in_receipt = True
                    if raw_pid not in bare_pid_set:
                        bare_pid_set.add(raw_pid)
                        bare_pids.append(raw_pid)
        if (
            receipt_obj.get("confirmed") is False
            and (bare_in_receipt or receipt_obj.get("tree_observation_complete") is False)
            and "method" in receipt_obj
        ):
            retained_receipts.append(dict(receipt_obj))

    cleanup = termination.get("cleanup")
    if isinstance(cleanup, dict):
        if cleanup.get("confirmed") is True:
            return _PriorCleanupObligations(True, (), (), False, ())
        if cleanup.get("tree_observation_complete") is False:
            tree_incomplete = True
        for entry in cleanup.get("unresolved_instances") or ():
            _collect_instance(entry)
        for inst_receipt in cleanup.get("instances") or ():
            if isinstance(inst_receipt, dict):
                _collect_receipt(inst_receipt, allow_bare=True)
        return _PriorCleanupObligations(
            True,
            tuple(instances_by_key.values()),
            tuple(bare_pids),
            tree_incomplete,
            tuple(retained_receipts),
        )

    if include_top_level and termination.get("confirmed") is False:
        _collect_receipt(termination, allow_bare=include_top_level_bare)
    return _PriorCleanupObligations(
        False,
        tuple(instances_by_key.values()),
        tuple(bare_pids),
        tree_incomplete,
        tuple(retained_receipts),
    )


def _count_live_leaders(record: dict[str, object]) -> int:
    live = 0
    for pid_key, birth_key in (("pid", "pid_birth_token"), ("child_pid", "child_pid_birth_token")):
        pid, birth = record[pid_key], record[birth_key]
        if not pid:
            continue
        if _process_instance_terminated(pid, birth) or not _same_process(pid, birth):
            continue
        live += 1
    return live


def stop_process_session(
    store: ProcessSessionStore,
    selected: dict[str, object],
    *,
    host_process: subprocess.Popen | None = None,
) -> ProcessSessionCleanup:
    if selected.get("schema") not in MANAGED_PROCESS_SESSION_SCHEMAS:
        raise ValueError("managed session cleanup requires versioned authority")
    frozen, receipts, committed = selected, (), False
    try:
        with store.transaction() as transaction:
            current = transaction.load(str(selected["session_id"]))
            if current is None:
                return ProcessSessionCleanup(selected, False, authority_missing=True)
            merge_process_record(selected, current)
            frozen = transaction.write({**current, "stop_requested": True})
            committed = True
        _await_original_host_cleanup(store, frozen)
        leaders_live_at_start = _count_live_leaders(frozen)
        _frozen_receipt_targets.entry = None
        try:
            receipts = _terminate_frozen_instances(frozen, host_process)
            target_entry = getattr(_frozen_receipt_targets, "entry", None)
        finally:
            _frozen_receipt_targets.entry = None
        receipt_targets = (
            target_entry[1]
            if isinstance(target_entry, tuple) and len(target_entry) == 2 and target_entry[0] is receipts
            else None
        )
        with store.transaction() as transaction:
            current = transaction.load(str(frozen["session_id"]))
            if current is None:
                return ProcessSessionCleanup(frozen, False, receipts)
            merge_process_record(frozen, current)
            known_terminal = current["status"] in PROCESS_TERMINAL_STATUSES
            child_known = bool(current["child_pid"]) or not current["child_launch_started"]
            instances_gone = all(
                not current[pid_key]
                or _process_instance_terminated(current[pid_key], current[birth_key])
                for pid_key, birth_key in (
                    ("pid", "pid_birth_token"),
                    ("child_pid", "child_pid_birth_token"),
                )
            )
            no_host_confirmed = (
                not current["pid"]
                and current["status"] == "not_started"
                and (current.get("termination") or {}).get("confirmed") is True
            )
            prior = _extract_prior_obligations(
                current,
                include_top_level=True,
                include_top_level_bare=(current["status"] == "unknown"),
            )
            prior_blocked = (
                prior.tree_incomplete
                or bool(prior.bare_unresolved_pids)
                or any(
                    not _process_instance_terminated(int(inst["pid"]), str(inst["birth_token"]))
                    for inst in prior.unresolved_instances
                )
            )
            settled = _host_exit_settled_receipts(
                receipts,
                current,
                instances_gone=instances_gone,
                prior_blocked=prior_blocked,
                receipt_targets=receipt_targets,
            )

            remaining_unresolved: dict[tuple[int, str], dict[str, object]] = {}
            receipt_bare_pids = False
            current_run_incomplete = False
            complete_leader_receipts = 0
            explicit_complete_seen = False

            for receipt in settled:
                if receipt.tree_observation_complete is False:
                    current_run_incomplete = True
                elif receipt.tree_observation_complete is True:
                    explicit_complete_seen = True
                    if receipt.method != "unresolved_instance_gone" and receipt.confirmed:
                        complete_leader_receipts += 1
                if receipt.confirmed:
                    continue
                receipt_inst_pids: set[int] = set()
                for raw_inst in receipt.unresolved_instances:
                    normalized = _normalize_unresolved_instance(raw_inst)
                    if normalized is None:
                        continue
                    key = (int(normalized["pid"]), str(normalized["birth_token"]))
                    remaining_unresolved[key] = normalized
                    receipt_inst_pids.add(int(normalized["pid"]))
                for bare_pid in receipt.unresolved_pids:
                    if bare_pid not in receipt_inst_pids:
                        receipt_bare_pids = True

            for inst in prior.unresolved_instances:
                key = (int(inst["pid"]), str(inst["birth_token"]))
                if key in remaining_unresolved:
                    continue
                if not _process_instance_terminated(int(inst["pid"]), str(inst["birth_token"])):
                    remaining_unresolved[key] = dict(inst)

            expected_leaders = sum(1 for key in ("pid", "child_pid") if current.get(key))
            if prior.tree_incomplete:
                # 若任一主/子组长在完整快照采集前已退出，粘性不完整标志绝不可被空重试抹除。
                sticky_incomplete = (
                    current_run_incomplete
                    or expected_leaders == 0
                    or leaders_live_at_start < expected_leaders
                    or complete_leader_receipts < expected_leaders
                    or bool(remaining_unresolved)
                )
            else:
                sticky_incomplete = current_run_incomplete

            prior_cleanup_obj = (current.get("termination") or {}).get("cleanup")
            prior_has_tree_flag = (
                isinstance(prior_cleanup_obj, dict)
                and "tree_observation_complete" in prior_cleanup_obj
            )
            if no_host_confirmed:
                tree_observation_complete: bool | None = None
                remaining_unresolved.clear()
            elif sticky_incomplete:
                tree_observation_complete = False
            elif explicit_complete_seen or prior_has_tree_flag or remaining_unresolved:
                tree_observation_complete = True
            else:
                tree_observation_complete = None

            # 上一次停止已把记录写成 unknown（当时实例未按期退出）；重试时仅在先前不完整状态与所有未决实例
            # 及纯 PID 遗留项都已按个体出生身份真正核销后，才能结清旧未知记录。
            retried_after_unknown = current["status"] == "unknown" and isinstance(
                prior_cleanup_obj, dict,
            )
            unsettled_obligations = (
                bool(remaining_unresolved)
                or bool(prior.bare_unresolved_pids)
                or receipt_bare_pids
                or (tree_observation_complete is False)
            )
            confirmed = (
                no_host_confirmed
                or bool(current["pid"])
                and child_known
                and instances_gone
                and not unsettled_obligations
                and (known_terminal or bool(settled) or retried_after_unknown)
                and all(receipt.confirmed for receipt in settled)
            )
            if not known_terminal:
                status = (
                    ("killed" if current["child_pid"] else "not_started")
                    if confirmed
                    else "unknown"
                )
                current = {
                    **current,
                    "status": status,
                    "finished_at": time.time() if confirmed else None,
                    "exit_code": None,
                }
            remaining_list = list(remaining_unresolved.values())
            cleanup_instances = [serialize_cleanup_receipt(r) for r in settled]
            if not confirmed and prior.retained_receipts:
                if not cleanup_instances:
                    cleanup_instances = [dict(item) for item in prior.retained_receipts]
                else:
                    covered_identities = {
                        ident
                        for item in cleanup_instances
                        for ident in _receipt_unresolved_identity_keys(item)
                    }
                    for item in prior.retained_receipts:
                        item_identities = _receipt_unresolved_identity_keys(item)
                        if item_identities - covered_identities:
                            cleanup_instances.append(dict(item))
            cleanup_data: dict[str, object] = {
                "confirmed": confirmed,
                "instances": cleanup_instances,
            }
            if tree_observation_complete is not None:
                cleanup_data["tree_observation_complete"] = tree_observation_complete
            if remaining_list:
                cleanup_data["unresolved_instances"] = remaining_list
            elif tree_observation_complete is not None:
                cleanup_data["unresolved_instances"] = []
            current = transaction.write({**current, "termination": {
                **(current.get("termination") or {}),
                "cleanup": cleanup_data,
            }})
            return ProcessSessionCleanup(current, confirmed, settled)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ProcessSessionCleanupError(exc, frozen, receipts, committed) from exc


# LLM: stop意图已耐久提交，host仍持有原child Popen与出生身份，应先让它收树、wait并发表原凭证。
#   只等当前精确session，锁外最多覆盖常态1秒观察+0.5秒TERM宽限；无凭证/身份变化仍走原清理，不推断成功。
# 函数用途: 给托管host有界协作收尾窗口，避免先杀host而丢失其直接子树退出证明。
def _await_original_host_cleanup(store: ProcessSessionStore, frozen: dict[str, object]) -> None:
    if (frozen["status"] in PROCESS_TERMINAL_STATUSES or not frozen["pid"]
            or not frozen["child_launch_started"] or not frozen["child_pid"]):
        return
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        # 这里只决定是否继续等；实例消失或身份漂移绝不在此签发清理确认。
        if (capture_process_birth_token(frozen["pid"]) != frozen["pid_birth_token"]
                or _process_instance_terminated(frozen["pid"], frozen["pid_birth_token"])):
            return
        report = store.load(str(frozen["session_id"]))
        if report.load_error or not report.record:
            raise ProcessSessionAuthorityError(report.load_error or {"error_type": "authority_missing"})
        merge_process_record(frozen, report.record)
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


# host 在本次停止期间自行退出：由终止原语对 host 目标的 identity_changed / identity_unavailable 回执
# 加独立终态与实例消失证明推出（见下方函数），不是原语自己的观测。
_HOST_EXITED_DURING_STOP = "host_exited_during_stop"
_frozen_receipt_targets = threading.local()


def _host_race_receipt_settleable(
    receipt: ProcessTerminationReceipt,
    target: str | None,
    record: dict[str, object],
) -> bool:
    if receipt.confirmed or receipt.method not in ("identity_changed", "identity_unavailable"):
        return False
    host_pid = record.get("pid")
    child_pid = record.get("child_pid")
    if type(host_pid) is not int or host_pid <= 0:
        return False
    if target is not None and target != "host":
        return False
    inst_pids: set[int] = set()
    for raw_inst in receipt.unresolved_instances:
        normalized = _normalize_unresolved_instance(raw_inst)
        if normalized is None:
            return False
        inst_pid = int(normalized["pid"])
        if (
            target is None
            and type(child_pid) is int
            and child_pid > 0
            and inst_pid == child_pid
            and inst_pid != host_pid
        ):
            return False
        if not _process_instance_terminated(inst_pid, str(normalized["birth_token"])):
            return False
        inst_pids.add(inst_pid)
    for raw_pid in receipt.unresolved_pids:
        if type(raw_pid) is not int or raw_pid <= 0:
            return False
        if (
            target is None
            and type(child_pid) is int
            and child_pid > 0
            and raw_pid == child_pid
            and raw_pid != host_pid
        ):
            return False
        if raw_pid != host_pid and raw_pid not in inst_pids:
            return False
    return True


# LLM: 竞态来历：调用方预检时 host 还活着，采快照前 host 已按停止意图清完 child、写回已确认回执并自行退出，
#   出生身份读不到（未复用返回 identity_unavailable，立刻复用返回 identity_changed），终止原语未发信号。
#   改写只在独立事实同时成立时发生：①prior_blocked=False（无先前未完成树观察/未决项）；②当前记录已是 host 写的终态且
#   顶层 termination.confirmed=True；③instances_gone，即 host/child 两级实例均已按出生身份证明消失；④回执属于 host 目标，
#   且不携带仍存活的后代实例或未绑定出生身份的其它裸 PID。满足时改写成已确认的 host_exited_during_stop，
#   保持存储不变式“cleanup 确认 ⇒ 每张回执确认”。其它未确认回执与 child 回执原样保留。只读入参，返回新元组，不写 Store。
# 函数用途: 把“host 在停止期间自己正常退出”识别出来，避免它被误报成“停止结果未知”。
def _host_exit_settled_receipts(
    receipts: tuple[ProcessTerminationReceipt, ...],
    record: dict[str, object],
    *,
    instances_gone: bool,
    prior_blocked: bool = False,
    receipt_targets: tuple[str, ...] | None = None,
) -> tuple[ProcessTerminationReceipt, ...]:
    host_settled = (
        not prior_blocked
        and instances_gone
        and record["status"] in PROCESS_TERMINAL_STATUSES
        and (record.get("termination") or {}).get("confirmed") is True
    )
    if not host_settled:
        return receipts
    targets_match = receipt_targets is not None and len(receipt_targets) == len(receipts)
    return tuple(
        ProcessTerminationReceipt(
            _HOST_EXITED_DURING_STOP,
            True,
            None,
            receipt.observed_processes,
            (),
            tree_observation_complete=(
                True if receipt.tree_observation_complete is not None else None
            ),
        )
        if _host_race_receipt_settleable(
            receipt,
            receipt_targets[idx] if targets_match else None,
            record,
        )
        else receipt
        for idx, receipt in enumerate(receipts)
    )


# LLM: 出生标识在终止原语采集快照后再次核对；旧 PID 不授予新树，启动者绝不在此列表里。
# 函数用途: 在 Store 锁外尝试清理已冻结的托管进程、业务子进程与先前持久化未决后代实例，逐项保留未确认回执。
def _terminate_frozen_instances(
    record: dict[str, object],
    host_process: subprocess.Popen | None,
) -> tuple[ProcessTerminationReceipt, ...]:
    receipts: list[ProcessTerminationReceipt] = []
    targets: list[str] = []
    invoked_targets: set[tuple[int, str]] = set()
    observed_unresolved: set[tuple[int, str]] = set()
    for pid_key, birth_key in (("pid", "pid_birth_token"), ("child_pid", "child_pid_birth_token")):
        pid, birth = record[pid_key], record[birth_key]
        if not pid:
            continue
        # 未回收组长即使已退出，也要保留其出生身份来检查仍占用同组的后代。
        if capture_process_birth_token(pid) != birth and _process_instance_terminated(pid, birth):
            continue
        invoked_targets.add((int(pid), str(birth)))
        process = (
            host_process
            if pid_key == "pid" and host_process is not None and host_process.pid == pid
            else None
        )
        receipt = terminate_process_tree(
            pid, process, expected_birth_token=birth, grace_seconds=0.5,
        )
        receipts.append(receipt)
        targets.append("host" if pid_key == "pid" else "child")
        for item in receipt.unresolved_instances:
            observed_unresolved.add((int(item["pid"]), str(item["birth_token"])))

    prior = _extract_prior_obligations(
        record,
        include_top_level=True,
        include_top_level_bare=(record.get("status") == "unknown"),
    )
    for inst in prior.unresolved_instances:
        inst_pid = int(inst["pid"])
        inst_birth = str(inst["birth_token"])
        key = (inst_pid, inst_birth)
        if key in invoked_targets or key in observed_unresolved:
            continue
        if _process_instance_terminated(inst_pid, inst_birth):
            receipts.append(
                ProcessTerminationReceipt(
                    "unresolved_instance_gone",
                    True,
                    None,
                    1,
                    (),
                    tree_observation_complete=True,
                )
            )
            targets.append("prior_unresolved")
            continue
        if not _same_process(inst_pid, inst_birth):
            # 出生标识不能确认一致时绝不发信号，原样保留未决实例等待下次重试核对。
            receipts.append(
                ProcessTerminationReceipt(
                    "identity_unavailable",
                    False,
                    None,
                    0,
                    (inst_pid,),
                    tree_observation_complete=False,
                    unresolved_instances=({"pid": inst_pid, "birth_token": inst_birth},),
                )
            )
            targets.append("prior_unresolved")
            continue
        receipt = terminate_process_tree(
            inst_pid, None, expected_birth_token=inst_birth, grace_seconds=0.5,
        )
        if (
            receipt.method in ("identity_unavailable", "identity_changed")
            and _process_instance_terminated(inst_pid, inst_birth)
        ):
            receipt = ProcessTerminationReceipt(
                "unresolved_instance_gone",
                True,
                None,
                max(1, receipt.observed_processes),
                (),
                tree_observation_complete=True,
            )
        elif (
            not receipt.confirmed
            and not receipt.unresolved_instances
            and not _process_instance_terminated(inst_pid, inst_birth)
        ):
            receipt = ProcessTerminationReceipt(
                receipt.method,
                False,
                receipt.return_code,
                receipt.observed_processes,
                receipt.unresolved_pids or (inst_pid,),
                tree_observation_complete=receipt.tree_observation_complete,
                unresolved_instances=({"pid": inst_pid, "birth_token": inst_birth},),
            )
        receipts.append(receipt)
        targets.append("prior_unresolved")
    result = tuple(receipts)
    _frozen_receipt_targets.entry = (result, tuple(targets))
    return result
