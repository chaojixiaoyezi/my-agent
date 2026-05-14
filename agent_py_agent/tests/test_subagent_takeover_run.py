from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.services.takeover_run import TakeoverRunRequest


# LLM: test_takeover_run_records_source_and_reuses_task_refs covers dead-run takeover.
# 函数用途: 原 run 挂死后，新 takeover run 要带着同一任务目录和 artifacts refs 接着做，并记录旧 run 被接管。
def test_takeover_run_records_source_and_reuses_task_refs(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    source = manager.create_run(
        goal="实现 checkout 支付流程",
        thought="worker 中途挂了。",
        plan=["写页面", "跑测试"],
        role="worker",
        allowed_tools=["read_file", "write_file"],
    )
    source.status = "TIMEOUT"
    source.failure_type = "runner_timeout"
    source.artifact_refs = [source.agent_run_artifacts_dir]
    manager.save(source)

    result = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="runner_timeout"))

    takeover = manager.load(result.takeover_run_id)
    reloaded_source = manager.load(source.id)
    source_refs = takeover.attributes["takeover_source_refs"]
    assert result.created is True
    assert reloaded_source.status == "TAKEN_OVER"
    assert reloaded_source.takeover_by == takeover.id
    assert takeover.parent_id == source.parent_id
    assert takeover.root_id == source.root_id
    assert source.task_dir in takeover.allowed_write_roots
    assert source_refs["task_dir"] == source.task_dir
    assert source_refs["agent_run_artifacts_dir"] == source.agent_run_artifacts_dir
    assert source_refs["latest_continue_packet"].endswith("latest_continue_packet.json")
    assert takeover.attributes["takeover_chain_depth"] == 1
    assert takeover.attributes["takeover_lineage_root_run_id"] == source.id


# LLM: takeover must inherit the parent-approved product write root, not force another capability request.
# 函数用途: 原 worker 已经被授权写业务产物目录时，新 takeover run 需要直接继承这个目录。
def test_takeover_run_inherits_source_product_write_roots(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    product_root = tmp_path / "deliverables" / "build"
    source = manager.create_run(
        goal="写 takeover demo",
        thought="worker 会被故意 timeout。",
        plan=["写产物"],
        role="worker",
        allowed_tools=["write_file"],
        extra_write_roots=[str(product_root)],
    )
    source.status = "TIMEOUT"
    source.failure_type = "runner_timeout"
    manager.save(source)

    result = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="runner_timeout"))
    takeover = manager.load(result.takeover_run_id)

    assert not product_root.exists()
    assert str(product_root) in takeover.allowed_write_roots


# LLM: test_takeover_run_is_idempotent_when_source_already_taken_over prevents runaway expansion.
# 函数用途: 同一个挂死 run 已经有 takeover 时，再次恢复不能继续创建无限多个接管 run。
def test_takeover_run_is_idempotent_when_source_already_taken_over(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    source = manager.create_run(goal="长任务 leaf", thought="超时", plan=["继续"], role="worker")
    source.status = "TIMEOUT"
    manager.save(source)

    first = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="first"))
    second = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="retry"))

    run_ids = [task.id for task in manager.list_runs()]
    assert second.created is False
    assert second.takeover_run_id == first.takeover_run_id
    assert run_ids.count(first.takeover_run_id) == 1


# LLM: takeover source-ref lookup catches stale source snapshots that lost takeover_by.
# 函数用途: 即便旧 run 的 takeover_by 被旧快照污染为空，也能通过现有 takeover run 的 attributes 复用接管者。
def test_takeover_run_reuses_existing_source_ref_when_source_snapshot_is_stale(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    source = manager.create_run(goal="长任务 leaf", thought="超时", plan=["继续"], role="worker")
    source.status = "TIMEOUT"
    manager.save(source)
    existing = manager.create_run(goal="接管旧 run", thought="继续", plan=["继续"], role="worker")
    existing.attributes["takeover_source_run_id"] = source.id
    manager.save(existing)

    result = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="retry"))

    assert result.created is False
    assert result.takeover_run_id == existing.id
    assert [task.id for task in manager.list_runs()].count(existing.id) == 1


# LLM: persistence merge protects recorded takeover from later stale full-object saves.
# 函数用途: 超时旧 runner 或旧父级快照保存 TIMEOUT 时，不能覆盖已经落盘的 TAKEN_OVER/takeover_by。
def test_takeover_state_survives_stale_task_save(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    source = manager.create_run(goal="长任务 leaf", thought="超时", plan=["继续"], role="worker")
    source.status = "TIMEOUT"
    manager.save(source)
    first = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="first"))

    stale = manager.load(source.id)
    stale.status = "TIMEOUT"
    stale.takeover_by = ""
    stale.takeover_records = []
    manager.save(stale)
    reloaded = manager.load(source.id)

    assert reloaded.status == "TAKEN_OVER"
    assert reloaded.takeover_by == first.takeover_run_id
    assert len(reloaded.takeover_records) == 1


# LLM: stale finalized runner results must not rewrite a source run after takeover.
# 函数用途: 旧 attempt 超时后继续返回结果时，只能得到 ignored quick result，不能把 TAKEN_OVER 写回 TIMEOUT。
def test_runner_result_is_ignored_after_takeover(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    source = manager.create_run(goal="长任务 leaf", thought="超时", plan=["继续"], role="worker")
    source.status = "TIMEOUT"
    manager.save(source)
    first = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="first"))

    result = manager.record_runner_result(
        RecordRunnerResultParams(
            run_id=source.id,
            dry_run=False,
            ok=False,
            message="late timeout",
            status="TIMEOUT",
            verification_status="UNVERIFIED",
            failure_type="runner_timeout",
        )
    )
    reloaded = manager.load(source.id)

    assert "already taken-over" in result.message
    assert reloaded.status == "TAKEN_OVER"
    assert reloaded.takeover_by == first.takeover_run_id


# LLM: takeover chain fuse prevents repeated timeouts from growing an infinite replacement tree.
# 函数用途: 验证接管 run 自己也超时后，超过链深度上限时会阻塞上报，不再继续创建新 run。
def test_takeover_run_chain_exhaustion_blocks_without_new_child(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    manager.takeover_chain_max_depth = 1
    source = manager.create_run(goal="短超时任务", thought="原 worker 超时", plan=["写文件"], role="worker")
    source.status = "TIMEOUT"
    source.failure_type = "runner_timeout"
    manager.save(source)

    first = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="first_timeout"))
    takeover = manager.load(first.takeover_run_id)
    takeover.status = "TIMEOUT"
    takeover.failure_type = "runner_timeout"
    manager.save(takeover)

    second = manager.create_takeover_run(TakeoverRunRequest(source_run_id=takeover.id, reason="second_timeout"))
    reloaded_takeover = manager.load(takeover.id)

    assert second.created is False
    assert second.takeover_run_id == ""
    assert "chain exhausted" in second.message
    assert reloaded_takeover.status == "BLOCKED"
    assert reloaded_takeover.failure_type == "takeover_chain_exhausted"
    assert len(manager.list_runs()) == 2
