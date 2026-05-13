from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt
from agent_py_agent.agent.subagents.manager import SubAgentManager


# LLM: subagent saves must materialize a task-local continue packet before any parent rerun.
# 函数用途: 验证子代理保存时自动生成 latest_continue_packet 和 session compact ledger，供父级按 refs 接管。
def test_subagent_save_writes_task_local_continue_packet(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="继续实现 checkout.html",
        thought="购物站 leaf 被 compact 后要能接着写。",
        plan=["恢复 checkpoint", "继续补测试"],
        role="leaf_worker",
    )
    task.status = "BLOCKED"
    task.current_step = "等待父级授权后继续 checkout tests"
    task.latest_summary = "已经写完商品页，checkout tests 还没补齐。"
    task.blockers = ["缺少父级重新 dispatch"]
    task.artifact_refs = ["build/products.html"]
    task.evidence_refs = ["reports/runner_result.json"]

    manager.save(task)
    loaded = manager.load(task.id)

    packet_ref = Path(loaded.agent_run_compactions_dir) / "latest_continue_packet.json"
    ledger_ref = Path(loaded.agent_run_compactions_dir) / "session_compact_ledger.jsonl"
    packet = json.loads(packet_ref.read_text(encoding="utf-8"))
    ledger_lines = ledger_ref.read_text(encoding="utf-8").splitlines()

    assert packet["schema_version"] == "subagent_continue_packet.v1"
    assert packet["memory_scope"] == "task_local"
    assert packet["writes_main_memory"] is False
    assert packet["automatic_tool_execution"] == "none"
    assert packet["ready_to_continue"] is True
    assert packet["owner"] == {"owner_type": "subagent_run", "owner_id": task.id}
    assert packet["next_action"] == "等待父级授权后继续 checkout tests"
    assert packet["latest_summary"] == "已经写完商品页，checkout tests 还没补齐。"
    assert packet["restore_refs"]["agent_run_checkpoint"].endswith("checkpoint.json")
    assert loaded.agent_run_checkpoint_json in packet["recommended_read_paths"]
    assert ledger_lines
    assert json.loads(ledger_lines[-1])["packet_ref"] == str(packet_ref)


# LLM: parent reruns should pick up generated task-local continue packets through the normal context path.
# 函数用途: 验证父级重新构建 runner prompt 时自动读取保存生成的 continue packet，而不是靠手工传入。
def test_runner_prompt_uses_generated_task_local_continue_packet(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="继续购物网站 leaf 任务",
        thought="需要从 task-local packet 接续。",
        plan=["读 checkpoint", "继续写验收证据"],
        role="leaf_worker",
    )
    task.status = "RUNNING"
    task.current_step = "从 task-local packet 继续写验收证据"
    task.latest_summary = "页面骨架已经存在，剩余验收证据。"
    manager.save(task)

    context = manager.build_execution_context(task.id)
    prompt = _build_subagent_runner_prompt(context)

    assert "Task-Local Compact Continuation" in prompt
    assert "Continue Packet" in prompt
    assert "latest_continue_packet.json" in prompt
    assert "从 task-local packet 继续写验收证据" in prompt
    assert "页面骨架已经存在" in prompt
    assert "SOUL.md" not in prompt
