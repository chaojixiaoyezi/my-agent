from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
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
