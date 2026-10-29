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


# LLM: 熔断只看已记录的宿主无进展事实；正常切片数量不是该事实。
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
    assert build_subagent_recovery_strategy(
        SubagentRecoveryStrategyRequest(task, no_progress_attempt_limit=3)
    ).no_progress_fuse


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


# LLM: 读取异常是未知，不是已证明的相同内容，既不能清零也不能累加宿主无进展计数。
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
