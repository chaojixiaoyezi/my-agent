"""PARAM-W3：在隔离输入上验证消费者参数、默认兼容和准入边界。"""
from __future__ import annotations

import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.failure_analysis_service import SubAgentFailureAnalyzer
from agent_py_agent.agent.agent_core.orchestration.dispatch.runner_batches import (
    _limited_runner_jobs,
    _runner_role_limits,
)
from agent_py_agent.agent.ingestion.config import IngestTuning, tuning_from_params
from agent_py_agent.agent.ingestion.harvest_execution import harvester_has_due_work
from agent_py_agent.agent.subagents.models import SubAgentRunnerResult, SubAgentTask
from agent_py_agent.agent.user_space import owner_quota as quota


def _analysis(analyzer, attempts, failure_type="model_error"):
    task = SubAgentTask(id="param-w3", goal="验证重试边界", thought="隔离验证", plan=[],
                        runner_attempts=attempts, failure_type=failure_type)
    result = SubAgentRunnerResult(run_id=task.id, dry_run=False, ok=False,
                                  status="BLOCKED", verification_status="UNVERIFIED", message="测试失败")
    return analyzer.analyze(task, result)


def test_retry_without_config_preserves_three_total_attempts():
    analyzer = SubAgentFailureAnalyzer()
    assert _analysis(analyzer, 2).should_retry is True
    assert _analysis(analyzer, 3).should_retry is False
    assert _analysis(SubAgentFailureAnalyzer(600, 4), 3).should_retry is True
    assert _analysis(SubAgentFailureAnalyzer(max_retry_attempts=2), 2).should_retry is False


def test_retry_config_converts_retries_to_total_attempts():
    for retries in (0, 1, 4):
        analyzer = SubAgentFailureAnalyzer(config=SimpleNamespace(runner_failure_retry_limit=retries))
        if retries:
            assert _analysis(analyzer, retries).should_retry is True
        assert _analysis(analyzer, retries + 1).should_retry is False


def test_retry_explicit_overrides_preserve_legacy_precedence():
    config = SimpleNamespace(runner_failure_retry_limit=6)
    analyzer = SubAgentFailureAnalyzer(retry_limit=0, config=config)
    assert _analysis(analyzer, 1).should_retry is False
    legacy = SubAgentFailureAnalyzer(max_retry_attempts=4, retry_limit=0, config=config)
    assert _analysis(legacy, 3).should_retry is True
    assert _analysis(legacy, 4).should_retry is False


def test_retry_config_uses_existing_normalization_and_rejects_bad_direct_limit():
    for invalid in (True, -1, "bad", None):
        analyzer = SubAgentFailureAnalyzer(config=SimpleNamespace(runner_failure_retry_limit=invalid))
        assert _analysis(analyzer, 1).should_retry is True
        assert _analysis(analyzer, 2).should_retry is False
    assert _analysis(SubAgentFailureAnalyzer(config=SimpleNamespace()), 2).should_retry is False
    for invalid in (True, -1, 1.5, "2"):
        with pytest.raises(ValueError):
            SubAgentFailureAnalyzer(retry_limit=invalid)


def test_retry_budget_applies_to_all_retrying_failure_types():
    analyzer = SubAgentFailureAnalyzer(retry_limit=0)
    for failure_type in ("runner_timeout", "structured_output_parse_error", "tool_result_missing",
                         "tool_error", "model_error", "api_error", "unclassified"):
        result = _analysis(analyzer, 1, failure_type)
        assert result.should_retry is False, failure_type
    timeout = _analysis(analyzer, 1, "runner_timeout")
    assert timeout.should_split is True
    assert timeout.new_timeout_seconds is None
    capability = _analysis(analyzer, 1, "capability_request")
    assert capability.suggested_action == "manual_capability_grant"
    assert capability.should_retry is False


def test_role_limits_discard_non_integer_values_without_mutating_input():
    raw = {"worker": 0, "reviewer": 2, "bool": True, "false": False, "negative": -1,
           "fraction": 2.5, "string": "2", "infinite": float("inf"), "none": None}
    before = dict(raw)
    assert _runner_role_limits(SimpleNamespace(runner_role_limits=raw)) == {"worker": 0, "reviewer": 2}
    assert raw == before
    for invalid in (None, True, 4, [], "worker: 1"):
        assert _runner_role_limits(SimpleNamespace(runner_role_limits=invalid)) == {}
    assert _runner_role_limits(SimpleNamespace()) == {}


def test_role_limits_zero_blocks_role_per_batch_but_not_other_roles():
    config = SimpleNamespace(runner_timeout_seconds="off", runner_concurrency="auto", runner_start_rate="0",
                             runner_role_limits={"worker": 0, "reviewer": 1})
    jobs = [(name, SimpleNamespace(role=role), "") for name, role in (
        ("a", "worker"), ("b", "reviewer"), ("c", "reviewer"), ("d", "other"))]
    agent = SimpleNamespace(config=config)
    assert [job[0] for job in _limited_runner_jobs(agent, jobs)] == ["b", "d"]
    assert [job[0] for job in _limited_runner_jobs(agent, jobs)] == ["b", "d"]
    assert [job[0] for job in jobs] == ["a", "b", "c", "d"]


def test_poll_interval_loads_positive_floats_and_plain_projections():
    for value, expected in ((0.125, 0.125), (3, 3.0), (" 2.75 ", 2.75)):
        tuning = tuning_from_params({"poll_interval_seconds": value})
        assert tuning.poll_interval_seconds == expected
        assert type(tuning.poll_interval_seconds) is float
        assert asdict(tuning)["poll_interval_seconds"] == expected
        assert tuning_from_params(asdict(tuning)).poll_interval_seconds == expected


def test_poll_interval_invalid_values_use_existing_default():
    for value in (0, -1.0, True, False, None, "bad", "nan", "inf", float("nan"), float("inf"), [], {}):
        assert tuning_from_params({"poll_interval_seconds": value}).poll_interval_seconds == 1.5
    assert tuning_from_params({}).poll_interval_seconds == IngestTuning().poll_interval_seconds == 1.5
    tuning = tuning_from_params({"page_limit": "9999", "poll_interval_seconds": 0.25})
    assert tuning.page_limit == 500
    assert tuning.poll_interval_seconds == 0.25


def _watch_payload(**overrides):
    return {"closed": False, "window_finalized_at": 0, "audit_guarantee": True,
            "tuning": {}, "watch_window_seconds": 0, "opened_at": 1, "saved_at": 100,
            "source_checkpoint": {}, **overrides}


def test_harvest_discovery_consumes_configured_interval_exactly():
    payload = _watch_payload(tuning={"poll_interval_seconds": 4.25})
    assert harvester_has_due_work(payload, 101.5) is False
    assert harvester_has_due_work(payload, 104.249) is False
    assert harvester_has_due_work(payload, 104.25) is True
    payload["tuning"]["poll_interval_seconds"] = 0.25
    assert harvester_has_due_work(payload, 100.249) is False
    assert harvester_has_due_work(payload, 100.25) is True
    assert harvester_has_due_work(_watch_payload(), 101.499) is False
    assert harvester_has_due_work(_watch_payload(), 101.5) is True


def test_harvest_invalid_interval_uses_same_default_as_loader():
    for value in (True, 0, -1, "nan", "bad", float("inf")):
        payload = _watch_payload(tuning={"poll_interval_seconds": value})
        assert harvester_has_due_work(payload, 101.499) is False
        assert harvester_has_due_work(payload, 101.5) is True


def test_harvest_deadline_and_durable_backoff_precedence_unchanged():
    payload = _watch_payload(tuning={"poll_interval_seconds": 50}, watch_window_seconds=100,
                             source_checkpoint={"_host_harvest": {"next_due_at": 200}})
    assert harvester_has_due_work(payload, 101) is True  # 首次截止先冻结。
    payload["source_checkpoint"]["_host_harvest"]["boundary"] = {"deadline": 101}
    assert harvester_has_due_work(payload, 101) is False
    assert harvester_has_due_work(payload, 200) is True
    payload["closed"] = True
    assert harvester_has_due_work(payload, 300) is False
    payload["closed"] = False
    payload["window_finalized_at"] = 201
    assert harvester_has_due_work(payload, 300) is False
    assert harvester_has_due_work(_watch_payload(audit_guarantee=False, tuning={"background_harvest": 0}), 300) is False


def _reconcile(root):
    for _ in range(16):
        result = quota.reconcile_owner_quota_slice(root, max_entries=1024, max_ms=1000)
        if result["complete"]:
            return
    pytest.fail("隔离配额对账未完成")


def _write(enforcer, path, size):
    with enforcer.reserve([quota.OwnerQuotaChange(path, size)]):
        with path.open("wb") as handle:
            handle.truncate(size)


def test_quota_pressure_uses_joint_config_watermarks_without_writes(tmp_path):
    maximum = 100 * 1024 * 1024
    (tmp_path / "quota.json").write_text(json.dumps({"max_disk_mb": 100}))
    _reconcile(tmp_path)
    config = SimpleNamespace(owner_quota_pressure_high_percent=60, owner_quota_pressure_critical_percent=75)
    for used, expected in ((60 * maximum // 100 - 1, "normal"), (60 * maximum // 100, "high"),
                           (75 * maximum // 100, "critical")):
        ledger = quota._read_quota_ledger(tmp_path)
        ledger["used_bytes"] = used
        quota._write_quota_ledger(tmp_path, ledger)
        before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
        assert quota.owner_quota_pressure(tmp_path, config=config)["level"] == expected
        assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
    assert quota.owner_quota_pressure(tmp_path)["level"] == "normal"


def test_quota_watermarks_joint_validation_is_strict(tmp_path):
    for high, critical in ((85, 85), (96, 95), (-1, 95), (85, 101), (True, 95), (85, 95.0), ("85", 95)):
        config = SimpleNamespace(owner_quota_pressure_high_percent=high, owner_quota_pressure_critical_percent=critical)
        with pytest.raises(ValueError):
            quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1000, config=config)
        with pytest.raises(ValueError):
            quota.owner_quota_pressure(tmp_path, config=config)
    for high, critical in ((0, 1), (99, 100)):
        quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1000, config=SimpleNamespace(
            owner_quota_pressure_high_percent=high, owner_quota_pressure_critical_percent=critical))
    assert list(tmp_path.iterdir()) == []


def test_quota_permanent_tuning_strict_bounds_and_no_artificial_byte_cap(tmp_path):
    for field, invalid in (("owner_quota_permanent_headroom_percent", True),
                           ("owner_quota_permanent_headroom_percent", 101),
                           ("owner_quota_permanent_pending_growth_bytes", -1),
                           ("owner_quota_permanent_pending_growth_bytes", 1.5)):
        with pytest.raises(ValueError):
            quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1000, config=SimpleNamespace(**{field: invalid}))
    quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1000, config=SimpleNamespace(
        owner_quota_permanent_headroom_percent=100, owner_quota_permanent_pending_growth_bytes=2**40))


def test_quota_configured_headroom_real_admission_boundary(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "filler").write_bytes(b"x" * 1000)
    enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1000, config=SimpleNamespace(
        owner_quota_permanent_headroom_percent=10))
    target = tmp_path / "memory" / "permanent.md"
    _write(enforcer, target, 100)
    assert quota.owner_logical_usage_bytes(tmp_path, exact=True) == 1100
    assert quota._read_quota_ledger(tmp_path)["reservations"] == {}
    with pytest.raises(quota.OwnerQuotaExceeded):
        _write(enforcer, target, 101)
    with pytest.raises(quota.OwnerQuotaExceeded):
        _write(enforcer, tmp_path / "ordinary", 1)
    with pytest.raises(quota.OwnerQuotaExceeded):
        with enforcer.reserve([quota.OwnerQuotaChange(target, 101), quota.OwnerQuotaChange(tmp_path / "mixed", 0)]):
            pytest.fail("混合写不应获得永久资产余量")
    assert target.stat().st_size == 100
    assert not (tmp_path / "ordinary").exists()


def test_quota_default_and_zero_headroom_are_preserved(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "filler").write_bytes(b"x" * 1000)
    enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1000)
    target = tmp_path / "memory" / "default.md"
    _write(enforcer, target, 50)
    with pytest.raises(quota.OwnerQuotaExceeded):
        _write(enforcer, target, 51)
    no_headroom = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1000, config=SimpleNamespace(
        owner_quota_permanent_headroom_percent=0))
    with pytest.raises(quota.OwnerQuotaExceeded):
        _write(no_headroom, target, 51)
    _write(no_headroom, target, 40)  # 已超额时非增长修复仍允许。
    assert target.stat().st_size == 40


def test_quota_pending_growth_config_is_cumulative_across_instances(tmp_path):
    (tmp_path / "memory").mkdir()
    for index in range(140):
        (tmp_path / f"item-{index}").write_bytes(b"x")
    _reconcile(tmp_path)
    ledger = quota._read_quota_ledger(tmp_path)
    ledger["dirty_since"] = 1
    quota._write_quota_ledger(tmp_path, ledger)
    config = SimpleNamespace(owner_quota_permanent_headroom_percent=10,
                             owner_quota_permanent_pending_growth_bytes=12)
    for name, size in (("first", 5), ("second", 7)):
        _write(quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1000, config=config), tmp_path / "memory" / name, size)
    assert quota._read_quota_ledger(tmp_path)["permanent_pending_growth_bytes"] == 12
    with pytest.raises(quota.OwnerQuotaUnavailable, match="reconciliation pending"):
        _write(quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1000, config=config), tmp_path / "memory" / "excess", 1)
    assert not (tmp_path / "memory" / "excess").exists()
    # 累计增长预算耗尽也不阻止等量替换或缩减，且缩减不重新赠送增长预算。
    repair = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=1000, config=config)
    _write(repair, tmp_path / "memory" / "second", 7)
    _write(repair, tmp_path / "memory" / "first", 4)
    assert quota._read_quota_ledger(tmp_path)["permanent_pending_growth_bytes"] == 12
    _reconcile(tmp_path)
    assert quota._read_quota_ledger(tmp_path)["permanent_pending_growth_bytes"] == 0


def test_quota_policy_factory_forwards_optional_tuning(tmp_path):
    (tmp_path / "quota.json").write_text(json.dumps({"max_disk_mb": 1}))
    (tmp_path / "memory").mkdir()
    config = SimpleNamespace(owner_quota_permanent_headroom_percent=0)
    enforcer = quota.owner_quota_enforcer_from_policy(tmp_path, config=config)
    with pytest.raises(quota.OwnerQuotaExceeded):
        _write(enforcer, tmp_path / "memory" / "x", enforcer.max_bytes + 1)
    assert not (tmp_path / "memory" / "x").exists()


@pytest.mark.parametrize("interval", [0.125, 4.25])
def test_poll_interval_survives_real_watch_snapshot_and_discovery(tmp_path, interval):
    from agent_py_agent.agent.ingestion.watch_state import (
        load_state,
        new_state,
        persist_state,
        state_dir,
    )

    state = new_state(tmp_path, (tmp_path / "feed.ndjson").as_uri(), {})
    # 单独检查真实投影，不让加载器的旧默认掩盖快照丢字段。
    state.tuning = replace(state.tuning, poll_interval_seconds=interval)
    persist_state(state)
    payload = json.loads((state_dir(tmp_path) / f"{state.watch_id}.json").read_text())
    assert payload["tuning"].get("poll_interval_seconds") == interval
    restored = load_state(tmp_path, state.watch_id)
    assert restored is not None
    assert restored.tuning.poll_interval_seconds == interval
    assert type(restored.tuning.poll_interval_seconds) is float
    payload["saved_at"] = 100.0
    assert harvester_has_due_work(payload, 100.0 + interval - 0.001) is False
    assert harvester_has_due_work(payload, 100.0 + interval) is True


def test_legacy_watch_snapshot_without_poll_interval_keeps_default(tmp_path):
    from agent_py_agent.agent.ingestion.watch_state import (
        load_state,
        new_state,
        persist_state,
        state_dir,
    )

    state = new_state(tmp_path, (tmp_path / "feed.ndjson").as_uri(), {})
    persist_state(state)
    path = state_dir(tmp_path) / f"{state.watch_id}.json"
    payload = json.loads(path.read_text())
    payload["tuning"].pop("poll_interval_seconds", None)
    path.write_text(json.dumps(payload))
    restored = load_state(tmp_path, state.watch_id)
    assert restored is not None
    assert restored.tuning.poll_interval_seconds == 1.5


def test_retry_legacy_budget_does_not_consume_lower_priority_invalid_values():
    analyzer = SubAgentFailureAnalyzer(
        max_retry_attempts=4, retry_limit=True,
        config=SimpleNamespace(runner_failure_retry_limit=float("inf")),
    )
    assert _analysis(analyzer, 3).should_retry is True
    assert _analysis(analyzer, 4).should_retry is False


def test_poll_oversized_integer_uses_same_default_in_both_consumers():
    value = 10**1000
    assert tuning_from_params({"poll_interval_seconds": value}).poll_interval_seconds == 1.5
    payload = _watch_payload(tuning={"poll_interval_seconds": value})
    assert harvester_has_due_work(payload, 101.499) is False
    assert harvester_has_due_work(payload, 101.5) is True


def test_quota_positive_headroom_rounding_to_zero_does_not_admit_growth(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "filler").write_bytes(b"x" * 19)
    enforcer = quota.OwnerQuotaEnforcer(tmp_path, max_bytes=19, config=SimpleNamespace(
        owner_quota_permanent_headroom_percent=5))
    target = tmp_path / "memory" / "one-byte"
    with pytest.raises(quota.OwnerQuotaExceeded):
        _write(enforcer, target, 1)
    assert not target.exists()
