"""覆盖智能体核心行为的小型回归测试。

这里故意保持为普通函数，因为仓库当前自带的是 `run_tests.py`
 这种轻量冒烟脚本，而不是引入完整测试框架依赖。
"""

from pathlib import Path
import tempfile

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


def test_memory_and_run():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "prompts").mkdir()
        (root / "prompts/default.md").write_text("动态规则", encoding="utf-8")
        cfg = AgentConfig(memory_path="memory.jsonl", prompt_files=["prompts/default.md"])
        agent = SimpleAgent(cfg, root)
        result = agent.run("记住我喜欢表格", inject=["回答要短"])
        assert "echo 后端" in result.response
        assert (root / "memory.jsonl").exists()
        assert len(agent.recall("表格")) >= 1


def test_subagents():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(subagent_workspace="subs", max_subagents=2)
        agent = SimpleAgent(cfg, root)
        tasks = agent.spawn_subagents("做一个 CLI", 5)
        assert len(tasks) == 2
        assert (root / "subs" / tasks[0].id / "task.json").exists()
