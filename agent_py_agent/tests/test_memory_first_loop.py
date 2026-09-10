from __future__ import annotations

"""LLM: regression tests for the first usable memory-system closed loop.

给人看的解释：
这组测试覆盖第一批闭环里最关键的新约束：
archive level 过滤、压缩前 snapshot 门禁、路由校验命令，以及 capability gap 到长期规则注入。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_py_agent.agent.agent_core.runtime.loop_support as runtime_loop_support
import agent_py_agent.agent.agent_core.runtime_mixin as runtime_mixin
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive import (
    CompressionSnapshot,
    RawMemoryEvent,
    append_raw_event,
    write_compression_snapshot,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityGapParams
from agent_py_agent.cli.parser import build_parser


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _write_config(tmp_path: Path, extra: str = "") -> Path:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        'workspace_root: ""\n'
        f'my_agent_home: "{(tmp_path / "home").as_posix()}"\n'
        'model_backend: "echo"\n'
        
        'subagent_workspace: "subagents"\n'
        'local_store_path: "local_store/local.db"\n'
        'local_store_files_dir: "local_store/files"\n'
        'local_store_events_path: "local_store/events.jsonl"\n'
        f"{extra}",
        encoding="utf-8",
    )
    return config_path


def test_write_compression_snapshot_creates_authoritative_json_file(tmp_path):
    """LLM: verify compression hook writes both the hook JSONL entry and authoritative snapshot JSON.

    给人看的解释：
    压缩前 hook 现在不只是写一条 JSONL，还要落一份单文件权威快照。
    这能保证压缩恢复和 doctor 有稳定的 readback 目标。
    """

    result = write_compression_snapshot(
        tmp_path,
        session_id="session-1",
        turn_id="turn-1",
        role="system",
        content="压缩前现场：最近几轮 memory、注入规则和恢复上下文。",
        archive_level=2,
        request_id="req-1",
    )

    snapshot_file = Path(result.snapshot_file_path)
    hook_file = Path(result.hook_path)
    assert snapshot_file.exists()
    assert hook_file.exists()
    payload = json.loads(snapshot_file.read_text(encoding="utf-8"))
    assert payload["turn_id"] == "turn-1"
    assert payload["archive_level"] == 2
    assert payload["token_estimate"] > 0
    assert payload["timestamp"]


def test_memory_archive_list_can_filter_by_archive_level(tmp_path, capsys):
    config_path = _write_config(tmp_path)
    root = tmp_path / "home" / "owners" / "local" / "main"
    append_raw_event(
        root,
        RawMemoryEvent(
            event_id="raw-level-0",
            session_id="session-1",
            speaker="user",
            target="assistant",
            action="message",
            content_preview="level0",
            content_hash="sha256:0",
            archive_level=0,
            created_at="2026-05-02T10:00:00+00:00",
        ),
    )
    append_raw_event(
        root,
        RawMemoryEvent(
            event_id="raw-level-3",
            session_id="session-1",
            speaker="assistant",
            target="user",
            action="response",
            content_preview="level3",
            content_hash="sha256:3",
            archive_level=3,
            created_at="2026-05-02T10:01:00+00:00",
        ),
    )

    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "memory-archive-list",
            "--layer",
            "raw",
            "--level",
            "3",
            "--json",
        ]
    )
    code = args.func(args)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert [record["id"] for record in payload["records"]] == ["raw-level-3"]
    assert payload["records"][0]["archive_level"] == 3


def test_output_token_budget_does_not_trigger_fake_memory_compaction(tmp_path, monkeypatch):
    """输出预算不能作为输入压缩阈值，也不能触发旧伪摘要旁路。"""

    def boom(*args, **kwargs):
        raise OSError("disk full")

    import agent_py_agent.agent.memory_archive as archive
    monkeypatch.setattr(archive, "write_compression_snapshot", boom)
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            max_tokens=1,
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        tmp_path,
    )

    result = agent.run("这是一段比输出 token 上限更长的输入，但还没到上下文压缩点。", save=False)
    assert result.compression_applied is False
    assert not result.compression_snapshot_id


def test_runtime_loop_preserves_routed_and_resume_context(monkeypatch):
    routed_context = SimpleNamespace(
        injected_sections=["### Routed Memory\nmemory/routing/rules/compact.md"],
        required_read_paths=["memory/routing/rules/compact.md"],
        candidate_paths=["memory/routing/rules/subagent.md"],
        matches=[],
        receipts=[],
        findings=[],
    )
    resume_context = SimpleNamespace(
        context_block="恢复线索：继续 compact split 前先读 checkpoint。",
        injected=True,
    )
    captured: dict[str, object] = {}
    agent = _runtime_context_capture_agent(captured)

    monkeypatch.setattr(
        runtime_loop_support, "build_routed_memory_context", lambda *args, **kwargs: routed_context
    )
    monkeypatch.setattr(
        runtime_loop_support,
        "runtime_route_root_and_index",
        lambda *args, **kwargs: (Path("."), "memory/routing/INDEX.md"),
    )
    monkeypatch.setattr(
        runtime_loop_support, "build_auto_resume_context", lambda *args, **kwargs: resume_context
    )

    prepared = runtime_loop_support._prepare_runtime_context(
        agent,
        runtime_loop_support.RuntimeContextRequest("继续 compact", [], True),
    )
    params = runtime_loop_support._runtime_loop_params(
        "继续 compact",
        prepared,
        runtime_loop_support.RunParams(request_id="req-compact"),
    )
    ctx = params

    assert ctx.routed_context is routed_context
    assert ctx.resume_context_section.startswith("### Auto Recovery Context")
    assert "恢复线索" in ctx.resume_context_section
    assert any("恢复线索" in item for item in ctx.runtime_injections)
    assert all("### Routed Memory" not in item for item in ctx.runtime_injections)


def _runtime_context_capture_agent(captured: dict[str, object]):
    class Memory:
        def search_scoped(self, user_prompt: str, top_k: int, predicate):
            return []

    class EmptyFormalRepository:
        def list(self):
            return []

    class Agent:
        root = Path(".")
        memory = Memory()
        memory_hot = EmptyFormalRepository()
        memory_lessons = EmptyFormalRepository()
        config = SimpleNamespace(
            enable_tools=False,
            memory_top_k=1,
            memory_rule_routing_enabled=True,
            memory_rule_routing_mode="soft",
            memory_rule_auto_read_limit=2,
        )
        tools = SimpleNamespace(owner_type="main_agent")

    return Agent()


def test_memory_route_validate_reports_keyword_conflict_and_dead_link(tmp_path, capsys):
    """LLM: verify memory-route --validate detects conflicts and missing source files.

    给人看的解释：
    路由校验是长期规则规模化后的第一道保险，至少要能看出冲突词和死链。
    """

    config_path = _write_config(tmp_path)
    root = tmp_path / "home" / "owners" / "local" / "main"
    index = root / "memory" / "routing" / "INDEX.md"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(
        """# Memory Routes

## route.alpha
topic: Alpha
trigger_keywords: same, same
source_file: references/rules/missing-a.md
inject_mode: on_hit

## route.beta
topic: Beta
trigger_keywords: same
source_file: references/rules/missing-b.md
inject_mode: always
""",
        encoding="utf-8",
    )

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "memory-route", "--validate", "--json"])
    code = args.func(args)
    payload = json.loads(capsys.readouterr().out)
    warnings = payload["routes"]["validation_warnings"]
    assert code == 0
    assert any("duplicate keyword" in item for item in warnings)
    assert any("keyword conflict" in item for item in warnings)
    assert any("does not exist" in item for item in warnings)


def test_capability_gap_injects_related_memory_route_paths(tmp_path):
    """LLM: verify capability gaps pull matching long-term rules into the task context manifest.

    给人看的解释：
    子代理上报 capability gap 以后，父链路应该把相关长期规则路径补进 required_read_paths，
    而不是只留下一个抽象缺口。
    """

    root = tmp_path
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), root)
    # 路由规则属于 owner 的 canonical workspace，测试也不能再把临时启动根目录
    # 冒充用户家目录；否则会掩盖从 /root 启动 TUI 时的真实越界风险。
    workspace_root = agent.subagents.workspace_root
    rule = workspace_root / "memory" / "routing" / "rules" / "auth.md"
    rule.parent.mkdir(parents=True, exist_ok=True)
    rule.write_text("能力缺口涉及鉴权时，先读取这份长期规则。", encoding="utf-8")
    (workspace_root / "memory" / "routing" / "INDEX.md").write_text(
        """# Memory Routes

## route.auth
topic: API Auth
trigger_keywords: authenticated_api_check, 登录态
source_file: memory/routing/rules/auth.md
inject_mode: on_hit
priority: 40
""",
        encoding="utf-8",
    )

    task = agent.subagents.create_run(goal="检查 API 调用", thought="需要鉴权能力。", plan=["分析"])

    gap = agent.subagents.lifecycle.record_capability_gap(
        task.id,
        RecordCapabilityGapParams(
            missing_capability="authenticated_api_check",
            why_failed="缺少登录态和安全授权。",
        ),
    )
    reloaded = agent.subagents.load(task.id)

    assert gap.memory_routes
    assert "memory/routing/rules/auth.md" in gap.injected_rule_paths
    assert "memory/routing/rules/auth.md" in reloaded.context_manifest.required_read_paths
