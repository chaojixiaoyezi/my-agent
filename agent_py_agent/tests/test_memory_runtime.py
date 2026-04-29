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
    hook_dir = tmp_path / "memory" / "hooks"
    files = sorted(raw_dir.glob("*.jsonl"))
    assert result.archive_events == 2
    assert result.archive_token_estimate > 0
    assert result.recovery_snapshot_path
    assert result.recovery_snapshot_id.startswith("snapshot:")
    assert len(files) == 1
    records = _read_jsonl(files[0])
    assert [record["speaker"] for record in records] == ["user", "assistant"]
    assert records[0]["content_preview"] == "请归档这轮对话"
    assert records[1]["action"] == "response"
    hook_files = sorted(hook_dir.glob("*.jsonl"))
    assert len(hook_files) == 1
    snapshots = _read_jsonl(hook_files[0])
    assert snapshots[0]["snapshot_id"] == result.recovery_snapshot_id
    assert snapshots[0]["user_intents"] == ["请归档这轮对话"]
    assert snapshots[0]["dispatch_events"][0]["source"] == "run"


def test_run_no_save_does_not_write_raw_archive(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)

    result = agent.run("不要归档这轮对话", save=False)

    assert result.archive_events == 0
    assert result.recovery_snapshot_path == ""
    assert not (tmp_path / "memory" / "raw").exists()
    assert not (tmp_path / "memory" / "hooks").exists()


def test_run_can_force_recovery_snapshot_without_raw_archive(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)

    result = agent.run(
        "子代理已完成，请写恢复锚点",
        save=False,
        recovery_snapshot=True,
        request_id="req-1",
        run_id="subagent-1",
        task_id="subagent-1",
        source="subagent_run",
        recovery_content_paths=["subagents/subagent-1/STATUS.md"],
        recovery_next_actions=["读取 STATUS.md 后继续验收"],
    )

    assert result.archive_events == 0
    assert result.recovery_snapshot_path
    assert not (tmp_path / "memory" / "raw").exists()
    snapshots = _read_jsonl(Path(result.recovery_snapshot_path))
    assert snapshots[0]["dispatch_events"][0]["request_id"] == "req-1"
    assert snapshots[0]["dispatch_events"][0]["run_id"] == "subagent-1"
    assert snapshots[0]["task_refs"] == ["subagent-1"]
    assert snapshots[0]["content_paths"] == ["subagents/subagent-1/STATUS.md"]
    assert snapshots[0]["next_actions"] == ["读取 STATUS.md 后继续验收"]
