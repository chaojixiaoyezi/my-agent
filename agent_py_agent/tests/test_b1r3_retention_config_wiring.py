# LLM: Only isolated pytest homes; real readers and retention commits, producer doubles do not claim gateway transport coverage.
# 模块用途: 验证四个留存参数从生产入口透传，并保持默认留存报告不变。
from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.memory_store import retention, retention_apply, retention_scan_budget
from agent_py_agent.agent.memory_store.reference_pins import ReferencePinSnapshot
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionReport
from agent_py_agent.agent.user_space import owner_maintenance, owner_maintenance_transition
from agent_py_agent.tests.test_issues_w7_maintenance_controller import (
    _controller,
)
from agent_py_agent.tests.test_issues_w7_maintenance_controller import (
    maintenance_clock as maintenance_clock,
)
from agent_py_agent.tests.test_memory_retention_phase2 import NOW, old_file, runtime
from agent_py_agent.tests.test_owner_maintenance import _home_with_old_cache


# LLM: Reuse the task's exact configured values, without introducing production constants.
# 函数用途: 提供四个已注册参数的非默认配置。
@pytest.fixture
def cfg():
    return SimpleNamespace(retention_scan_slice_entries=1, retention_scan_slice_bytes=8388608,
                           retention_scan_slice_ms=25, memory_reference_pin_chunk_bytes=4096)


# LLM: Both spies delegate to real readers; only the budget's own clock is deterministic.
# 函数用途: 记录配置身份及实际读取结果，不替换保留执行器。
@pytest.fixture
def readers(monkeypatch):
    seen = SimpleNamespace(budgets=[], snapshots=[])
    original_budget = retention_scan_budget.RetentionScanBudget.default
    original_snapshot = ReferencePinSnapshot.__init__
    monkeypatch.setattr(retention_scan_budget, "time", SimpleNamespace(monotonic=lambda: 100.0))

    # LLM: Preserve classmethod dispatch and all reader validation.
    # 函数用途: 保存真实预算对象与收到的原配置。
    def budget(cls, config=None):
        value = original_budget(config)
        seen.budgets.append((config, value))
        return value

    # LLM: Keep the actual snapshot constructor and per-instance chunk cache.
    # 函数用途: 保存真实引用快照及配置身份。
    def snapshot(self, home, roots, now, builder, *, config=None):
        original_snapshot(self, home, roots, now, builder, config=config)
        seen.snapshots.append((config, self))

    monkeypatch.setattr(retention_scan_budget.RetentionScanBudget, "default", classmethod(budget))
    monkeypatch.setattr(ReferencePinSnapshot, "__init__", snapshot)
    return seen


# LLM: Fail explicitly on missing API plumbing before attempting the new keyword.
# 函数用途: 检查新增参数为可选且仅限关键字。
def _config_contract(operation):
    parameters = inspect.signature(operation).parameters
    assert "config" in parameters, f"config plumbing missing: {operation.__qualname__}"
    assert parameters["config"].default is None
    assert parameters["config"].kind is inspect.Parameter.KEYWORD_ONLY


# LLM: Verify all four knobs reached actual reader results, not just mock call presence.
# 函数用途: 核对预算三维值、分块大小及同一配置对象。
def _assert_readers(seen, config):
    assert len(seen.budgets) == len(seen.snapshots) == 1
    received, budget = seen.budgets[0]
    assert received is config
    assert (budget.max_entries, budget.max_bytes) == (1, 8388608)
    assert budget.deadline == pytest.approx(100.025)
    assert seen.snapshots[0][0] is config
    assert seen.snapshots[0][1].contents["chunk_bytes"] == 4096


# LLM: Every changed public seam must preserve the old positional parameter surface.
# 函数用途: 锁定五处新增可选关键字参数的合同。
@pytest.mark.parametrize("operation", [retention.MemoryRetentionService.__init__,
    retention.apply_owner_retention, retention_apply.execute_retention_plan,
    owner_maintenance.run_owner_retention_if_due, owner_maintenance_transition.run_with_transition])
def test_config_parameters_are_optional_keyword_only(operation):
    _config_contract(operation)


# LLM: Exercise real planning, cursor persistence and executor for ordinary and short-transition paths.
# 函数用途: 验证保留服务的两个预算创建点都读取原配置。
@pytest.mark.parametrize("transition", [False, True])
def test_apply_owner_retention_reaches_both_readers(tmp_path, cfg, readers, transition):
    _config_contract(retention.apply_owner_retention)
    home, _, _ = runtime(tmp_path)
    options = {"commit_transition": lambda operation: operation()} if transition else {}
    result = retention.apply_owner_retention(home, now=NOW, scan_budget=True, config=cfg, **options)
    assert result.applied and not result.errors
    _assert_readers(readers, cfg)
    assert (home.owner_data_dir / "retention_scan_cursor.json").is_file()


# LLM: An empty executable plan must still construct the real configured pin snapshot.
# 函数用途: 验证执行器不丢失引用扫描分块参数。
def test_executor_passes_config_to_reference_snapshot(tmp_path, cfg, readers):
    _config_contract(retention_apply.execute_retention_plan)
    home, candidates, _ = runtime(tmp_path)
    result = retention_apply.execute_retention_plan(home=home, candidates=candidates,
        plan=MemoryRetentionReport(applied=False, actions=()), now=NOW, config=cfg)
    assert result.applied and result.actions == result.errors == ()
    assert readers.budgets == [] and len(readers.snapshots) == 1
    assert readers.snapshots[0][0] is cfg
    assert readers.snapshots[0][1].contents["chunk_bytes"] == 4096


# LLM: Use real owner maintenance, real due state and original transition/CAS machinery.
# 函数用途: 验证到期维护的普通和短提交链都把配置送到两个读取器。
@pytest.mark.parametrize("transition", [False, True])
def test_due_maintenance_keeps_config_through_transition(tmp_path, cfg, readers, transition):
    _config_contract(owner_maintenance.run_owner_retention_if_due)
    home, _ = _home_with_old_cache(tmp_path)
    options = {"include_global_indexes": False}
    if transition:
        options["commit_transition"] = lambda operation: operation()
    result = owner_maintenance.run_owner_retention_if_due(home, now=200_000, config=cfg, **options)
    assert result.ran and result.retention.applied
    _assert_readers(readers, cfg)
    marker = json.loads((home.owner_data_dir / "maintenance.json").read_text())
    assert marker["last_attempt_at"] == 200_000


# LLM: Drive the existing controller fixture, replacing only maintenance work with a typed recorder.
# 函数用途: 验证基础owner与发现的owner均使用基础Agent配置。
def test_gateway_tick_passes_base_config_to_both_calls(tmp_path, monkeypatch, maintenance_clock, cfg):
    from agent_py_agent.cli import gateway_loops

    controller, home, owners, _ = _controller(tmp_path, monkeypatch, maintenance_clock, 1)
    controller._base_agent.config = cfg
    calls = []

    # LLM: A config-aware producer double; it does not modify any existing test double.
    # 函数用途: 记录生产控制器实际传入的配置与目录。
    def record(scoped_home, *, now, config=None):
        calls.append((scoped_home.owner_home_dir, now, config))
        return owner_maintenance.OwnerMaintenanceResult(True, "success")

    monkeypatch.setattr(gateway_loops, "run_owner_retention_if_due", record)
    result = controller.tick(now=200_000)
    assert result["ran"] == 2
    assert calls == [(home.owner_home_dir, 200_000, cfg), (owners[0].home_dir, 200_000, cfg)]


# LLM: Check only the producer boundary; no claim/P3/P5 or authorization bypass is simulated as E2E.
# 函数用途: 验证多进程维护入口保留原来源指纹和短提交回调并透传配置。
def test_multiprocess_maintenance_producer_passes_config(monkeypatch, cfg):
    from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge

    agent = SimpleNamespace(home_paths=object(), config=cfg)
    calls, commits = [], []

    # LLM: Retain every supplied producer keyword for exact boundary assertions.
    # 函数用途: 保存入口调用并返回原维护结果类型。
    def record(home, **kwargs):
        calls.append((home, kwargs))
        return owner_maintenance.OwnerMaintenanceResult(True, "success")

    # LLM: Execute the callback only in this local producer unit test.
    # 函数用途: 核对原短提交标签和操作对象没有被改写。
    def commit(phase, operation):
        commits.append(phase)
        return operation()

    monkeypatch.setattr(owner_maintenance, "run_owner_retention_if_due", record)
    monkeypatch.setattr(bridge, "background_short_operation", commit)
    result = bridge._execute_maintenance(agent, {"source_id": "maintenance", "input_revision": "policy",
        "source_sha256": "state", "source_exists": True})
    assert result["ran"] and result["outcome"] == "handled"
    home, options = calls[0]
    assert home is agent.home_paths and options.get("config") is cfg
    assert options["include_global_indexes"] is False
    assert options["expected_policy_fingerprint"] == "policy"
    assert options["expected_state_fingerprint"] == "state"
    assert options["commit_transition"](lambda: "committed") == "committed"
    assert commits == ["owner_maintenance_commit"]


# LLM: Keep real CLI admission and due checks; observe only the maintenance call boundary.
# 函数用途: 验证本地主聊天收尾入口不会丢掉Agent配置。
def test_cli_completion_producer_passes_agent_config(tmp_path, monkeypatch, cfg):
    home, _ = _home_with_old_cache(tmp_path)
    agent = SimpleNamespace(home_paths=home, config=cfg)
    calls = []

    # LLM: Do not replace the wrapper's admission or owner checks.
    # 函数用途: 记录已经通过CLI准入的维护调用。
    def record(received, **kwargs):
        calls.append((received, kwargs))
        return owner_maintenance.OwnerMaintenanceResult(True, "success")

    monkeypatch.setattr(owner_maintenance, "run_owner_retention_if_due", record)
    assert owner_maintenance.maybe_run_cli_owner_maintenance(agent, now=200_000).ran
    assert calls[0][0] is home and calls[0][1].get("config") is cfg


# LLM: Apply receives config; the existing dry-run planning call must remain config-free.
# 函数用途: 验证显式清理命令仅在apply分支传入配置。
@pytest.mark.parametrize("apply", [False, True])
def test_home_retention_command_only_configures_apply(tmp_path, monkeypatch, capsys, cfg, apply):
    from agent_py_agent.cli import home_runtime_commands as commands

    home, _, _ = runtime(tmp_path)
    calls = []
    monkeypatch.setattr(commands, "make_agent", lambda _args: SimpleNamespace(home_paths=home, config=cfg))

    # LLM: Record both possible command targets without running cleanup from a producer unit test.
    # 函数用途: 保存命令选中的分支与关键字。
    def record(received, **kwargs):
        calls.append((received, kwargs))
        return MemoryRetentionReport(applied=apply, actions=())

    monkeypatch.setattr(commands, "apply_owner_retention", record)
    monkeypatch.setattr(commands, "plan_owner_retention", record)
    assert commands.cmd_home_retention(SimpleNamespace(apply=apply, json=True)) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert calls == [(home, {"config": cfg} if apply else {})]


# LLM: Read defaults from the original readers; do not duplicate or edit production constants.
# 函数用途: 锁定无配置时原有预算和引用分块默认值。
def test_none_reader_defaults_are_unchanged(tmp_path):
    home, _, _ = runtime(tmp_path)
    budget = retention_scan_budget.RetentionScanBudget.default(None)
    assert (budget.max_entries, budget.max_bytes) == (128, 8388608)
    snapshot = ReferencePinSnapshot(home, (), NOW, lambda *_args: None, config=None)
    assert snapshot.contents["chunk_bytes"] == 65536


# LLM: 省略config与显式None都必须让两个读取器拿到None并使用原默认值；不依赖git历史或scan_budget默认值。
# 函数用途: 证明默认配置走原读取默认值，并删除同一个过期文件。
@pytest.mark.parametrize("explicit_none", [False, True])
def test_default_config_reaches_readers_as_none(tmp_path, readers, explicit_none):
    home, _, _ = runtime(tmp_path)
    path = old_file(home.owner_cache_dir / "default-parity.tmp", "expired bytes")
    options = {"config": None} if explicit_none else {}
    report = retention.apply_owner_retention(home, now=NOW, **options)
    assert report.applied and report.actions and not path.exists()
    assert readers.snapshots and all(config is None and snapshot.contents["chunk_bytes"] == 65536
                                     for config, snapshot in readers.snapshots)
    assert all(config is None and (budget.max_entries, budget.max_bytes) == (128, 8388608)
               for config, budget in readers.budgets)
