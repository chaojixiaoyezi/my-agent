"""Basic memory runtime integration tests.
记忆运行时基础集成测试。"""

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive import (
    CompressionSnapshot,
    RawMemoryEvent,
    append_raw_event,
    append_snapshot,
)


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


# LLM: CaptureBackend lets compact auto-continuation tests inspect each model prompt without network calls.
# 类用途: 测试后端，记录每次模型请求 prompt，并返回可区分第几轮的响应。
class CaptureBackend:
    name = "capture"

    # LLM: CaptureBackend.__init__ keeps prompt capture state isolated per test.
    # 函数用途: 初始化 prompt 记录列表，供断言自动续跑是否真的发起第二轮模型调用。
    def __init__(self):
        self.prompts: list[str] = []

    # LLM: CaptureBackend.generate records prompt text and returns deterministic model output.
    # 函数用途: 模拟模型响应，不调用外部 API；响应文本带轮次，方便区分原始轮和续跑轮。
    def generate(self, prompt: str, on_chunk=None):
        self.prompts.append(prompt)
        return ModelResponse(text=f"capture response {len(self.prompts)}", backend=self.name)


def test_run_injects_routed_memory_authority_context(tmp_path):
    """LLM: Tests that agent.run() injects routed memory authority context when routing is enabled."""
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
    """LLM: Tests that agent.run() writes raw archive events when save=True."""
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


def test_run_surfaces_compact_suggestion_without_auto_apply(tmp_path):
    """LLM: Tests that agent.run() can suggest compact without running apply automatically."""
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    agent.config.memory_compact_context_window_tokens = 20

    result = agent.run("请生成足够长的 compact 提示触发内容", save=False)

    assert result.memory_compact_suggested is True
    assert result.memory_compact_status in {"suggest_compact", "artifact_guard", "stop_required"}
    assert result.memory_compact_commands
    assert "memory-compact" in result.memory_compact_commands[0]
    assert result.memory_compact_auto_status == "needs_user_confirmation"
    assert result.memory_compact_auto_next_action == "ask_user_before_apply"
    assert result.memory_compact_auto_tool_execution == "none"
    assert result.memory_compact_auto_allowed_to_continue is False
    assert result.memory_compact_auto_apply_id == ""
    assert result.memory_compact_auto_continue_ready is False
    assert not (tmp_path / "memory_archive" / "compact_applies").exists()


def test_run_auto_compact_apply_continues_once_after_continue_packet(tmp_path):
    """LLM: Tests opt-in auto compact apply performs one guarded continuation turn."""
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    agent.config.memory_compact_context_window_tokens = 20
    agent.config.memory_compact_auto_allow_apply = True

    result = agent.run(
        "验收: auto compact packet exists\n约束: no automatic tool execution\n测试: focused compact runtime test",
        save=True,
        request_id="req-auto-compact",
        run_id="run-auto-compact",
        task_id="run-auto-compact",
        recovery_next_actions=["continue only after reading continue packet"],
    )

    assert result.memory_compact_auto_status == "skipped_after_guarded_continuation"
    assert result.memory_compact_auto_next_action == "continue_without_compact"
    assert result.memory_compact_auto_allowed_to_continue is False
    assert result.memory_compact_auto_continue_ready is False
    assert result.memory_compact_auto_tool_execution == "none"
    assert result.memory_compact_auto_continued is True
    assert result.memory_compact_auto_continued_from_apply_id
    assert "# Compact Auto Continuation" in result.prompt
    assert (tmp_path / "memory_archive" / "compact_applies").exists()


# LLM: auto compact continuation should turn a ready continue packet into the next guarded model turn.
# 函数用途: 验证主 agent 自动 compact apply 后会把继续包注入下一轮 prompt，并重新带上家目录关键文件。
def test_run_auto_compact_apply_continues_with_home_entries_and_packet(tmp_path):
    home = tmp_path / "home"
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(home)), tmp_path)
    backend = CaptureBackend()
    agent.backend = backend
    agent.config.memory_compact_context_window_tokens = 20
    agent.config.memory_compact_auto_allow_apply = True
    agent.home_paths.agents_md.write_text("执行制度：每轮先读 AGENTS。\n", encoding="utf-8")
    agent.home_paths.soul_md.write_text("人格：稳住状态继续干。\n", encoding="utf-8")
    agent.home_paths.user_md.write_text("用户偏好：大白话汇报。\n", encoding="utf-8")
    agent.home_paths.memory_md.write_text("关键记忆：不要重做已完成步骤。\n", encoding="utf-8")

    result = agent.run(
        "验收: continuation packet injected\n约束: do not redo completed work\n测试: focused compact auto continuation",
        save=True,
        request_id="req-auto-continuation",
        run_id="run-auto-continuation",
        task_id="run-auto-continuation",
        recovery_next_actions=["continue from compact next_step"],
    )

    assert len(backend.prompts) == 2
    assert result.response == "capture response 2"
    assert result.memory_compact_auto_continued is True
    assert result.memory_compact_auto_continued_from_apply_id
    second_prompt = backend.prompts[1]
    assert "# Home Entry: AGENTS.md" in second_prompt
    assert "# Home Entry: SOUL.md" in second_prompt
    assert "# Home Entry: USER.md" in second_prompt
    assert "# Home Entry: memory.md" in second_prompt
    assert second_prompt.index("# Home Entry: AGENTS.md") < second_prompt.index("# Compact Auto Continuation")
    assert "continue from compact next_step" in second_prompt
    assert "Do not redo completed work" in second_prompt


# LLM: blocked compact resumes must not trigger an automated second model turn.
# 函数用途: 验证缺少验收/约束/测试/next_step 等字段时，自动 compact 只停车，不自动续跑。
def test_run_auto_compact_apply_does_not_continue_when_guard_blocks(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    backend = CaptureBackend()
    agent.backend = backend
    agent.config.memory_compact_context_window_tokens = 20
    agent.config.memory_compact_auto_allow_apply = True

    result = agent.run("请生成足够长的 compact 提示触发内容", save=True, request_id="req-blocked-continuation")

    assert len(backend.prompts) == 1
    assert result.memory_compact_auto_continued is False
    assert result.memory_compact_auto_continued_from_apply_id == ""


def test_run_no_save_blocks_opt_in_auto_compact_apply(tmp_path):
    """LLM: Tests that save=False remains a hard persistence boundary for auto compact apply."""
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    agent.config.memory_compact_context_window_tokens = 20
    agent.config.memory_compact_auto_allow_apply = True

    result = agent.run(
        "验收: auto compact packet exists\n约束: no persistence during no-save\n测试: focused compact runtime test",
        save=False,
        request_id="req-no-save-auto-compact",
        run_id="run-no-save-auto-compact",
        task_id="run-no-save-auto-compact",
    )

    assert result.memory_compact_suggested is True
    assert result.memory_compact_auto_status == "needs_user_confirmation"
    assert result.memory_compact_auto_next_action == "ask_user_before_apply"
    assert result.memory_compact_auto_allowed_to_continue is False
    assert result.memory_compact_auto_apply_id == ""
    assert not (tmp_path / "memory_archive" / "compact_applies").exists()


def test_run_no_save_does_not_write_raw_archive(tmp_path):
    """LLM: Tests that agent.run() with save=False does not write any raw archive files."""
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)

    result = agent.run("不要归档这轮对话", save=False)

    assert result.archive_events == 0
    assert result.recovery_snapshot_path == ""
    assert not (tmp_path / "memory" / "raw").exists()
    assert not (tmp_path / "memory" / "hooks").exists()


def test_run_can_force_recovery_snapshot_without_raw_archive(tmp_path):
    """LLM: Tests that recovery_snapshot=True forces a snapshot without writing raw archive events."""
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


def test_auto_resume_context_is_disabled_by_default(tmp_path):
    """LLM: Tests that auto resume context is disabled by default in agent config."""
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    agent.run("README 恢复上下文任务", save=True)

    result = agent.run("继续 README", save=False)

    assert result.memory_resume_context_injected is False
    assert "### Auto Recovery Context" not in result.prompt


def test_auto_resume_context_injects_on_trigger_when_enabled(tmp_path):
    """LLM: Tests that auto resume context injects recovery context when enabled and triggered."""
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            memory_resume_auto_context_enabled=True,
            memory_resume_auto_context_limit=3,
        ),
        tmp_path,
    )
    agent.run("README 恢复上下文任务", save=True, request_id="request-auto-1")

    result = agent.run("继续 README", save=False)

    assert result.memory_resume_context_injected is True
    assert result.memory_resume_context_query == "README"
    assert result.memory_resume_context_matches >= 1
    assert "### Auto Recovery Context" in result.prompt
    assert "# Recovery Brief" in result.prompt
    assert "latest_user_intent: README 恢复上下文任务" in result.prompt
    assert result.memory_resume_context_error == ""


def test_auto_resume_context_can_be_enabled_per_run(tmp_path):
    """LLM: Tests that auto resume context can be enabled per-run with resume_context=True."""
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    agent.run("README 临时恢复开关任务", save=True, request_id="request-auto-override")

    result = agent.run("继续 README", save=False, resume_context=True)

    assert result.memory_resume_context_injected is True
    assert result.memory_resume_context_token_estimate > 0
    assert result.prompt_token_estimate >= result.memory_resume_context_token_estimate


def test_auto_resume_context_can_be_disabled_per_run(tmp_path):
    """LLM: Tests that auto resume context can be disabled per-run with resume_context=False override."""
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", memory_resume_auto_context_enabled=True),
        tmp_path,
    )
    agent.run("README 禁用恢复开关任务", save=True)

    result = agent.run("继续 README", save=False, resume_context=False)

    assert result.memory_resume_context_injected is False
    assert "### Auto Recovery Context" not in result.prompt
