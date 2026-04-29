from __future__ import annotations

"""memory runtime integration tests."""

import json
from pathlib import Path

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


def _write_route(root: Path) -> None:
    authority = root / "references" / "memory" / "routing.md"
    authority.parent.mkdir(parents=True, exist_ok=True)
    authority.write_text("长期规则正文：回答前必须读权威文件。", encoding="utf-8")
    index = root / "memory" / "routing" / "INDEX.md"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(
        """# Memory Routes

## memory.routing
topic: 长期规则索引
trigger_keywords: 长期规则, 规则索引
aliases: memory index
when_to_read: 用户讨论长期规则或 memory index 时读取
authority_path: references/memory/routing.md
scope: global
priority: 30
""",
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_run_injects_routed_memory_authority_context(tmp_path):
    _write_route(tmp_path)
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            memory_rule_routing_enabled=True,
            memory_rule_routing_mode="soft",
            memory_rule_auto_read_limit=1,
        ),
        tmp_path,
    )

    result = agent.run("请按长期规则处理 memory index", save=False)

    assert result.memory_route_matches == 1
    assert result.memory_route_paths == ["references/memory/routing.md"]
    assert "### Routed memory authority: references/memory/routing.md" in result.prompt
    assert "长期规则正文" in result.prompt
    assert result.archive_events == 0


def test_run_writes_raw_archive_when_saved(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)

    result = agent.run("请归档这轮对话", save=True)

    raw_dir = tmp_path / "memory" / "raw"
    files = sorted(raw_dir.glob("*.jsonl"))
    assert result.archive_events == 2
    assert result.archive_token_estimate > 0
    assert len(files) == 1
    records = _read_jsonl(files[0])
    assert [record["speaker"] for record in records] == ["user", "assistant"]
    assert records[0]["content_preview"] == "请归档这轮对话"
    assert records[1]["action"] == "response"


def test_run_no_save_does_not_write_raw_archive(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)

    result = agent.run("不要归档这轮对话", save=False)

    assert result.archive_events == 0
    assert not (tmp_path / "memory" / "raw").exists()
