"""LLM: Tests for basic agent memory and subagent spawning; covers echo-backend
run, memory persistence, and subagent workspace file layout.

给人看的解释：
测试智能体记忆功能和子代理创建时文件目录结构是否正确。
"""

import tempfile
from pathlib import Path

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def test_run_does_not_auto_save_formal_memory():
    """LLM: Verifies that an ordinary turn cannot bypass Candidate/Promotion into long-term."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "prompts").mkdir()
        (root / "prompts/default.md").write_text("动态规则", encoding="utf-8")
        cfg = AgentConfig(
            memory_path="memory.jsonl",
            prompt_files=["prompts/default.md"],
        )
        agent = SimpleAgent(cfg, root)
        result = agent.run("记住我喜欢表格", inject=["回答要短"])
        assert "echo 后端" in result.response
        assert agent.memory.path.exists()
        assert agent.memory.path == agent.home_paths.owner_memory_long_term_dir / "memory.jsonl"
        assert not (root / "memory.jsonl").exists()
        assert agent.memory.all() == []
        assert agent.recall("表格") == []


def test_subagents():
    """LLM: Verifies that spawning subagents creates the expected workspace files."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs", max_subagents=2)
        agent = SimpleAgent(cfg, root)
        tasks = agent.spawn_subagents("做一个 CLI", 5)
        assert len(tasks) == 2
        assert (root / "subs" / tasks[0].id / "task.json").exists()
        assert (root / "subs" / tasks[0].id / "run.json").exists()
        assert tasks[0].root_id == tasks[0].id
        assert Path(tasks[0].status_file).exists()
        assert Path(tasks[0].work_log_file).exists()
        assert Path(tasks[0].action_receipts_file).exists()
        assert Path(tasks[0].acceptance_file).exists()
        assert Path(tasks[0].test_checklist_file).exists()
        assert Path(tasks[0].bugs_file).exists()
        assert Path(tasks[0].skill_usage_file).exists()
        assert Path(tasks[0].skill_sparks_file).exists()
        assert Path(tasks[0].handoff_file).exists()
        assert Path(tasks[0].debrief_file).exists()
        assert Path(tasks[0].output_json).exists()
        assert Path(tasks[0].dependencies_json).exists()
