#!/usr/bin/env python3
"""有限状态协议模型的穷举检查器；不是 TLC，也不是生产源码的自动精化证明。

只用标准库、无文件/服务副作用。一个动作是一个原子协议步骤；BFS 穷举有限
域内的全部交错并保留最短父链。活性是 EF(terminal)，不是无公平性前提的 AF。
状态计数是真实去重后的可达状态，不含补数节点。共享台账由集成者维护。
"""

import argparse
import json
import time
from collections import Counter, deque
from typing import NamedTuple


class FiniteModel:
    """转移与性质是两个独立接口；突变只能修改转移，不能改变检查断言。"""

    needs_progress = False
    invariants = ()

    def initial_state(self):
        raise NotImplementedError

    def successors(self, state):
        raise NotImplementedError

    def is_terminal(self, state):
        raise NotImplementedError

    def state_violations(self, state):
        return ()

    def edge_violations(self, before, action, after):
        return ()

    def wait_cycle(self, state, enabled=None):
        return False

    def progress_required(self, state):
        return False

    def progress_goal(self, state):
        return self.is_terminal(state)


def json_state(state):
    """只在反例边界序列化状态，正常穷举不承担 JSON 成本。"""
    if hasattr(state, "_asdict"):
        return {key: json_state(value) for key, value in state._asdict().items()}
    if isinstance(state, tuple):
        return [json_state(value) for value in state]
    return state


def _trace(parents, state, extra=None):
    chain = []
    while parents[state] is not None:
        previous, action = parents[state]
        chain.append({"action": action, "state": json_state(state)})
        state = previous
    chain.append({"action": "Init", "state": json_state(state)})
    chain.reverse()
    if extra is not None:
        action, target = extra
        chain.append({"action": action, "state": json_state(target)})
    return chain


def replay_trace(model, trace):
    """逐边重放反例；拒绝错误初态、伪造动作或状态，不只检查 trace 非空。"""
    state = model.initial_state()
    if not trace or trace[0] != {"action": "Init", "state": json_state(state)}:
        raise ValueError("counterexample_initial_state_mismatch")
    for step in trace[1:]:
        candidates = [target for action, target in model.successors(state)
                      if action == step["action"] and json_state(target) == step["state"]]
        if len(candidates) != 1:
            raise ValueError("counterexample_transition_mismatch")
        state = candidates[0]
    return state


def check_model(model):
    """完整 BFS；状态/边安全性质、实际无进展状态和等待环分别检查，无深度截断。"""
    initial = model.initial_state()
    parents = {initial: None}
    distances = {initial: 0}
    queue = deque([initial])
    reverse = {} if model.needs_progress else None
    goals, deadlocks, witnesses = set(), set(), {}
    safety, liveness, actions = Counter(), Counter(), Counter()
    transitions = 0

    def witness(name, state, extra=None):
        # 同名性质可同时查状态和边；先发现的出边反例可能长于队列里未出队的状态。
        length = distances[state] + 1 + (extra is not None)
        if name not in witnesses or length < len(witnesses[name]["trace"]):
            witnesses[name] = {"trace": _trace(parents, state, extra)}

    while queue:
        state = queue.popleft()
        for name in model.state_violations(state):
            safety[name] += 1
            witness(name, state)
        next_steps = list(model.successors(state))
        if model.wait_cycle(state, next_steps) or not next_steps and not model.is_terminal(state):
            deadlocks.add(state)
            witness("deadlock_freedom", state)
        if model.needs_progress and model.progress_goal(state):
            goals.add(state)
        for action, target in next_steps:
            transitions += 1
            actions[action.split("(", 1)[0]] += 1
            for name in model.edge_violations(state, action, target):
                safety[name] += 1
                witness(name, state, (action, target))
            if reverse is not None:
                reverse.setdefault(target, []).append(state)
            if target not in parents:
                parents[target] = (state, action)
                distances[target] = distances[state] + 1
                queue.append(target)

    if reverse is not None:
        # 对整个已穷举图反向闭包，包含有环路径；不是给每个 pending 猜一个出口。
        can_finish = set(goals)
        work = deque(goals)
        while work:
            for previous in reverse.get(work.popleft(), ()):
                if previous not in can_finish:
                    can_finish.add(previous)
                    work.append(previous)
        for state in parents:
            if model.progress_required(state) and state not in can_finish:
                liveness[model.progress_property] += 1
                witness(model.progress_property, state)

    return {"name": type(model).__name__, "ok": not (safety or liveness or deadlocks),
            "reachable_states": len(parents), "transitions": transitions, "exhaustive": True,
            "invariants_checked": list(model.invariants),
            "progress_obligation_states": sum(model.progress_required(state) for state in parents),
            "invariant_violations": sum(safety.values()) + sum(liveness.values()),
            "safety_violations": dict(safety), "liveness_violations": dict(liveness),
            "deadlock_states": len(deadlocks), "actions": dict(sorted(actions.items())),
            "counterexamples": witnesses}


class LockState(NamedTuple):
    pc: tuple
    owners: tuple


class LockHierarchyDAGModel(FiniteModel):
    """T1=创建/owner，T2=原执行收口，T3=stop；忙失败逐把退 L/M/T 后重试。"""

    lock_order = ("CG", "GG", "TG", "BSG", "T", "M", "L")
    invariants = ("deadlock_freedom",)
    traces = ((0, 4, 5, 6), (4, 5, 6, 0), (0, 1, 2, 3))

    def __init__(self, *, blocking_cg=False):
        self.blocking_cg = blocking_cg

    def initial_state(self):
        return LockState((0, 0, 0), (-1,) * len(self.lock_order))

    def _next_lock(self, thread, pc):
        return self.traces[thread][pc] if pc < 4 else None

    def successors(self, state):
        for thread, pc in enumerate(state.pc):
            if pc == 11:
                continue
            owners, positions = list(state.owners), list(state.pc)
            lock = self._next_lock(thread, pc)
            if lock is not None:
                if owners[lock] == -1:
                    owners[lock], positions[thread] = thread, pc + 1
                    action = f"T{thread + 1}.acquire({self.lock_order[lock]})"
                elif thread == 1 and pc == 3 and not self.blocking_cg:
                    # Busy 是启用的异常转移，不是等待边；随后每次只释放一把锁。
                    positions[thread] = 8
                    action = "T2.SubagentCreationGuardBusy(CG)"
                else:
                    continue
            elif pc < 8:
                lock = self.traces[thread][7 - pc]
                owners[lock] = -1
                positions[thread] = 11 if pc == 7 else pc + 1
                action = f"T{thread + 1}.release({self.lock_order[lock]})"
            else:
                lock = (6, 5, 4)[pc - 8]
                owners[lock] = -1
                positions[thread] = 0 if pc == 10 else pc + 1
                action = f"T2.busy_unwind({self.lock_order[lock]})"
            yield action, LockState(tuple(positions), tuple(owners))

    def is_terminal(self, state):
        return state.pc == (11, 11, 11) and all(owner == -1 for owner in state.owners)

    def wait_cycle(self, state, enabled=None):
        # 固定 oracle 只观察实际关系：争锁线程若能退让，就不是阻塞等待。
        # 不读 blocking_cg 开关，避免突变时偷偷切换检查规则。
        enabled = list(self.successors(state)) if enabled is None else enabled
        advancing = {thread for _action, target in enabled for thread in range(len(state.pc))
                     if target.pc[thread] != state.pc[thread]}
        waits = {}
        for thread, pc in enumerate(state.pc):
            lock = self._next_lock(thread, pc)
            if lock is not None and state.owners[lock] not in (-1, thread) and thread not in advancing:
                waits[thread] = state.owners[lock]
        for start in waits:
            seen, node = set(), start
            while node in waits:
                if node in seen:
                    return True
                seen.add(node)
                node = waits[node]
        return False


class GenerationState(NamedTuple):
    active_gen: int
    worker_count: int
    closure_state: str
    live_workers_mask: int
    pending_req_gen: int
    pending_req_worker: int
    pending_req_status: str


class MultiprocessGenerationAndRoutingModel(FiniteModel):
    """代次做保持相等关系的商：两 fresh 名轮换，旧请求永久映为 -1，避免 ABA。

    start 的前提是调用方已拥有原生命周期排他权且旧 worker 均退出；不模拟
    P2 的物理死亡证明。退役与 startup 是同一个受锁原子操作，不补封 closure。
    """

    needs_progress = True
    progress_property = "no_zombie_pending_request"
    invariants = ("no_startup_brick", "no_zombie_pending_request")

    def __init__(self, *, skip_retirement=False, disable_reconcile=False):
        self.skip_retirement = skip_retirement
        self.disable_reconcile = disable_reconcile

    def initial_state(self):
        return GenerationState(0, 0, "sealed", 0, -1, -1, "absent")

    def start_generation(self, state, count):
        if state.live_workers_mask:
            return None
        needs_retirement = state.closure_state == "unclean_null" or state.worker_count != count
        if needs_retirement and self.skip_retirement:
            return None
        # 保留旧请求原归属而非 restamp；-1 代表任何已退役代次，不会被 fresh 复用。
        old_req = -1 if state.pending_req_status != "absent" else state.pending_req_gen
        return GenerationState(1 - state.active_gen, count, "open", (1 << count) - 1,
                               old_req, state.pending_req_worker, state.pending_req_status)

    def successors(self, state):
        for count in (0, 2):
            target = self.start_generation(state, count)
            if target is not None:
                yield f"start_generation({count};retire_dead_generations)", target
        for worker in range(state.worker_count):
            if state.live_workers_mask & (1 << worker):
                yield f"worker_crash({worker})", state._replace(
                    live_workers_mask=state.live_workers_mask & ~(1 << worker))
        if state.closure_state != "unclean_null":
            yield "unclean_coordinator_crash()", state._replace(closure_state="unclean_null", live_workers_mask=0)
        if state.pending_req_status != "pending" and state.closure_state == "open":
            for worker in (0, 1, 2):
                yield f"submit_request({worker})", state._replace(pending_req_gen=state.active_gen,
                    pending_req_worker=worker, pending_req_status="pending")
        if state.pending_req_status == "pending":
            worker = state.pending_req_worker
            unroutable = (state.pending_req_gen != state.active_gen or state.worker_count == 0
                          or worker >= state.worker_count)
            if unroutable and not self.disable_reconcile:
                yield "reconcile_unroutable_requests()", state._replace(pending_req_status="failed_unroutable")
            if not unroutable and state.live_workers_mask & (1 << worker):
                yield f"worker_claim_and_complete({worker})", state._replace(pending_req_status="completed")

    def is_terminal(self, state):
        return False  # Gateway 是有环服务，不把空闲状态错误标成停机死锁。

    def state_violations(self, state):
        if state.live_workers_mask == 0:
            for count in (0, 2):
                if self.start_generation(state, count) is None:
                    yield "no_startup_brick"

    def progress_required(self, state):
        return state.pending_req_status == "pending"

    def progress_goal(self, state):
        return state.pending_req_status in ("completed", "failed_unroutable")


class StoreState(NamedTuple):
    statuses: tuple
    locks: int
    holders: int
    stale: int
    body_bound: int
    archived: int


class SessionTaskStoreStateMachineModel(FiniteModel):
    """同 scope 两记录争执行权；元数据锁与尚未证退出的执行 holder 分开。

    对应 session_tasks.bind_body / store_tasks 窄更新和 store_claims 原锁归档合同。
    每个 ID 仅入队一次，已归档 ID 不重用；显式新任务不是复活旧终态。
    """

    terminal_statuses = frozenset(("done", "failed", "cancelled"))
    invariants = ("terminal_irreversibility", "same_scope_mutual_exclusion", "active_lock_archive_exclusion")

    def __init__(self, *, stale_overwrite=False, archive_active=False, duplicate_start=False):
        self.stale_overwrite = stale_overwrite
        self.archive_active = archive_active
        self.duplicate_start = duplicate_start

    def initial_state(self):
        return StoreState(("absent", "absent"), 0, 0, 0, 0, 0)

    def successors(self, state):
        for index, status in enumerate(state.statuses):
            bit = 1 << index

            def changed(new_status, **fields):
                values = list(state.statuses)
                values[index] = new_status
                return state._replace(statuses=tuple(values), **fields)

            if status == "absent":
                yield f"enqueue({index})", changed("queued")
                continue
            if not state.archived & bit:
                if not state.locks & bit:
                    yield f"acquire_lock({index})", state._replace(locks=state.locks | bit)
                else:
                    yield f"release_lock({index})", state._replace(locks=state.locks & ~bit)
                    if status == "queued" and (not state.holders or self.duplicate_start):
                        yield f"start_running({index})", changed("running", holders=state.holders | bit,
                                                               stale=state.stale | bit)
                    if status == "running":
                        for result in ("done", "failed"):
                            yield f"mark_{result}({index})", changed(result)
                    if status in ("queued", "running"):
                        yield f"mark_cancelled({index})", changed("cancelled")
                    if state.stale & bit:
                        # 原窄绑定仅补正文，取消状态不来自旧 running 快照。
                        result = "running" if self.stale_overwrite else status
                        yield f"late_bind_body({index})", changed(result, stale=state.stale & ~bit,
                                                                 body_bound=state.body_bound | bit)
                if (status in self.terminal_statuses and not state.holders & bit
                        and (not state.locks & bit or self.archive_active)):
                    yield f"archive_sweep({index})", state._replace(archived=state.archived | bit)
            elif state.stale & bit:
                yield f"late_bind_archived_noop({index})", state._replace(stale=state.stale & ~bit)
            if state.holders & bit and status in self.terminal_statuses:
                yield f"execution_exit_observed({index})", state._replace(holders=state.holders & ~bit)

    def is_terminal(self, state):
        return state.archived == 3 and not (state.holders or state.locks or state.stale)

    def state_violations(self, state):
        if state.holders.bit_count() > 1:
            yield "same_scope_mutual_exclusion"
        if state.archived & state.locks:
            yield "active_lock_archive_exclusion"

    def edge_violations(self, before, action, after):
        for index, status in enumerate(before.statuses):
            if status in self.terminal_statuses and after.statuses[index] in ("queued", "running"):
                yield "terminal_irreversibility"
        if (after.archived & ~before.archived) & before.locks:
            yield "active_lock_archive_exclusion"


class ClaimStoreStateMachineModel(SessionTaskStoreStateMachineModel):
    """独立穷举相同的 scope/终态/原锁合同；记录身份是 claim，不与 task 状态混算。"""


class PtyState(NamedTuple):
    next_id: int
    entries: tuple
    proofs: tuple


class PtySessionRegistryStateMachineModel(FiniteModel):
    """K=4、K+1 个不复用身份制造真实容量压力；证明仓独立于有界内存表。

    ack 表示退出码已耐久保存。无 ack 时保持原条目；正常淘汰只选最老终态，
    不靠表里已经没有条目推断进程退出。引用 tooling/pty_sessions.py 的责任边界。
    """

    invariants = ("exit_proof_monotonicity", "bounded_pty_table")

    def __init__(self, *, capacity=4, evict_unacked=False):
        if capacity < 1:
            raise ValueError("positive_capacity_required")
        self.capacity, self.sessions = capacity, capacity + 1
        self.evict_unacked = evict_unacked

    def initial_state(self):
        return PtyState(0, ("unused",) * self.sessions, (None,) * self.sessions)

    def successors(self, state):
        entries, proofs = state.entries, state.proofs
        occupancy = sum(value not in ("unused", "evicted_0", "evicted_1") for value in entries)

        def changed(index, status, proof=None):
            values, saved = list(entries), list(proofs)
            values[index] = status
            if proof is not None:
                saved[index] = proof
            return state._replace(entries=tuple(values), proofs=tuple(saved))

        if state.next_id < self.sessions and occupancy < self.capacity:
            target = changed(state.next_id, "running")._replace(next_id=state.next_id + 1)
            yield f"spawn_session({state.next_id})", target
        oldest = None
        for index, status in enumerate(entries):
            if status == "running":
                for code in (0, 1):
                    yield f"exit_session({index},{code})", changed(index, f"exited_{code}")
            if status.startswith(("exited_", "acked_")) and oldest is None:
                oldest = index
            if status.startswith("exited_"):
                code = int(status[-1])
                yield f"ack_exit_proof({index},{code})", changed(index, f"acked_{code}", code)
        if oldest is not None and (entries[oldest].startswith("acked_") or self.evict_unacked):
            yield f"evict_oldest({oldest})", changed(oldest, f"evicted_{entries[oldest][-1]}")

    def is_terminal(self, state):
        return state.next_id == self.sessions and all(value.startswith("evicted_") for value in state.entries)

    def state_violations(self, state):
        if sum(value not in ("unused", "evicted_0", "evicted_1") for value in state.entries) > self.capacity:
            yield "bounded_pty_table"
        for index, status in enumerate(state.entries):
            if status.startswith("evicted_") and state.proofs[index] != int(status[-1]):
                yield "exit_proof_monotonicity"

    def edge_violations(self, before, action, after):
        for index, proof in enumerate(before.proofs):
            if proof is not None and after.proofs[index] != proof:
                yield "exit_proof_monotonicity"
            if before.entries[index].startswith("exited_") and after.entries[index].startswith("evicted_"):
                if after.proofs[index] != int(before.entries[index][-1]):
                    yield "exit_proof_monotonicity"


class WorkflowState(NamedTuple):
    status: str
    steps: tuple
    callbacks: int


class WorkflowEngineStateMachineModel(FiniteModel):
    """两独立步骤、取消与迟到成功/失败回调；第一个 canonical 终态胜出。

    收敛不是声称不同外部输入都返回同一个结果：固定已提交的终态后，所有回调
    排列都保持同一 workflow/step 投影，并严格减少 outstanding callback 数。
    此不变量的归纳加上完整图的可终结性，证明回调清空后的唯一正常形。
    """

    terminal_statuses = frozenset(("completed", "failed", "cancelled"))
    needs_progress = True
    progress_property = "workflow_finite_convergence"
    invariants = ("terminal_irreversibility", "deterministic_convergence", "workflow_finite_convergence")

    def __init__(self, *, late_completion=False):
        self.late_completion = late_completion

    def initial_state(self):
        return WorkflowState("pending", ("pending", "pending"), 0)

    def successors(self, state):
        if state.status not in self.terminal_statuses:
            cancelled = tuple("cancelled" if step in ("pending", "running") else step for step in state.steps)
            yield "cancel_workflow()", WorkflowState("cancelled", cancelled, state.callbacks)
            for index, step in enumerate(state.steps):
                if step == "pending":
                    steps = list(state.steps)
                    steps[index] = "running"
                    yield f"start_step({index})", WorkflowState("running", tuple(steps), state.callbacks | (1 << index))
        for index in range(len(state.steps)):
            bit = 1 << index
            if not state.callbacks & bit:
                continue
            for outcome in ("completed", "failed"):
                steps, status = list(state.steps), state.status
                if status not in self.terminal_statuses:
                    steps[index] = outcome
                    if outcome == "failed":
                        status = "failed"
                        steps = ["cancelled" if step in ("pending", "running") else step for step in steps]
                    elif all(step == "completed" for step in steps):
                        status = "completed"
                elif self.late_completion:
                    # 故意错误的旧快照回写；oracle 不随此开关改变。
                    steps[index], status = outcome, "running"
                yield f"deliver_callback({index},{outcome})", WorkflowState(status, tuple(steps), state.callbacks & ~bit)

    def is_terminal(self, state):
        return state.status in self.terminal_statuses and state.callbacks == 0

    def progress_required(self, state):
        return not self.is_terminal(state)

    def edge_violations(self, before, action, after):
        if before.status in self.terminal_statuses and before.status != after.status:
            yield "terminal_irreversibility"
        if any(old in self.terminal_statuses and old != new for old, new in zip(before.steps, after.steps)):
            yield "terminal_irreversibility"
        if before.status in self.terminal_statuses:
            if ((before.status, before.steps) != (after.status, after.steps)
                    or after.callbacks.bit_count() >= before.callbacks.bit_count()):
                yield "deterministic_convergence"


def verify_counterfactual_blocking_cg_deadlocks():
    """同一 oracle、仅把 T2 的 CG 获取改为阻塞，必须实际找到等待环。"""
    report = check_model(LockHierarchyDAGModel(blocking_cg=True))
    return {**report, "mutation_detected": report["deadlock_states"] > 0}


def mutation_names():
    return ("blocking_cg", "skip_retirement", "disable_reconcile", "stale_bind_body",
            "archive_active_lock", "duplicate_start", "evict_unacked", "late_completion")


def run_all_models(*, mutation=None):
    """五组报告（task/claim 分别穷举后归为一组），计数只来自 BFS，不缓存旧结果。"""
    if mutation is not None and mutation not in mutation_names():
        raise ValueError("unknown_model_mutation")
    started = time.perf_counter()
    locks = check_model(LockHierarchyDAGModel(blocking_cg=mutation == "blocking_cg"))
    generation = check_model(MultiprocessGenerationAndRoutingModel(
        skip_retirement=mutation == "skip_retirement", disable_reconcile=mutation == "disable_reconcile"))
    store_flags = {"stale_overwrite": mutation == "stale_bind_body",
                   "archive_active": mutation == "archive_active_lock", "duplicate_start": mutation == "duplicate_start"}
    stores = [check_model(cls(**store_flags)) for cls in (SessionTaskStoreStateMachineModel, ClaimStoreStateMachineModel)]
    store_group = {"name": "SessionTaskStoreStateMachineModel+ClaimStoreStateMachineModel", "components": stores,
                   "ok": all(report["ok"] for report in stores), "exhaustive": all(report["exhaustive"] for report in stores),
                   **{key: sum(report[key] for report in stores)
                      for key in ("reachable_states", "transitions", "invariant_violations", "deadlock_states")}}
    models = [locks, generation, store_group,
              check_model(PtySessionRegistryStateMachineModel(evict_unacked=mutation == "evict_unacked")),
              check_model(WorkflowEngineStateMachineModel(late_completion=mutation == "late_completion"))]
    counterfactual = verify_counterfactual_blocking_cg_deadlocks()
    return {"schema": "tla_state_machines.v1", "ok": all(model["ok"] for model in models)
            and counterfactual["mutation_detected"], "models": models,
            **{key: sum(report[key] for report in models)
               for key in ("reachable_states", "transitions", "invariant_violations", "deadlock_states")},
            "counterfactual_blocking_cg": counterfactual,
            "semantics": {"exploration": "exhaustive finite BFS; no depth cutoff",
                          "liveness": "AG(pending implies EF terminal); no scheduler fairness claim",
                          "workflow": "first canonical terminal wins; all late callback orders converge",
                          "generation": "fresh identities quotient; old pending generation never reused"},
            "bounds": {"threads": 3, "worker_counts": [0, 2], "same_scope_records": 2,
                       "pty_capacity": 4, "pty_session_ids": 5, "workflow_steps": 2},
            "elapsed_seconds": time.perf_counter() - started}


def main(argv=None):
    parser = argparse.ArgumentParser(description="穷举五组有限状态协议模型，失败打印可重放反例。")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON 报告")
    parser.add_argument("--mutation", choices=mutation_names(), help="反事实：只破坏转移、不修改 oracle")
    args = parser.parse_args(argv)
    report = run_all_models(mutation=args.mutation)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(f"{'通过' if report['ok'] else '失败'}：{report['reachable_states']} 个可达状态，"
              f"{report['invariant_violations']} 个不变量违反，{report['deadlock_states']} 个死锁状态")
        if not report["ok"]:
            print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


# === Pytest Unit Tests ===
"""FG1：完整有限图、独立负例、反例逐边重放及真实 CLI；不替代生产精化证明。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def report():
    started = time.perf_counter()
    result = run_all_models()
    return result, time.perf_counter() - started


def _step(model, state, action):
    targets = [target for name, target in model.successors(state) if name == action]
    assert len(targets) == 1, (action, list(model.successors(state)))
    return targets[0]


def _reachable(model, initial=None):
    initial = model.initial_state() if initial is None else initial
    seen, queue = {initial}, deque([initial])
    while queue:
        state = queue.popleft()
        yield state
        for _action, target in model.successors(state):
            if target not in seen:
                seen.add(target)
                queue.append(target)


def _cli(*args):
    root = Path(__file__).resolve().parents[2]
    return subprocess.run([sys.executable, str(Path(__file__).resolve()), "--json", *args],
        cwd=root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ".:agent_py_agent"},
        text=True, capture_output=True, timeout=10, check=False)


def test_all_five_groups_exhaustive_safe_and_fast(report):
    result, elapsed = report
    assert result["ok"] is True
    assert len(result["models"]) == 5
    assert result["reachable_states"] >= 500
    assert result["invariant_violations"] == result["deadlock_states"] == 0
    assert elapsed < 2.0
    # 内外计时采用同一CI墙钟预算；另钉完整穷举规模，不能靠少跑状态变快。
    assert result["elapsed_seconds"] < 2.0
    assert {group["name"]: (group["reachable_states"], group["transitions"])
            for group in result["models"]} == {
        "LockHierarchyDAGModel": (257, 447),
        "MultiprocessGenerationAndRoutingModel": (211, 855),
        "SessionTaskStoreStateMachineModel+ClaimStoreStateMachineModel": (3016, 9264),
        "PtySessionRegistryStateMachineModel": (15233, 47268),
        "WorkflowEngineStateMachineModel": (23, 42),
    }
    assert result["reachable_states"] == sum(item["reachable_states"] for item in result["models"])
    assert result["transitions"] == sum(item["transitions"] for item in result["models"])
    for group in result["models"]:
        assert group["ok"] and group["exhaustive"] and group["reachable_states"] > 1
        assert group["transitions"] > 0
        assert group["invariant_violations"] == group["deadlock_states"] == 0
    stores = result["models"][2]["components"]
    assert [item["name"] for item in stores] == ["SessionTaskStoreStateMachineModel", "ClaimStoreStateMachineModel"]
    for item in (result["models"][1], result["models"][4]):
        assert item["progress_obligation_states"] > 0
        assert item["liveness_violations"] == {}


def test_json_cli_is_real_subprocess_and_bounded():
    started = time.perf_counter()
    process = _cli()
    elapsed = time.perf_counter() - started
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["schema"] == "tla_state_machines.v1" and result["ok"]
    assert result["invariant_violations"] == result["deadlock_states"] == 0
    assert result["reachable_states"] >= 500 and len(result["models"]) == 5
    # 真实解释器启动与导入采用5s CI预算，仍小于原10s子进程超时。
    assert elapsed < 5.0
    assert process.stderr == ""


def test_blocking_cg_counterfactual_has_exact_shortest_replayable_cycle(report):
    broken = LockHierarchyDAGModel(blocking_cg=True)
    result = verify_counterfactual_blocking_cg_deadlocks()
    assert result["mutation_detected"] and result["deadlock_states"] > 0 and not result["ok"]
    trace = result["counterexamples"]["deadlock_freedom"]["trace"]
    assert [step["action"] for step in trace] == ["Init", "T1.acquire(CG)", "T2.acquire(T)",
                                                "T2.acquire(M)", "T2.acquire(L)"]
    final = replay_trace(broken, trace)
    assert broken.wait_cycle(final) and not list(broken.successors(final))
    assert result == verify_counterfactual_blocking_cg_deadlocks()
    assert report[0]["counterfactual_blocking_cg"] == result


def test_trylock_is_enabled_and_unwinds_each_gate_before_retry():
    model = LockHierarchyDAGModel()
    state = model.initial_state()
    for action in ("T1.acquire(CG)", "T2.acquire(T)", "T2.acquire(M)", "T2.acquire(L)"):
        state = _step(model, state, action)
    assert not model.wait_cycle(state)
    state = _step(model, state, "T2.SubagentCreationGuardBusy(CG)")
    for lock in ("L", "M", "T"):
        state = _step(model, state, f"T2.busy_unwind({lock})")
    assert state.pc[1] == 0 and 1 not in state.owners
    assert "T2.acquire(T)" in dict(model.successors(state))
    rank = {lock: index for index, lock in enumerate(model.lock_order)}
    for thread in (0, 2):
        assert list(model.traces[thread]) == sorted(model.traces[thread])
    assert rank["CG"] < rank["GG"] < rank["TG"] < rank["BSG"] < rank["T"] < rank["M"] < rank["L"]


@pytest.mark.parametrize("factory,property_name", [
    (lambda: LockHierarchyDAGModel(blocking_cg=True), "deadlock_freedom"),
    (lambda: MultiprocessGenerationAndRoutingModel(skip_retirement=True), "no_startup_brick"),
    (lambda: MultiprocessGenerationAndRoutingModel(disable_reconcile=True), "no_zombie_pending_request"),
    (lambda: SessionTaskStoreStateMachineModel(stale_overwrite=True), "terminal_irreversibility"),
    (lambda: ClaimStoreStateMachineModel(stale_overwrite=True), "terminal_irreversibility"),
    (lambda: SessionTaskStoreStateMachineModel(archive_active=True), "active_lock_archive_exclusion"),
    (lambda: ClaimStoreStateMachineModel(archive_active=True), "active_lock_archive_exclusion"),
    (lambda: SessionTaskStoreStateMachineModel(duplicate_start=True), "same_scope_mutual_exclusion"),
    (lambda: ClaimStoreStateMachineModel(duplicate_start=True), "same_scope_mutual_exclusion"),
    (lambda: PtySessionRegistryStateMachineModel(evict_unacked=True), "exit_proof_monotonicity"),
    (lambda: WorkflowEngineStateMachineModel(late_completion=True), "terminal_irreversibility"),
    (lambda: WorkflowEngineStateMachineModel(late_completion=True), "deterministic_convergence"),
])
def test_mutated_transition_is_detected_and_counterexample_replays(factory, property_name):
    model = factory()
    result = check_model(model)
    assert not result["ok"] and result["exhaustive"]
    assert result["invariant_violations"] > 0 or result["deadlock_states"] > 0
    trace = result["counterexamples"][property_name]["trace"]
    final = replay_trace(model, trace)
    if property_name == "deadlock_freedom":
        assert model.wait_cycle(final) or not list(model.successors(final))
    elif property_name == "no_zombie_pending_request":
        assert model.progress_required(final)
        assert not any(model.progress_goal(state) for state in _reachable(model, final))
    elif property_name not in set(model.state_violations(final)):
        previous = replay_trace(model, trace[:-1])
        assert property_name in set(model.edge_violations(previous, trace[-1]["action"], final))
    assert result["counterexamples"] == check_model(factory())["counterexamples"]


def test_bfs_does_not_confuse_stuck_nonterminal_with_success():
    class Stuck(FiniteModel):
        def initial_state(self):
            return 0

        def successors(self, state):
            return ()

        def is_terminal(self, state):
            return False

    result = check_model(Stuck())
    assert result["reachable_states"] == result["deadlock_states"] == 1
    assert not result["ok"]
    assert result["counterexamples"]["deadlock_freedom"]["trace"] == [{"action": "Init", "state": 0}]


def test_enabled_cycle_without_terminal_is_a_liveness_failure():
    class Livelock(FiniteModel):
        needs_progress = True
        progress_property = "must_finish"

        def initial_state(self):
            return 0

        def successors(self, state):
            yield "tick()", 1 - state

        def is_terminal(self, state):
            return False

        def progress_required(self, state):
            return True

    result = check_model(Livelock())
    assert result["deadlock_states"] == 0
    assert result["liveness_violations"] == {"must_finish": 2} and not result["ok"]


def test_counterexample_replay_rejects_forged_action_and_state(report):
    original = report[0]["counterfactual_blocking_cg"]["counterexamples"]["deadlock_freedom"]["trace"]
    for patch in ({"action": "invented()"}, {"state": {"invented": True}}):
        forged = [*original[:-1], {**original[-1], **patch}]
        with pytest.raises(ValueError, match="counterexample_transition_mismatch"):
            replay_trace(LockHierarchyDAGModel(blocking_cg=True), forged)


@pytest.mark.parametrize("count", [0, 2])
def test_all_dead_generations_can_start_both_counts_after_null_closure(count):
    model = MultiprocessGenerationAndRoutingModel()
    state = model.start_generation(model.initial_state(), 2)
    state = _step(model, state, "submit_request(0)")
    state = _step(model, state, "unclean_coordinator_crash()")
    assert state.closure_state == "unclean_null" and state.live_workers_mask == 0
    started = model.start_generation(state, count)
    assert started is not None and started.worker_count == count
    assert started.closure_state == "open" and started.live_workers_mask == (1 << count) - 1
    assert started.pending_req_status == "pending" and started.pending_req_gen != started.active_gen
    failed = _step(model, started, "reconcile_unroutable_requests()")
    assert failed.pending_req_status == "failed_unroutable"
    broken = MultiprocessGenerationAndRoutingModel(skip_retirement=True)
    assert broken.start_generation(state, count) is None


def test_generation_quotient_cannot_restamp_old_request_via_aba():
    model = MultiprocessGenerationAndRoutingModel()
    state = model.start_generation(model.initial_state(), 2)
    state = _step(model, state, "submit_request(1)")
    original = state.pending_req_gen
    state = _step(model, state, "unclean_coordinator_crash()")
    state = model.start_generation(state, 0)
    state = model.start_generation(state, 2)
    assert state.active_gen == original and state.pending_req_gen == -1
    assert state.pending_req_status == "pending"
    assert not any(action.startswith("worker_claim_and_complete") for action, _ in model.successors(state))
    assert _step(model, state, "reconcile_unroutable_requests()").pending_req_status == "failed_unroutable"


def test_generation_oracle_checks_every_reachable_zero_worker_state():
    model = MultiprocessGenerationAndRoutingModel()
    dead, pending = 0, 0
    for state in _reachable(model):
        if state.live_workers_mask == 0:
            dead += 1
            assert all(model.start_generation(state, count) is not None for count in (0, 2))
        pending += state.pending_req_status == "pending"
    assert dead > 1 and pending > 1


@pytest.mark.parametrize("model_type", [SessionTaskStoreStateMachineModel, ClaimStoreStateMachineModel])
def test_stale_bind_preserves_cancelled_and_active_lock_cannot_archive(model_type):
    model = model_type()
    state = model.initial_state()
    for action in ("enqueue(0)", "acquire_lock(0)", "start_running(0)", "mark_cancelled(0)", "late_bind_body(0)"):
        state = _step(model, state, action)
    assert state.statuses[0] == "cancelled" and state.body_bound & 1
    state = _step(model, state, "execution_exit_observed(0)")
    assert "archive_sweep(0)" not in dict(model.successors(state))
    state = _step(model, state, "release_lock(0)")
    state = _step(model, state, "archive_sweep(0)")
    assert state.archived & 1 and state.statuses[0] == "cancelled"


@pytest.mark.parametrize("model_type", [SessionTaskStoreStateMachineModel, ClaimStoreStateMachineModel])
def test_terminal_status_does_not_release_execution_holder_early(model_type):
    model = model_type()
    state = model.initial_state()
    for action in ("enqueue(0)", "enqueue(1)", "acquire_lock(0)", "acquire_lock(1)",
                   "start_running(0)", "mark_done(0)"):
        state = _step(model, state, action)
    assert state.statuses[0] == "done" and state.holders == 1
    assert "start_running(1)" not in dict(model.successors(state))
    state = _step(model, state, "execution_exit_observed(0)")
    state = _step(model, state, "start_running(1)")
    assert state.holders == 2 and state.holders.bit_count() == 1


@pytest.mark.parametrize("exit_code", [0, 1])
def test_pty_capacity_pressure_requires_ack_and_preserves_exact_exit_proof(exit_code):
    model = PtySessionRegistryStateMachineModel()
    state = model.initial_state()
    for index in range(model.capacity):
        state = _step(model, state, f"spawn_session({index})")
    assert not any(action.startswith("spawn_session") for action, _ in model.successors(state))
    state = _step(model, state, f"exit_session(0,{exit_code})")
    assert "evict_oldest(0)" not in dict(model.successors(state))
    state = _step(model, state, f"ack_exit_proof(0,{exit_code})")
    state = _step(model, state, "evict_oldest(0)")
    state = _step(model, state, "spawn_session(4)")
    assert state.entries[0] == f"evicted_{exit_code}" and state.proofs[0] == exit_code
    assert state.entries[4] == "running" and not list(model.state_violations(state))


def test_pty_model_rejects_invalid_capacity():
    with pytest.raises(ValueError, match="positive_capacity_required"):
        PtySessionRegistryStateMachineModel(capacity=0)


def test_workflow_two_late_callbacks_converge_in_both_delivery_orders():
    model = WorkflowEngineStateMachineModel()
    state = _step(model, model.initial_state(), "start_step(0)")
    state = _step(model, state, "start_step(1)")
    assert state.callbacks == 3
    cancelled = _step(model, state, "cancel_workflow()")
    first = _step(model, cancelled, "deliver_callback(0,completed)")
    first = _step(model, first, "deliver_callback(1,failed)")
    second = _step(model, cancelled, "deliver_callback(1,failed)")
    second = _step(model, second, "deliver_callback(0,completed)")
    assert first == second == WorkflowState("cancelled", ("cancelled", "cancelled"), 0)
    assert model.is_terminal(first) and not list(model.successors(first))


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_workflow_normal_success_keeps_original_terminal_decision(order):
    model = WorkflowEngineStateMachineModel()
    state = _step(model, model.initial_state(), "start_step(0)")
    state = _step(model, state, "start_step(1)")
    for index in order:
        state = _step(model, state, f"deliver_callback({index},completed)")
    assert state == WorkflowState("completed", ("completed", "completed"), 0)
    assert not list(model.successors(state))


@pytest.mark.parametrize("mutation", ["blocking_cg", "skip_retirement", "stale_bind_body"])
def test_mutated_cli_exits_one_and_prints_actual_counterexamples(mutation):
    process = _cli("--mutation", mutation)
    assert process.returncode == 1, process.stderr
    result = json.loads(process.stdout)
    assert not result["ok"]
    assert result["deadlock_states"] > 0 or result["invariant_violations"] > 0
    groups = result["models"]
    leaves = [leaf for group in groups for leaf in group.get("components", [group])]
    assert any(leaf["counterexamples"] for leaf in leaves)


def test_unknown_mutation_is_not_silently_ignored():
    with pytest.raises(ValueError, match="unknown_model_mutation"):
        run_all_models(mutation="not-a-model-mutation")


def test_mixed_state_and_edge_property_keeps_the_actual_shortest_witness():
    class MixedProperty(FiniteModel):
        def initial_state(self):
            return "Init"

        def successors(self, state):
            if state == "Init":
                yield "visit_A()", "A"
                yield "visit_B()", "B"
            elif state == "A":
                yield "visit_C()", "C"

        def is_terminal(self, state):
            return state in ("B", "C")

        def state_violations(self, state):
            return ("mixed_property",) if state == "B" else ()

        def edge_violations(self, before, action, after):
            return ("mixed_property",) if (before, after) == ("A", "C") else ()

    model = MixedProperty()
    result = check_model(model)
    trace = result["counterexamples"]["mixed_property"]["trace"]
    assert trace == [{"action": "Init", "state": "Init"}, {"action": "visit_B()", "state": "B"}]
    assert replay_trace(model, trace) == "B"


@pytest.mark.parametrize("actual_blocking", [False, True])
def test_wait_graph_oracle_uses_actual_transitions_not_mutation_switch(actual_blocking, monkeypatch):
    actual = LockHierarchyDAGModel(blocking_cg=actual_blocking)
    # 故意令声明开关与真实转移矛盾；oracle 必须观察真实关系，不能偷看开关。
    observed = LockHierarchyDAGModel(blocking_cg=not actual_blocking)
    monkeypatch.setattr(observed, "successors", actual.successors)
    state = actual.initial_state()
    for action in ("T1.acquire(CG)", "T2.acquire(T)", "T2.acquire(M)", "T2.acquire(L)"):
        state = _step(actual, state, action)
    assert observed.wait_cycle(state) is actual_blocking
    assert (check_model(observed)["deadlock_states"] > 0) is actual_blocking


def test_r8_thread_history_projection_bfs_never_resurrects_terminal(tmp_path):
    """穷举三步回调交错，实际TaskStore的活动投影必须服从当前终态。"""
    from collections import deque

    from agent_py_agent.tests.test_r8_record_ledgers import _thread

    pending = deque([()])
    number = 0
    while pending:
        actions = pending.popleft()
        store, thread = _thread(tmp_path / str(number))
        number += 1
        store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'task'})
        terminal = False
        for action in actions:
            if action == 'finish':
                store.tasks.update_status({'task_id': 'task', 'status': 'done'})
                terminal = True
            elif action == 'late_bind':
                store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'task'})
            else:
                store.tasks.update_thread_index(thread.thread_id, 'task', 'active', 1)
            current = store.threads.require(thread.thread_id)
            assert store.tasks.load('task').status == ('done' if terminal else 'active')
            assert ('task' in current.active_task_ids) is not terminal
            assert tuple(store.tasks.iter_task_ids(thread.thread_id)) == ('task',)
        if len(actions) < 3:
            pending.extend((*actions, action) for action in ('finish', 'late_bind', 'late_projection'))


def test_workflow_unknown_effect_retry_finite_state_space():
    """穷举真实重试器的有限事实域；未知效果不因定义自报幂等而产生第二次执行。"""
    from types import SimpleNamespace

    from agent_py_agent.agent.workflows.step_retry import run_step_with_retry

    pending = deque((started, outcome, declared) for started in (False, True)
                    for outcome in ('unknown', 'not_started', 'running', None)
                    for declared in (False, True))
    visited = set()
    while pending:
        started, outcome, declared = pending.popleft()
        key = (started, outcome, declared)
        assert key not in visited
        visited.add(key)
        step = SimpleNamespace(step_id='effect', mode='tool_call', status='running', error_code='',
            error_message='', needs_repair=False,
            escalation_result={'effect_started': started, 'effect_outcome': outcome})
        calls = []

        def execute():
            calls.append(len(calls))
            step.status, step.error_code = 'failed', 'NETWORK_TIMEOUT'

        run_step_with_retry(engine=None, step=step, spec={'retry_policy': {
            'max_attempts': 2, 'idempotent': declared}}, outputs={}, definition={}, run=None,
            execute_fn=execute)
        blocked = outcome == 'unknown' or started and outcome != 'not_started'
        assert len(calls) == (1 if blocked else 2)
    assert len(visited) == 16


def test_workflow_declared_review_missing_host_blocks_all_execution_phases(tmp_path):
    """枚举原控制/步骤边界：缺审核服务只能取消，不能借恢复或DAG步骤执行。"""
    from agent_py_agent.agent.workflows.business_scope import WorkflowPolicyRefusal
    from agent_py_agent.agent.workflows.engine import WorkflowEngine
    from agent_py_agent.agent.workflows.governance import WorkflowEffectPhase
    from agent_py_agent.agent.workflows.models import WorkflowRunRecord

    engine = WorkflowEngine(tmp_path)
    for declared in (False, True):
        for phase in ('preflight', 'resume', WorkflowEffectPhase('effect'), 'cancel'):
            run = WorkflowRunRecord('qa-review', 'qa-flow', '', 'manual', 'running', 1, 0, 0)
            definition = {'descriptor': {'metadata': {'workflow_review_policy': {'steps': ['effect']}} if declared else {}}}
            try:
                engine._authorize_run(run, phase, definition=definition)
            except WorkflowPolicyRefusal as error:
                assert declared and phase != 'cancel'
                assert error.error_code == 'WORKFLOW_REVIEW_SERVICE_UNAVAILABLE'
            else:
                assert not declared or phase == 'cancel'
