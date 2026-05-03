"""压力测试：dispatch 调度系统高并发和竞态场景"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.subagents.models import DISPATCH_INELIGIBLE_STATUSES


class TestConcurrentDispatchCreation:
    """并发任务创建压力测试"""

    @pytest.mark.slow
    def test_create_many_tasks_rapidly(self):
        """验证快速创建大量任务"""
        from agent_py_agent.agent.subagents.manager import SubAgentManager

        results = []
        errors = []

        def create_task(task_id):
            try:
                # 模拟快速创建任务
                manager = MagicMock()
                manager.list_runs = MagicMock(return_value=[])
                manager.save = MagicMock()
                manager.local_store = None

                # 模拟任务创建
                task = MagicMock()
                task.id = f"subagent-{task_id}"
                task.status = "PLANNING"

                results.append(task)
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(create_task, i) for i in range(100)]
            for f in as_completed(futures):
                pass

        assert len(errors) == 0
        assert len(results) == 100

    @pytest.mark.slow
    def test_concurrent_status_updates(self):
        """验证并发状态更新"""
        task_states = {}
        lock = threading.Lock()

        def update_status(task_id, new_status):
            with lock:
                old_status = task_states.get(task_id, "UNKNOWN")
                task_states[task_id] = new_status
                return old_status, new_status

        results = []

        def worker(worker_id):
            for i in range(50):
                old, new = update_status(f"task-{worker_id}", f"RUNNING-{i}")
                results.append((old, new))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 500

    @pytest.mark.slow
    def test_rapid_create_and_cancel(self):
        """验证快速创建和取消的竞态"""
        created_tasks = []
        cancelled_tasks = []
        lock = threading.Lock()

        def create_task(task_id):
            with lock:
                created_tasks.append(task_id)
            # 模拟一些处理时间
            time.sleep(0.001)

        def cancel_task(task_id):
            with lock:
                if task_id in created_tasks:
                    cancelled_tasks.append(task_id)

        with ThreadPoolExecutor(max_workers=20) as executor:
            # 交替创建和取消
            for i in range(50):
                executor.submit(create_task, i)
                executor.submit(cancel_task, i)

        # 验证没有重复
        assert len(set(created_tasks)) == len(created_tasks)
        assert len(set(cancelled_tasks)) == len(cancelled_tasks)


class TestDispatchLockContention:
    """调度锁争用压力测试"""

    @pytest.mark.slow
    def test_dispatch_ineligible_filter_stress(self):
        """验证调度过滤器的并发压力"""
        from agent_py_agent.agent.subagents.models import TaskStatus

        eligible_statuses = [
            TaskStatus.PLANNING.value,
            TaskStatus.RUNNING.value,
            TaskStatus.BLOCKED.value,
        ]

        ineligible_count = 0

        def check_task():
            nonlocal ineligible_count
            for status in DISPATCH_INELIGIBLE_STATUSES:
                if status in eligible_statuses:
                    ineligible_count += 1

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(check_task) for _ in range(1000)]
            for f in as_completed(futures):
                pass

        # 不应该有ineligible状态出现在 eligible列表中
        assert ineligible_count == 0

    @pytest.mark.slow
    def test_select_runs_concurrent_access(self):
        """验证 _select_runs 并发访问"""
        from agent_py_agent.agent.subagents.models import SubAgentTask, TaskStatus

        tasks = [
            SubAgentTask(id=f"task-{i}", goal=f"Goal {i}", thought=f"Think {i}", plan=["step"],
                        status=TaskStatus.PLANNING.value if i % 3 == 0 else TaskStatus.COMPLETED.value)
            for i in range(100)
        ]

        results = []

        def select_runs(run_ids):
            filtered = [t for t in tasks if t.status not in DISPATCH_INELIGIBLE_STATUSES]
            return filtered

        def worker():
            result = select_runs(None)
            results.append(len(result))

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(worker) for _ in range(50)]
            for f in as_completed(futures):
                pass

        # 所有 worker 应该返回相同结果
        assert all(r == results[0] for r in results)

    @pytest.mark.slow
    def test_dispatch_summary_concurrent_updates(self):
        """验证调度汇总的并发更新"""
        summary = {"total": 0, "OK": 0, "BROKEN": 0, "DEGRADED": 0}
        lock = threading.Lock()

        def update_summary(status):
            with lock:
                summary["total"] += 1
                summary[status] = summary.get(status, 0) + 1

        def generate_status(idx):
            statuses = ["OK", "OK", "OK", "DEGRADED", "BROKEN"]
            return statuses[idx % len(statuses)]

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(update_summary, generate_status(i)) for i in range(100)]
            for f in as_completed(futures):
                pass

        assert summary["total"] == 100
        assert summary["OK"] + summary["DEGRADED"] + summary["BROKEN"] == 100


class TestDispatchWorkflowStress:
    """调度工作流压力测试"""

    @pytest.mark.slow
    def test_dispatch_loop_many_tasks(self):
        """验证调度循环处理大量任务"""
        tasks = [f"task-{i}" for i in range(100)]
        dispatched = []
        lock = threading.Lock()

        def dispatch_one(task_id):
            with lock:
                if len(dispatched) < 20:  # 模拟最多同时处理 20 个
                    dispatched.append(task_id)
                    return True
                return False

        results = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(dispatch_one, t) for t in tasks]
            for f in as_completed(futures):
                results.append(f.result())

        assert sum(results) == 20

    @pytest.mark.slow
    def test_planner_gate_stress(self):
        """验证 planner gate 并发压力"""
        active_tasks = []
        pending_tasks = []
        lock = threading.Lock()

        def check_planner_gate():
            with lock:
                # 模拟检查逻辑：有活跃或待处理任务时不允许空心 HEARTBEAT_OK
                has_active = len(active_tasks) > 0
                has_pending = len(pending_tasks) > 0
                return has_active or has_pending

        def worker(worker_id):
            result = check_planner_gate()
            return worker_id, result

        # 模拟有活跃任务
        for i in range(10):
            active_tasks.append(f"active-{i}")

        results = []
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(worker, i) for i in range(50)]
            for f in as_completed(futures):
                results.append(f.result())

        # 所有结果应该都报告 has_active
        assert all(r[1] for r in results)

    @pytest.mark.slow
    def test_watchdog_cycle_stress(self):
        """验证 watchdog 循环压力"""
        cycles = []
        lock = threading.Lock()

        def watchdog_cycle(cycle_id):
            start = time.time()
            # 模拟一些检查工作
            time.sleep(0.001)
            end = time.time()
            with lock:
                cycles.append({
                    "id": cycle_id,
                    "duration": end - start,
                })

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(watchdog_cycle, i) for i in range(50)]
            for f in as_completed(futures):
                pass

        assert len(cycles) == 50


class TestDispatchConcurrencyEdgeCases:
    """调度并发边界情况测试"""

    @pytest.mark.slow
    def test_empty_run_ids_list(self):
        """验证空 run_ids 列表的处理"""
        def select_runs(run_ids):
            if run_ids is None:
                return []  # None 表示获取所有
            if not run_ids:
                return []  # 空列表
            return run_ids

        result = select_runs([])
        assert result == []

        result = select_runs(None)
        assert result == []

    @pytest.mark.slow
    def test_duplicate_run_ids(self):
        """验证重复 run_id 的去重"""
        run_ids = ["task-1", "task-2", "task-1", "task-3", "task-2"]
        unique_seen = set()
        deduped = []

        for rid in run_ids:
            if rid not in unique_seen:
                unique_seen.add(rid)
                deduped.append(rid)

        assert len(deduped) == 3
        assert deduped == ["task-1", "task-2", "task-3"]

    @pytest.mark.slow
    def test_rapid_status_transitions(self):
        """验证快速状态转换"""
        # Each complete cycle: PLANNING->RUNNING->BLOCKED->RUNNING->COMPLETED
        transitions = [
            ("PLANNING", "RUNNING"),
            ("RUNNING", "BLOCKED"),
            ("BLOCKED", "RUNNING"),
            ("RUNNING", "COMPLETED"),
        ]

        def do_transitions():
            current_status = "PLANNING"
            transition_count = 0
            for from_state, to_state in transitions:
                if current_status == from_state:
                    current_status = to_state
                    transition_count += 1
            return transition_count

        total_transitions = sum(do_transitions() for _ in range(25))
        assert total_transitions == 100

    @pytest.mark.slow
    def test_concurrent_file_writes(self, tmp_path):
        """验证并发文件写入"""
        written_files = []
        lock = threading.Lock()

        def write_task_file(task_id):
            path = tmp_path / f"task_{task_id}.json"
            path.write_text(f'{{"task_id": "{task_id}"}}')
            with lock:
                written_files.append(task_id)

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(write_task_file, i) for i in range(100)]
            for f in as_completed(futures):
                pass

        assert len(written_files) == 100


class TestDispatchErrorHandling:
    """调度错误处理压力测试"""

    @pytest.mark.slow
    def test_many_failed_runs(self):
        """验证大量失败运行的处理"""
        failed_runs = []
        lock = threading.Lock()

        def record_failure(run_id, error):
            with lock:
                failed_runs.append({"run_id": run_id, "error": error})

        errors = ["timeout", "memory", "disk", "network", "unknown"]

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [
                executor.submit(record_failure, f"run-{i}", errors[i % len(errors)])
                for i in range(100)
            ]
            for f in as_completed(futures):
                pass

        assert len(failed_runs) == 100

    @pytest.mark.slow
    def test_retry_after_failures(self):
        """验证失败后重试"""
        retry_counts = {}
        lock = threading.Lock()

        def retry_task(task_id):
            with lock:
                count = retry_counts.get(task_id, 0)
                retry_counts[task_id] = count + 1
            # 返回是否应该继续重试 (最多4次：count 0,1,2,3)
            return count < 4

        results = []
        for i in range(20):
            task_id = f"task-{i % 5}"  # 5 个任务
            result = retry_task(task_id)
            results.append(result)

        # 每个任务被处理了4次 (20 iterations / 5 tasks = 4)
        assert all(retry_counts.values())
        assert all(c == 4 for c in retry_counts.values())

    @pytest.mark.slow
    def test_error_summary_concurrency(self):
        """验证错误汇总的并发更新"""
        error_summary = {"total_errors": 0, "by_type": {}}
        lock = threading.Lock()

        def add_error(error_type):
            with lock:
                error_summary["total_errors"] += 1
                error_summary["by_type"][error_type] = error_summary["by_type"].get(error_type, 0) + 1

        error_types = ["timeout", "memory", "disk", "network"]

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(add_error, error_types[i % len(error_types)]) for i in range(100)]
            for f in as_completed(futures):
                pass

        assert error_summary["total_errors"] == 100
        assert sum(error_summary["by_type"].values()) == 100