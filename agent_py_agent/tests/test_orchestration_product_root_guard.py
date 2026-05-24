"""LLM: Tests product-root guards for root/coordinator orchestration seeds.

函数/模块用途: 保护真实产物目录必须从 create_subagents 的 extra_write_roots 进入，
避免 root/coordinator 把内部 agent-run workspace 当成用户 deliverables 目录。
"""

from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool


# LLM: This regression uses structured output_files because prose is not a machine fact source.
# 函数用途: 显式 root/coordinator 交付结构化产物文件时必须带产物写入根，缺失时拒绝创建并要求模型重试。
def test_explicit_coordinator_product_delivery_requires_write_root():
    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10

    tool = CreateSubagentsTool(mock_agent)
    result = tool.execute({
        "goal": "交付示例网站。",
        "output_files": ["build/index.html", "build/style.css", "build/app.js"],
        "role": "coordinator",
    })

    assert result.ok is False
    assert "extra_write_roots" in result.output
    assert "build" in result.output
    mock_agent.subagents.create_run.assert_not_called()
