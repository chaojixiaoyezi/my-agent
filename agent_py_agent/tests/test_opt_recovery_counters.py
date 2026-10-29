# LLM: 使用真实canonical提交口模拟精确执行轮的开始/收口，不调用模型、网络或真实owner。
# 模块用途: 验证审计attempt与失败重试、无进展熔断分别记账。
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.runner.dispatch import _runner_retry_reason
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.persistence import service as persistence
from agent_py_agent.agent.subagents.services.recovery.strategy import (
    SubagentRecoveryStrategyRequest,
    build_subagent_recovery_strategy,
)


# LLM: 开始与结算分开走原mutate，以便检验每个attempt只能消耗一次预算。
# 函数用途: 在临时任务上结算一拍正常yield或真实失败。
def _slice(manager, run_id, number, *, failed=False, effect=None):
    # LLM: 只写宿主启动字段，不伪造工具效果或进展证据。
    # 函数用途: 发布本拍唯一attempt标识。
    def start(task):
        task.runner_active_attempt_id = f"attempt-{number}"
        task.status = "RUNNING"
        task.failure_type = ""

    manager.mutate(run_id, start)
    if effect is not None:
        effect()

    # LLM: 只模拟既有runner结果提交的结构化字段；审计次数每拍均增长。
    # 函数用途: 收口当前attempt并记录其真实分类。
    def settle(task):
        task.runner_attempts += 1
        task.runner_active_attempt_id = ""
        task.status = "FAILED" if failed else "PLANNING"
        task.failure_type = "runner_error" if failed else ""
        task.turn_end_reason = "error" if failed else "yielded"

    return manager.mutate(run_id, settle)


# LLM: 6次正常切片后才发生首次失败，绝不能按总尝试次数耗光重试额度。
# 函数用途: 复现并阻断total-1算法。
def test_runner_yields_do_not_consume_failure_retry_budget(tmp_path):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="多拍任务", thought="计数分离", plan=["继续"])
    for number in range(1, 7):
        _slice(manager, task.id, number)
    task = _slice(manager, task.id, 7, failed=True)
    assert "retry=1/+1" in _runner_retry_reason(task, 1)
    path = Path(task.agent_run_workspace_dir) / "canonical_state.json"
    raw = json.loads(path.read_text())
    assert raw["runner_attempts"] == 7
    assert raw["failure_retry_count"] == 0
    assert raw["no_progress_count"] == 1
    task = _slice(manager, task.id, 8, failed=True)
    assert not _runner_retry_reason(task, 1)
    assert json.loads(path.read_text())["failure_retry_count"] == 1


# LLM: 熔断只看宿主无进展失败累计，正常切片不消耗也不冒称连续失败，文案须保持同义。
# 函数用途: 保证board与strategy不再拿总attempt做无进展计数。
def test_subagent_no_progress_fuse_ignores_normal_slices(tmp_path):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="无进展判据", thought="分别计数", plan=["继续"])
    for number in range(1, 6):
        _slice(manager, task.id, number)
    task = _slice(manager, task.id, 6, failed=True)
    result = build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(task, no_progress_attempt_limit=3))
    assert not result.no_progress_fuse
    for number in (7, 8):
        task = _slice(manager, task.id, number, failed=True)
    fused = build_subagent_recovery_strategy(
        SubagentRecoveryStrategyRequest(task, no_progress_attempt_limit=3)
    )
    assert fused.no_progress_fuse
    assert "累计" in fused.runner_instruction
    assert "连续" not in fused.runner_instruction


# LLM: 保存同一已收口快照不能重复累加；未知旧历史只能保留为unknown，不从总数猜失败。
# 函数用途: 检验重复保存幂等与旧数据迁移的诚实性。
def test_subagent_recovery_counter_save_is_idempotent(tmp_path):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="幂等提交", thought="不重复消费预算", plan=["继续"])
    task = _slice(manager, task.id, 1, failed=True)
    manager.save(task)
    manager.save(task)
    raw = json.loads((Path(task.agent_run_workspace_dir) / "canonical_state.json").read_text())
    assert raw["no_progress_count"] == 1
    assert raw["failure_retry_count"] == 0


# LLM: 既存产物在启动前就存在，不是本次执行新产生的进展；真实失败仍应增加无进展计数。
# 函数用途: 复现首次指纹没有启动基线而误清零的审查问题。
def test_subagent_preexisting_artifact_is_not_new_progress(tmp_path):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="旧产物基线", thought="不靠首次观察假进展", plan=["继续"])
    artifact = Path(task.task_workspace_dir) / "old-result.md"
    artifact.write_text("unchanged old content")
    task.artifact_refs = [str(artifact)]
    manager.save(task)
    task = _slice(manager, task.id, 1, failed=True)
    raw = json.loads((Path(task.agent_run_workspace_dir) / "canonical_state.json").read_text())
    assert raw["no_progress_count"] == 1


# LLM: 部分计数缺失/非法必须留下非零unknown，不得在再次保存后重新开放自动重试额度。
# 函数用途: 复现provider/no-progress/unknown字段被or零洗掉的审查问题。
@pytest.mark.parametrize("key", ["provider_retry_count", "no_progress_count", "recovery_history_unknown_count"])
@pytest.mark.parametrize("broken", [None, True, -1, "invalid"])
def test_subagent_partial_counter_corruption_is_not_zeroed(tmp_path, key, broken):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="损坏计数保真", thought="不重开失败额度", plan=["继续"])
    task = _slice(manager, task.id, 1, failed=True)
    path = Path(task.agent_run_workspace_dir) / "canonical_state.json"
    raw = json.loads(path.read_text())
    if broken is None:
        raw.pop(key)
    else:
        raw[key] = broken
    path.write_text(json.dumps(raw))
    manager.save(manager.load(task.id))
    raw = json.loads(path.read_text())
    assert raw["recovery_history_unknown_count"] > 0
    assert not _runner_retry_reason(manager.load(task.id), 10)


# LLM: 比较启动与结算时真实文件字节；缺失文件的首次创建和旧文件修改都应识别为本轮进展。
# 函数用途: 验证真实产物推进可重置之前的无进展计数。
@pytest.mark.parametrize("initial", [None, "old content"])
def test_subagent_actual_artifact_progress_resets_fuse(tmp_path, initial):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="真实字节进展", thought="从宿主读取", plan=["写产物"])
    artifact = Path(task.task_workspace_dir) / "result.md"
    if initial is not None:
        artifact.write_text(initial)
    task.artifact_refs = [str(artifact)]
    manager.save(task)
    task = _slice(manager, task.id, 1, failed=True)
    assert task._extra_fields["no_progress_count"] == 1
    task = _slice(manager, task.id, 2, failed=True, effect=lambda: artifact.write_text("new content"))
    assert task._extra_fields["no_progress_count"] == 0


# LLM: 读取异常不清零/累加已证无进展，只增加独立未知观察次数，须在恢复回执可见。
# 函数用途: 避免读取失败制造虚假的无进展熔断证据。
def test_subagent_unreadable_artifact_is_unknown_progress(tmp_path, monkeypatch):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="未知观察", thought="不拿错误当证据", plan=["读取产物"])
    artifact = Path(task.task_workspace_dir) / "result.md"
    artifact.write_text("stable")
    task.artifact_refs = [str(artifact)]
    manager.save(task)
    task = _slice(manager, task.id, 1, failed=True)

    # LLM: 只在第二拍开始之后注入宿主读取故障，不伪造进展或更改生产读取权限。
    # 函数用途: 构造结算时的权限读取错误。
    def unreadable(*args, **kwargs):
        raise PermissionError("injected read failure")

    task = _slice(manager, task.id, 2, failed=True,
                  effect=lambda: monkeypatch.setattr(persistence, "read_bytes_beneath", unreadable))
    assert task._extra_fields["no_progress_count"] == 1
    assert task._extra_fields["no_progress_unknown_count"] == 1
    strategy = build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(task))
    assert strategy.to_dict()["no_progress_unknown_count"] == 1
    from agent_py_agent.agent.subagents.services.recovery.orchestrator import (
        RecoveryOrchestrationRequest,
    )

    report = manager.hierarchy.orchestrate_recovery(params=RecoveryOrchestrationRequest(run_ids=[task.id]))
    assert report.to_dict()["steps"][0]["strategy_snapshot"]["no_progress_unknown_count"] == 1
    ledger = json.loads((tmp_path / "subagent_recovery_ledger.jsonl").read_text().splitlines()[-1])
    assert ledger["strategy_snapshot"]["no_progress_unknown_count"] == 1


# LLM: 读满1MiB但仍有声明产物未读时必须返回未知，不可用前四件的部分指纹冒充全集。
# 函数用途: 验证有界读取预算不会生成不完整的进展证据。
def test_subagent_fingerprint_budget_does_not_hash_partial_set(tmp_path):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="读取预算", thought="拒绝部分证据", plan=["核对产物"])
    refs = []
    for index in range(5):
        path = Path(task.task_workspace_dir) / f"result-{index}.txt"
        path.write_bytes(b"x" * (256 * 1024 if index < 4 else 1))
        refs.append(str(path))
    task.artifact_refs = refs
    assert persistence._recovery_progress_fingerprint(task) is None


# LLM: 更改模型声明的引用或删除产物均不能伪造内容推进；只比较同一宿主观察集合中的有效内容版本。
# 函数用途: 阻止引用元数据和产物消失清零无进展计数。
@pytest.mark.parametrize("change", ["declare_old", "delete"])
def test_subagent_artifact_metadata_or_deletion_does_not_fake_progress(tmp_path, change):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="进展证据保真", thought="不信引用元数据", plan=["核对字节"])
    artifact = Path(task.task_workspace_dir) / "old-result.md"
    artifact.write_text("already existed before attempt")
    if change == "delete":
        task.artifact_refs = [str(artifact)]
        manager.save(task)
    task = _slice(manager, task.id, 1, failed=True)
    assert task._extra_fields["no_progress_count"] == 1

    # LLM: 在第二次执行中改变引用或删除测试产物，文件原内容没有产生任何新版本。
    # 函数用途: 构造两种不应认定为正向进展的宿主观察。
    def alter():
        if change == "declare_old":
            manager.mutate(task.id, lambda current: current.artifact_refs.append(str(artifact)))
        else:
            artifact.unlink()

    task = _slice(manager, task.id, 2, failed=True, effect=alter)
    assert task._extra_fields["no_progress_count"] == (1 if change == "declare_old" else 2)


# LLM: 不手动改状态，通过真实准备与结果提交制造BLOCKED，分别钉住普通1次和供应8次预算。
# 函数用途: 验证阻塞失败不会绕过扣账而无限重派。
@pytest.mark.parametrize("failure,limit", [("structured_output_parse_error", 1), ("transient_error", 8)])
def test_blocked_real_lifecycle_stops_at_retry_budget(tmp_path, failure, limit):
    from agent_py_agent.agent.subagents.manager_runner_result_payload import (
        RecordRunnerResultParams,
    )
    from agent_py_agent.agent.subagents.models import SubAgentParsedOutput
    from agent_py_agent.agent.subagents.services.lifecycle_runner_attempts import (
        prepare_runner_attempt,
    )

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="阻塞扣账", thought="真实生命周期", plan=["继续"])
    for number in range(limit + 1):
        if number:
            assert _runner_retry_reason(task, 1, no_progress_attempt_limit=0)
        started = prepare_runner_attempt(manager, task.id)
        manager.runner_result.record_runner_result(RecordRunnerResultParams(
            run_id=task.id, attempt_id=started.runner_active_attempt_id,
            dry_run=False, ok=False, message="隔离测试失败", status="BLOCKED",
            failure_type=failure, turn_end_reason="error",
            structured_output=SubAgentParsedOutput(found=failure == "structured_output_parse_error", ok=False),
        ))
        task = manager.load(task.id)
        assert task.status == "BLOCKED"
        assert task.failure_type == failure
    assert not _runner_retry_reason(task, 1, no_progress_attempt_limit=0)
    counter = "provider_retry_count" if failure == "transient_error" else "failure_retry_count"
    assert task._extra_fields[counter] == limit
    assert task.runner_attempts == limit + 1


# LLM: 原始JSON损坏首次加载即拒绝候选和自动恢复建议，保留有效refs也不能绕过；全程不save且canonical字节不变。
# 函数用途: 同时验证只读load、策略、正式恢复报告及原账本的坏账关闭。
@pytest.mark.parametrize("key", ["failure_retry_count", "provider_retry_count", "no_progress_count", "recovery_history_unknown_count"])
@pytest.mark.parametrize("value", [True, False, -1, "1", None])
def test_corrupted_counter_read_only_load_fails_closed(tmp_path, key, value):
    from agent_py_agent.agent.subagents.services.recovery.orchestrator import (
        RecoveryOrchestrationRequest,
    )

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="只读坏账", thought="不经save准入", plan=["读取"])
    _slice(manager, task.id, 1, failed=True)
    path = Path(task.agent_run_workspace_dir) / "canonical_state.json"
    raw = json.loads(path.read_text())
    raw[key] = value
    path.write_text(json.dumps(raw))
    original = path.read_bytes()
    loaded = manager.load(task.id)
    assert loaded._extra_fields["recovery_history_unknown_count"] >= 1
    assert not _runner_retry_reason(loaded, 20, no_progress_attempt_limit=0)
    strategy = build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(loaded))
    assert strategy.recovery_refs
    assert strategy.recommended_action == "manual_review"
    assert strategy.recovery_mode.value == "manual_review_unknown_recovery_history"
    assert strategy.blocked_by == ["recovery_history_unknown"]
    assert not strategy.no_progress_fuse
    assert "人工复核" in strategy.runner_instruction
    report = manager.hierarchy.orchestrate_recovery(params=RecoveryOrchestrationRequest(run_ids=[task.id]))
    step = report.steps[0]
    assert step.orchestration_action == "manual_review"
    assert step.requires_human
    assert not step.requires_dispatch
    assert step.blocked_by == ["recovery_history_unknown"]
    ledger = json.loads((tmp_path / "subagent_recovery_ledger.jsonl").read_text().splitlines()[-1])
    assert ledger["orchestration_action"] == "manual_review"
    assert ledger["requires_dispatch"] is False
    assert ledger["blocked_by"] == ["recovery_history_unknown"]
    assert path.read_bytes() == original


# LLM: 不合法的计数总和及缺失runner_attempts均是新格式坏账，不能误当干净旧版本迁移。
# 函数用途: 验证不保存也会拒绝矛盾恢复记录。
@pytest.mark.parametrize("change", ["sum", "missing_attempts"])
def test_inconsistent_counter_read_only_load_fails_closed(tmp_path, change):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="矛盾坏账", thought="不经save", plan=["读取"])
    _slice(manager, task.id, 1, failed=True)
    path = Path(task.agent_run_workspace_dir) / "canonical_state.json"
    raw = json.loads(path.read_text())
    if change == "sum":
        raw.update(failure_retry_count=1, provider_retry_count=1)
    else:
        raw.pop("runner_attempts")
    path.write_text(json.dumps(raw))
    loaded = manager.load(task.id)
    assert loaded._extra_fields["recovery_history_unknown_count"] >= 1
    assert not _runner_retry_reason(loaded, 20, no_progress_attempt_limit=0)


# LLM: 所有新计数均缺失才是干净旧记录，按总审计次数保守迁移，不制造永久unknown；读取不写盘。
# 函数用途: 验证旧数据仍可恢复且首次正式保存保留迁移依据。
@pytest.mark.parametrize("failure,counter", [("runner_error", "failure_retry_count"), ("transient_error", "provider_retry_count")])
def test_clean_legacy_counters_migrate_without_unknown(tmp_path, failure, counter):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="旧计数迁移", thought="保守恢复", plan=["读取"])
    path = Path(task.agent_run_workspace_dir) / "canonical_state.json"
    raw = json.loads(path.read_text())
    for name in ("failure_retry_count", "provider_retry_count", "no_progress_count", "recovery_history_unknown_count", "no_progress_unknown_count"):
        raw.pop(name, None)
    raw.update(status="FAILED", failure_type=failure, runner_attempts=3)
    path.write_text(json.dumps(raw))
    original = path.read_bytes()
    loaded = manager.load(task.id)
    assert loaded._extra_fields[counter] == 2
    assert loaded._extra_fields["recovery_history_unknown_count"] == 0
    assert _runner_retry_reason(loaded, 3, no_progress_attempt_limit=0)
    assert path.read_bytes() == original
    manager.save(loaded)
    saved = json.loads(path.read_text())
    assert saved[counter] == 2
    assert saved["recovery_counter_migration"]["runner_attempts"] == 3


# LLM: 读取真实隔离能力配置并经过生产pending/count入口；0不被or默认4覆盖，10与2分别允许/熔断。
# 函数用途: 验证派发循环与规范候选选择使用同一阈值。
@pytest.mark.parametrize("limit,expected", [(0, True), (10, True), (2, False)])
def test_dispatch_configured_no_progress_limit(tmp_path, limit, expected):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
        _final_pending_runner_count,
    )
    from agent_py_agent.agent.agent_core.orchestration.dispatch.runner_candidates import (
        _runner_candidates_for_context,
    )
    from agent_py_agent.agent.agent_core.orchestration.dispatch.service import (
        update_pending_work_state,
    )

    task = SimpleNamespace(
        id="retry-task", status="FAILED", channel_status="OK", failure_type="runner_error",
        runner_attempts=4, capability_requests=[], capability_gaps=[], capability_grants=[],
        _extra_fields={"failure_retry_count": 0, "provider_retry_count": 0,
                       "no_progress_count": 4, "recovery_history_unknown_count": 0},
    )
    config = tmp_path / "capability.yaml"
    config.write_text(f"subagent_no_progress_attempt_limit: {limit}\n")
    agent = SimpleNamespace(
        config=SimpleNamespace(runner_failure_retry_limit=10), capability_config_path=config,
        subagents=SimpleNamespace(list_runs=lambda: [task]),
    )
    assert update_pending_work_state(agent) is expected
    assert _final_pending_runner_count(agent) == (int(expected), {})
    ctx = SimpleNamespace(include_run_ids=[], background_launch_id="", max_runners=4)
    selected = _runner_candidates_for_context([task], ctx, 10, no_progress_attempt_limit=limit)
    assert bool(selected) is expected


# LLM: 真实组合根绑定原能力配置文件；同一配置必须同时控制候选、prepare模型可见指令和正式树/恢复报告。
# 函数用途: 复现筛选接线之后启动和恢复仍暗用默认4的残留。
@pytest.mark.parametrize("limit,fused", [(0, False), (10, False), (2, True)])
def test_recovery_config_reaches_real_prepare_and_public_reports(tmp_path, monkeypatch, limit, fused):
    from types import SimpleNamespace

    from agent_py_agent.agent import core
    from agent_py_agent.agent.subagents.services.hierarchy.recovery import (
        HierarchyRecoveryRequest,
    )
    from agent_py_agent.agent.subagents.services.lifecycle_runner_attempts import (
        prepare_runner_attempt,
    )
    from agent_py_agent.agent.subagents.services.recovery.orchestrator import (
        RecoveryOrchestrationRequest,
    )

    config_path = tmp_path / "explicit-capability.yaml"
    config_path.write_text(f"subagent_no_progress_attempt_limit: {limit}\n")
    agent = SimpleNamespace(
        root=tmp_path / "different-root", capability_config_path=config_path,
        effective_workspace_root=tmp_path, effective_workspace_roots=[tmp_path],
        home_paths=SimpleNamespace(owner_id="", owner_home_dir=""),
        config=SimpleNamespace(subagent_role_template_dirs=[], subagent_debug_trace_level=0,
                               subagent_takeover_chain_max_depth=0, execution_mode="local_unmanaged"),
        local_store=None, collaboration_store=None, conversation_store=None, memory_candidates=None,
        owner_policy=SimpleNamespace(to_dict=lambda: {}), capability_router=SimpleNamespace(),
        ensure_mcp_catalog_ready=lambda _router: None,
    )
    monkeypatch.setattr(core, "_skill_proposal_service", lambda _agent: None)
    manager = core._build_subagent_manager(agent, {"subagent_workspace": tmp_path / "runs"})
    task = manager.create_run(goal="配置一致", thought="真实组合及恢复入口", plan=["继续"])
    for number in range(1, 5):
        task = _slice(manager, task.id, number, failed=True)
    assert task._extra_fields["no_progress_count"] == 4
    report = manager.hierarchy.orchestrate_recovery(params=RecoveryOrchestrationRequest(run_ids=[task.id]))
    assert report.steps[0].strategy_snapshot["no_progress_fuse"] is fused
    tree = manager.hierarchy.build_hierarchy_recovery_packet(params=HierarchyRecoveryRequest(root_run_id=task.id))
    assert tree.nodes[0].no_progress_fuse is fused
    started = prepare_runner_attempt(manager, task.id)
    instruction = started.attributes["runner_recovery_preflight"]["runner_instruction"]
    assert ("不要继续自动重试" in instruction) is fused
    assert bool(_runner_retry_reason(task, 10, no_progress_attempt_limit=limit)) is (not fused)
