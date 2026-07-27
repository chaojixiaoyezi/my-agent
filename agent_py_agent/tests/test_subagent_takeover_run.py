from __future__ import annotations

from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.services.takeover.run import TakeoverRunRequest


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
    source.current_step = "写完支付回调并验证幂等"
    source.latest_summary = "checkout 页面已完成，支付回调尚未完成。"
    source.blockers = ["旧 runner 已超时"]
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
    assert source_refs["checkpoint"].endswith("checkpoint.json")
    assert takeover.attributes["takeover_chain_depth"] == 1
    assert takeover.attributes["takeover_lineage_root_run_id"] == source.id
    context = manager.runner_context.build_execution_context(takeover.id)
    handoff = context.context_bundle["takeover"]
    assert handoff["source_run_id"] == source.id
    assert handoff["current_step"] == "写完支付回调并验证幂等"
    assert handoff["latest_summary"] == "checkout 页面已完成，支付回调尚未完成。"
    prompt = _build_subagent_runner_prompt(context)
    assert '"takeover"' in prompt
    assert "不要用通用 read_file/list_files 读取受管状态面" in prompt


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


def test_takeover_run_reports_lookup_error_instead_of_duplicate_takeover(tmp_path, monkeypatch) -> None:
    manager = SubAgentManager(tmp_path)
    source = manager.create_run(goal="长任务 leaf", thought="超时", plan=["继续"], role="worker")
    source.status = "TIMEOUT"
    manager.save(source)

    def fail_list_runs():
        raise ValueError("bad subagent index")

    monkeypatch.setattr(manager, "list_runs", fail_list_runs)

    result = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="retry"))

    assert result.created is False
    assert result.applied is False
    assert result.takeover_run_id == ""
    assert result.load_error["context"] == "subagent_takeover_run.list_existing_takeovers"
    assert result.to_dict()["load_error"]["error"]["message"]


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


def test_runner_result_is_ignored_after_takeover(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    source = manager.create_run(goal="长任务 leaf", thought="超时", plan=["继续"], role="worker")
    source.status = "TIMEOUT"
    manager.save(source)
    first = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="first"))

    result = manager.runner_result.record_runner_result(
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


def test_takeover_chain_zero_allows_replacement_creation(tmp_path) -> None:
    manager = SubAgentManager(tmp_path, takeover_chain_max_depth=0)
    source = manager.create_run(goal="真实长任务", thought="中途失败", plan=["继续"], role="worker")
    source.status = "TIMEOUT"
    source.failure_type = "runner_timeout"
    manager.save(source)

    result = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="timeout"))

    assert result.created is True
    assert result.takeover_run_id
