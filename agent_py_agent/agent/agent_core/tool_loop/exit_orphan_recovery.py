# LLM: run 出口的孤儿子代理回收(REFACTORING_BACKLOG"孤儿子代理回收"专项,
#   实锤 R6a:主代理 RUN_EXIT 后后台 dispatch 进程继续运行 21 分钟、往交付区写
#   占位符产物,无人验收)。契约:①只在出口合同闸断放行且任务未收口时被调用
#   (final_exit_contract._unfinished_exit_response 接线,配置开关
#   run_exit_orphan_recovery_enabled);②进程层按 background_start.pid 客观事实
#   终止,pid 去重、
#   每进程只杀一次;③任务层只动 RUNNING(被杀后即僵尸状态):abandon attempt +
#   打回 PENDING(可重派,保住 resume 语义;ABANDONED 终态会让 dispatch 默认不捡),
#   BLOCKED/WAITING 等状态语义与进程无关,保持不动;④全程防御零异常上抛,逐项
#   错误进报告 errors。改动时同步检查 final_exit_contract、subagents/process_control、
#   tests/test_exit_orphan_recovery.py。
# 模块用途: 主代理带着没做完的任务退出前,把还在后台跑的子代理进程收掉,把被
#   打断的任务放回待派队列并留下结构化痕迹,避免孤儿进程白烧资源、污染交付区。
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

# 协议终态:回收只关心非终态任务(终态任务的进程理应已结束,且状态不该再动)。
from ...contracts.state_machine import TERMINAL_STATES, normalize_status
from ...runtime_errors import runtime_error_report
from ...subagents.process_control import terminate_pid_with_escalation

_RECOVERY_REASON = "parent_run_unfinished_exit"


@dataclass
class OrphanRecoveryReport:
    """出口回收的结构化结果(进 resume 块与任务 orphan_recovery 属性)。"""

    terminated_processes: list[dict[str, object]] = field(default_factory=list)
    requeued_run_ids: list[str] = field(default_factory=list)
    untouched_run_ids: list[str] = field(default_factory=list)
    # P2(Step3):live-pid 豁免——还在后台跑、这轮没被回收的子代理 run_id(wake 叫回续处)。
    exempted_live_run_ids: list[str] = field(default_factory=list)
    errors: list[dict[str, object]] = field(default_factory=list)

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_version": "exit_orphan_recovery.v1",
            "reason": _RECOVERY_REASON,
            "terminated_processes": self.terminated_processes,
            "requeued_run_ids": self.requeued_run_ids,
            "untouched_run_ids": self.untouched_run_ids,
            "exempted_live_run_ids": self.exempted_live_run_ids,
            "errors": self.errors,
        }


# LLM: 回收唯一入口。流程:任务工作区第一层子代理 → manager 全量 BFS 子树(覆盖
#   孙代理自己 spawn 的后台进程)→ 收集非终态任务的 background_start.pid → 去重
#   终止(SIGTERM 组→宽限→SIGKILL,见 process_control)→ RUNNING 任务 requeue 回
#   PENDING + orphan_recovery 留痕 + work log。先杀进程后改状态(否则 runner 线程
#   可能并发把任务写回 RUNNING)。副作用:发进程信号、改任务 store、写 work log。
# 函数用途: 主代理退出前的"清场":后台帮手进程全部收掉,被打断的活儿放回队列。
def recover_orphan_subagents(
    agent,
    task_root: Path | None,
    *,
    exempt_live_pids: bool = False,
) -> dict[str, object]:
    report = OrphanRecoveryReport()
    manager = getattr(agent, "subagents", None)
    if manager is None or task_root is None:
        return report.as_payload()
    tasks = _open_subtree_tasks(manager, Path(task_root), report)
    if exempt_live_pids:
        tasks = _exempt_live_tasks(agent, tasks, report)
    pid_groups = _collect_pid_groups(tasks)
    pid_reports = _terminate_pid_groups(pid_groups, report)
    for task in tasks:
        _recover_one_task(manager, task, pid_reports, report)
    return report.as_payload()


# LLM: P2 非阻塞出口门的 live-pid 豁免(source-gated,仅 wake-capable 来源开)。还有
#   活着后台派工的子代理不是"孤儿"——wake 事件会把主代理叫回继续处置,此刻杀它 /
#   requeue 反而打断正常后台工作。只把真僵尸(pid 死 / 无活线程,background_liveness
#   判定)留在待回收集合;live 的记进 exempted_live_run_ids 留痕、原地不动。
# 函数用途: 从待回收集合里摘掉"还在后台好好跑着"的子代理,只留真僵尸交给回收。
def _exempt_live_tasks(agent, tasks: list, report: OrphanRecoveryReport) -> list:
    from .background_liveness import is_task_background_live

    survivors: list = []
    for task in tasks:
        if is_task_background_live(task, agent):
            report.exempted_live_run_ids.append(str(getattr(task, "id", "") or ""))
        else:
            survivors.append(task)
    return survivors


# LLM: 子树事实采集:第一层 run_id 来自任务工作区 work/agents/ 目录名(与出口
#   合同触发判定 open_task_state_summary 同源),再用 manager.list_runs() 的
#   parent_id 索引 BFS 扩到孙代理;统一从 manager 权威 store 取任务对象,
#   canonical_state.json 只当名单线索。只返回非终态任务。
# 函数用途: 找齐"这轮 run 派出去且还没收口"的整棵子代理树。
def _open_subtree_tasks(manager, task_root: Path, report: OrphanRecoveryReport) -> list:
    seed_ids = _first_level_run_ids(task_root)
    if not seed_ids:
        return []
    children_by_parent = _children_index(manager, report)
    tasks: list = []
    seen: set[str] = set()
    queue = list(seed_ids)
    while queue:
        run_id = queue.pop(0)
        if run_id in seen:
            continue
        seen.add(run_id)
        task = _safe_load(manager, run_id, report)
        if task is None:
            continue
        queue.extend(children_by_parent.get(run_id, []))
        if normalize_status(getattr(task, "status", "")) not in TERMINAL_STATES:
            tasks.append(task)
    return tasks


# 函数用途: 列出任务工作区第一层子代理 run_id(work/agents/ 的子目录名)。
def _first_level_run_ids(task_root: Path) -> list[str]:
    agents_dir = task_root / "work" / "agents"
    if not agents_dir.is_dir():
        return []
    return sorted(entry.name for entry in agents_dir.iterdir() if entry.is_dir())


# 函数用途: 用全量任务列表建 parent_id → [child_id] 索引,BFS 找孙代理用。
def _children_index(manager, report: OrphanRecoveryReport) -> dict[str, list[str]]:
    try:
        all_tasks = manager.list_runs()
    except Exception as exc:
        report.errors.append(runtime_error_report(exc, context="exit_orphan_recovery.list_runs"))
        return {}
    index: dict[str, list[str]] = {}
    for task in all_tasks:
        parent_id = str(getattr(task, "parent_id", "") or "")
        if parent_id:
            index.setdefault(parent_id, []).append(str(getattr(task, "id", "") or ""))
    return index


# 函数用途: 防御式加载单个任务,失败记报告返回 None。
def _safe_load(manager, run_id: str, report: OrphanRecoveryReport):
    try:
        return manager.load(run_id)
    except Exception as exc:
        report.errors.append(
            {"run_id": run_id, **runtime_error_report(exc, context="exit_orphan_recovery.load")}
        )
        return None


# LLM: pid 分组:同一 launch 的多个任务共享一个 dispatch 进程 pid,杀一次、
#   报告挂到每个任务。pid 来自 background_start.pid(mark_background_start 落盘)。
# 函数用途: 把"哪些任务挂在哪个后台进程上"整理成 pid → 任务列表。
def _collect_pid_groups(tasks: list) -> dict[int, list[str]]:
    groups: dict[int, list[str]] = {}
    for task in tasks:
        pid = _task_background_pid(task)
        if pid > 0:
            groups.setdefault(pid, []).append(str(getattr(task, "id", "") or ""))
    return groups


# 函数用途: 从任务属性里读后台进程 pid,没有或坏值返回 0。
def _task_background_pid(task) -> int:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return 0
    background = attrs.get("background_start")
    if not isinstance(background, dict):
        return 0
    try:
        pid = int(background.get("pid") or 0)
    except (TypeError, ValueError):
        return 0
    return pid if pid > 0 else 0


# 函数用途: 逐个 pid 执行两阶段终止,结果同时进总报告和 per-pid 映射。
def _terminate_pid_groups(
    pid_groups: dict[int, list[str]],
    report: OrphanRecoveryReport,
) -> dict[int, dict[str, object]]:
    pid_reports: dict[int, dict[str, object]] = {}
    for pid, run_ids in sorted(pid_groups.items()):
        result = terminate_pid_with_escalation(pid)
        pid_reports[pid] = result
        report.terminated_processes.append({**result, "run_ids": run_ids})
    return pid_reports


# LLM: 单任务回收:仅 RUNNING 任务 requeue(abandon attempt → PENDING → 清
#   failure_type);其余非终态任务(BLOCKED/WAITING_* 等)状态不动,只记
#   untouched。两类都写 orphan_recovery 属性留痕。abandon_runner_attempt 内部
#   自带 load+save,之后必须重新 load 再改,避免覆盖。
# 函数用途: 给一个被打断的子代理任务安排去处:跑着的放回队列,其他的原地留痕。
def _recover_one_task(manager, task, pid_reports: dict[int, dict[str, object]], report: OrphanRecoveryReport) -> None:
    run_id = str(getattr(task, "id", "") or "")
    try:
        requeued = _requeue_or_annotate(manager, task, pid_reports)
    except Exception as exc:
        report.errors.append(
            {"run_id": run_id, **runtime_error_report(exc, context="exit_orphan_recovery.recover_one")}
        )
        return
    if requeued:
        report.requeued_run_ids.append(run_id)
    else:
        report.untouched_run_ids.append(run_id)


# 函数用途: 执行单任务的状态处置与留痕,返回是否发生 requeue。
def _requeue_or_annotate(manager, task, pid_reports: dict[int, dict[str, object]]) -> bool:
    run_id = str(getattr(task, "id", "") or "")
    previous_status = str(getattr(task, "status", "") or "")
    is_running = normalize_status(previous_status) == "RUNNING"
    attempt_id = str(getattr(task, "runner_active_attempt_id", "") or "").strip()
    if is_running and attempt_id:
        manager.lifecycle.abandon_runner_attempt(run_id, attempt_id, reason=_RECOVERY_REASON)
    refreshed = manager.load(run_id)
    _annotate_orphan_recovery(refreshed, pid_reports, previous_status, requeued=is_running)
    if is_running:
        refreshed.status = "PENDING"
        refreshed.failure_type = ""
    refreshed.updated_at = time.time()
    manager.save(refreshed)
    if is_running:
        manager.actions._append_task_work_log(
            refreshed,
            f"orphan_recovery: requeued RUNNING->PENDING reason={_RECOVERY_REASON}",
        )
    return is_running


# 函数用途: 把回收痕迹(原状态/进程报告/时间)写进任务属性,并把 background_start
#   标记为 terminated,防止后续误判后台进程仍活跃。
def _annotate_orphan_recovery(
    task,
    pid_reports: dict[int, dict[str, object]],
    previous_status: str,
    *,
    requeued: bool,
) -> None:
    attrs = dict(getattr(task, "attributes", {}) or {})
    pid = _task_background_pid(task)
    attrs["orphan_recovery"] = {
        "schema_version": "orphan_recovery.v1",
        "reason": _RECOVERY_REASON,
        "previous_status": previous_status,
        "requeued": requeued,
        "pid_report": pid_reports.get(pid, {"status": "no_pid"}),
        "recovered_at": time.time(),
    }
    background = attrs.get("background_start")
    if isinstance(background, dict) and pid > 0:
        updated = dict(background)
        updated["status"] = "terminated"
        updated["updated_at"] = time.time()
        attrs["background_start"] = updated
    task.attributes = attrs


__all__ = ["recover_orphan_subagents"]
