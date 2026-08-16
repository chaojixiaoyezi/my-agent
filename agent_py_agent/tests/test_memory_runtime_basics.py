"""Basic memory runtime integration tests.
记忆运行时基础集成测试。"""

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.agent_core._finalization_service import FinalizationService
from agent_py_agent.agent.agent_core._runtime_params import ArchiveRunParams, EstimateTokenParams
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderContextWindowError
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive import (
    CompressionSnapshot,
    RawMemoryEvent,
    append_raw_event,
    append_snapshot,
    has_resume_trigger,
)
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.settings import AgentConfig


def _promote_formal_route(agent: SimpleAgent) -> str:
    candidate = None
    for task_id in ("task-route-a", "task-route-b"):
        candidate = agent.memory_candidates.observe(
            CandidateObservation(
                candidate_type="lesson",
                content="长期规则正文：回答 memory routing 问题时先核对正式 lesson。",
                subject_key="memory.routing",
                scope=MemoryScope("global", "global"),
                origin="subagent_lesson",
                evidence_refs=({"ref_id": task_id, "kind": "task"},),
                source_task_ids=(task_id,),
                observation_id=f"route-observation:{task_id}",
                promotion_target="lesson",
            )
        )
    assert candidate is not None
    agent.memory_promotion.review(
        candidate.candidate_id,
        approved=True,
        reviewer="runtime-test",
    )
    result = agent.memory_promotion.promote(
        candidate.candidate_id,
        reviewer="runtime-test",
    )
    assert result.promoted is True
    return result.promotion_ref.split("#", 1)[0]


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _test_config(tmp_path: Path, **kwargs) -> AgentConfig:
    kwargs.setdefault("tool_protocol", "text")
    return AgentConfig(my_agent_home=str(tmp_path / "home"), **kwargs)


class RuntimeOverflowBackend:
    name = "runtime-overflow"

    def generate(self, prompt: str, on_chunk=None):
        return ModelResponse(
            text="context overflow response",
            backend=self.name,
            runtime_status="blocked",
            runtime_reason="context_overflow",
        )


class SequenceUsageBackend:
    name = "sequence-usage"
    context_window_tokens = 20_000

    def __init__(self, usages: list[dict[str, int]]):
        self.usages = usages
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None):
        self.prompts.append(prompt)
        usage = self.usages[min(len(self.prompts) - 1, len(self.usages) - 1)]
        return ModelResponse(text=f"usage response {len(self.prompts)}", backend=self.name, usage=usage)


class RaisingContextBackend:
    name = "raising-context"
    context_window_tokens = 100_000

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None):
        self.calls += 1
        raise ProviderContextWindowError("maximum context length exceeded")


class NeverCalledBackend:
    name = "never-called"
    context_window_tokens = 20

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None):
        self.calls += 1
        raise AssertionError("backend should not be called after preflight overflow")


def test_run_injects_only_formal_routed_lesson_in_memory_envelope(tmp_path):
    """LLM: Tests runtime injects only formal routed lesson through the one Memory envelope."""
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            memory_rule_routing_enabled=True,
            memory_rule_routing_mode="soft",
            memory_rule_auto_read_limit=1,
        ),
        tmp_path,
    )
    lesson_path = _promote_formal_route(agent)

    result = agent.run("请按长期规则处理 memory index", save=False)

    assert result.memory_route_matches == 1
    assert result.memory_route_paths == [lesson_path]
    assert "### Routed memory authority:" not in result.prompt
    assert result.prompt.count("<memory-context") == 1
    assert "长期规则正文" in result.prompt
    assert result.archive_events == 0


def test_run_writes_raw_archive_when_saved(tmp_path):
    """LLM: Tests that agent.run() writes raw archive events when save=True."""
    agent = SimpleAgent(_test_config(tmp_path, model_backend="echo"), tmp_path)

    result = agent.run("请归档这轮对话", save=True, request_id="req-archive-save")

    raw_dir = Path(agent.home_paths.owner_audit_dir)
    fact_path = Path(agent.home_paths.owner_home_dir) / "memory_archive" / "runtime_facts" / "req-archive-save" / "task.json"
    files = sorted(raw_dir.glob("*.jsonl"))
    assert result.archive_events == 2
    assert result.archive_token_estimate > 0
    assert result.recovery_snapshot_path == ""
    assert fact_path.exists()
    assert len(files) == 1
    records = _read_jsonl(files[0])
    assert [record["speaker"] for record in records] == ["user", "assistant"]
    assert records[0]["content_preview"] == "请归档这轮对话"
    assert records[1]["action"] == "response"
    facts = json.loads(fact_path.read_text(encoding="utf-8"))
    assert facts["goal"] == "请归档这轮对话"
    assert facts["runtime_progress"]["phase"] == "final"


def test_saved_empty_model_response_is_archived_without_empty_long_term_memory(tmp_path):
    agent = SimpleAgent(_test_config(tmp_path, model_backend="echo"), tmp_path)
    service = FinalizationService(agent)

    archived = service._archive_run_if_needed(
        ArchiveRunParams(
            do_save=True,
            user_prompt="保留用户请求，但模型没有生成正文",
            final_response=ModelResponse(text="", backend="test"),
            archive_tool_calls=[],
            run_request_id="req-empty-response",
            run_id="run-empty-response",
            task_id="task-empty-response",
            source="test",
            task_attributes=None,
        )
    )

    assert archived is not None
    assert agent.memory.all() == []
    audit_files = sorted(Path(agent.home_paths.owner_audit_dir).glob("*.jsonl"))
    assert audit_files
    assert any(
        record.get("content_preview") == "保留用户请求，但模型没有生成正文"
        for record in _read_jsonl(audit_files[0])
    )


def test_provider_saved_run_writes_only_owner_task_workspace(tmp_path):
    agent = SimpleAgent(
        _test_config(
            tmp_path,
            model_backend="echo",
            my_agent_owner_provider="feishu",
            my_agent_owner_kind="user",
            my_agent_owner_id="ou_123",
        ),
        tmp_path / "workspace",
    )

    agent.run("请保存 provider 任务", save=True, request_id="req-provider-task", run_id="run-provider-task")

    owner_states = sorted((Path(agent.home_paths.owner_home_dir) / "tasks").glob("*/*/work/state.json"))
    owner_task_state = owner_states[0]
    assert owner_task_state.exists()
    assert not (Path(agent.home_paths.root) / "tasks").exists()
    state = json.loads(owner_task_state.read_text(encoding="utf-8"))
    assert state["status"] == "DONE"
    assert state["verification_status"] == "UNVERIFIED"
    assert state["runtime_status"] == "ok"
    assert state["progress"] == 1.0
    index_path = Path(agent.home_paths.global_index_active_tasks_jsonl)
    records = _read_jsonl(index_path)
    assert records[-1]["task_path"] == str(owner_task_state.parents[1])
    assert records[-1]["status"] == "done"


def test_run_surfaces_compact_suggestion_without_persistence(tmp_path):
    """LLM: Tests save=False keeps compact read-only while saved runs auto-apply."""
    agent = SimpleAgent(AgentConfig(model_backend="echo", tool_protocol="text"), tmp_path)
    agent.backend.context_window_tokens = 20

    result = agent.run("请生成足够长的 compact 提示触发内容", save=False)

    assert result.memory_compact_suggested is True
    assert result.memory_compact_status == "forced_compact"
    assert result.memory_compact_trigger_source == "preflight"
    assert result.memory_compact_commands
    assert "memory-compact" in result.memory_compact_commands[0]
    assert result.memory_compact_auto_status == "needs_user_confirmation"
    assert result.memory_compact_auto_next_action == "ask_user_before_apply"
    assert result.memory_compact_auto_tool_execution == "none"
    assert result.memory_compact_auto_allowed_to_continue is False
    assert result.memory_compact_auto_apply_id == ""
    assert result.memory_compact_auto_continue_ready is False
    assert not (tmp_path / "memory_archive" / "compact_applies").exists()


def test_run_context_overflow_uses_same_compact_cycle_even_below_threshold(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", tool_protocol="text"), tmp_path)
    agent.backend = RuntimeOverflowBackend()

    result = agent.run("普通短任务，但后端报告上下文溢出", save=False)

    assert result.runtime_reason == "context_overflow"
    assert result.memory_compact_suggested is True
    assert result.memory_compact_status == "forced_compact"
    assert result.memory_compact_trigger_reason == "provider_context_overflow"
    assert result.memory_compact_trigger_source == "runtime_status"
    assert result.memory_compact_trigger_forced is True
    assert result.memory_compact_auto_status == "needs_user_confirmation"
    assert result.memory_compact_auto_apply_id == ""


def test_run_context_error_uses_same_compact_cycle(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", tool_protocol="text"), tmp_path)
    backend = RaisingContextBackend()
    agent.backend = backend

    result = agent.run("普通短任务，但 provider 抛上下文过长错误", save=False)

    assert backend.calls == 1
    assert result.runtime_reason == "context_overflow"
    assert result.memory_compact_status == "forced_compact"
    assert result.memory_compact_trigger_reason == "provider_context_overflow"


def test_run_preflights_prompt_over_context_before_provider_call(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", tool_protocol="text"), tmp_path)
    backend = NeverCalledBackend()
    agent.backend = backend

    result = agent.run("请处理这段很长的内容：" + ("长内容" * 500), save=False)

    assert backend.calls == 0
    assert result.runtime_reason == "context_overflow"
    assert result.memory_compact_status == "forced_compact"
    assert result.memory_compact_trigger_source == "preflight"


def test_run_uses_provider_usage_for_active_compact_budget_not_cumulative(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", prompt_files=[], tool_protocol="text"),
        tmp_path,
    )
    backend = SequenceUsageBackend(
        [
            {"input_tokens": 19_000, "output_tokens": 200},
            {"input_tokens": 500, "output_tokens": 200},
        ]
    )
    agent.backend = backend

    first = agent.run("第一轮很大，但这里由 provider usage 表示真实输入。", save=False)
    second = agent.run("第二轮很小，不应因为历史累计 token 再次触发 compact。", save=False)

    assert 19_200 <= first.turn_token_estimate < 19_300
    assert first.memory_compact_suggested is True
    assert 700 <= second.turn_token_estimate < 800
    assert second.cumulative_token_estimate >= first.turn_token_estimate + second.turn_token_estimate
    assert second.memory_compact_suggested is False


def test_active_compact_budget_excludes_full_archive_tool_history(tmp_path):
    agent = SimpleAgent(_test_config(tmp_path, model_backend="echo"), tmp_path)
    service = FinalizationService(agent)
    archive_tool_calls = [
        {
            "tool": "read_file",
            "parameters": {"path": f"/tmp/source-{idx}.md"},
            "output": "x" * 2000,
        }
        for idx in range(40)
    ]

    ledger = service._estimate_token_usage(
        EstimateTokenParams(
            user_prompt="短任务",
            runtime_injections=[],
            memories=[],
            final_response=ModelResponse(text="完成", backend="test"),
            archive_tool_calls=archive_tool_calls,
            run_request_id="active-budget-archive-history",
            turn_id="active-budget-archive-history",
        )
    )

    assert ledger["turn"] > 10_000
    assert ledger["active"] < 1_000


def test_run_no_save_blocks_persistent_auto_compact_apply(tmp_path):
    """LLM: Tests that save=False remains a hard persistence boundary for auto compact apply."""
    agent = SimpleAgent(AgentConfig(model_backend="echo", tool_protocol="text"), tmp_path)
    agent.backend.context_window_tokens = 20

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
    agent = SimpleAgent(AgentConfig(model_backend="echo", tool_protocol="text"), tmp_path)

    result = agent.run("不要归档这轮对话", save=False)

    assert result.archive_events == 0
    assert result.recovery_snapshot_path == ""
    assert not (tmp_path / "audit").exists()
    assert not (tmp_path / "memory" / "hooks").exists()


def test_run_no_save_does_not_write_runtime_fact(tmp_path):
    """LLM: Tests that save=False does not write runtime fact sources."""
    agent = SimpleAgent(AgentConfig(model_backend="echo", tool_protocol="text"), tmp_path)

    result = agent.run(
        "子代理已完成，请写恢复锚点",
        save=False,
        request_id="req-1",
        run_id="subagent-1",
        task_id="subagent-1",
        source="subagent_run",
        recovery_content_paths=["subagents/subagent-1/STATUS.md"],
        recovery_next_actions=["读取 STATUS.md 后继续验收"],
    )

    assert result.archive_events == 0
    assert result.recovery_snapshot_path == ""
    assert not (tmp_path / "audit").exists()
    assert not (tmp_path / "memory" / "hooks").exists()
    assert not (tmp_path / "memory_archive" / "runtime_facts").exists()


def test_saved_run_runtime_fact_keeps_delivery_contract_outputs(tmp_path):
    agent = SimpleAgent(_test_config(tmp_path, model_backend="echo"), tmp_path)
    requested = tmp_path / "requested-output" / "report.md"
    contract = {
        "schema_version": "delivery_contract.v1",
        "artifacts": [
            {
                "artifact_id": "user_requested_report_md",
                "kind": "md",
                "preferred_path": str(requested),
                "allowed_output_roots": [str(requested.parent)],
                "required": True,
            }
        ],
    }

    agent.run(
        f"最后把报告写到 {requested}",
        save=True,
        request_id="req-contract-output",
        run_id="run-contract-output",
        task_id="task-contract-output",
        source="cli_run",
        delivery_contract=contract,
    )

    fact_path = next((tmp_path / "home").rglob("runtime_facts/req-contract-output/task.json"))
    payload = json.loads(fact_path.read_text(encoding="utf-8"))

    assert payload["desired_outputs"] == [
        {
            "artifact_id": "user_requested_report_md",
            "kind": "md",
            "target_path": str(requested),
        }
    ]
    assert payload["run_intent"]["desired_outputs"]["items"] == [str(requested)]


def test_saved_run_runtime_fact_keeps_target_coverage_contract(tmp_path):
    agent = SimpleAgent(_test_config(tmp_path, model_backend="echo"), tmp_path)
    contract = {
        "schema_version": "delivery_contract.v1",
        "artifacts": [
            {
                "artifact_id": "report",
                "kind": "md",
                "preferred_path": "outputs/report.md",
                "required": True,
            }
        ],
        "target_coverage_contract": {
            "coverage_requirement": "full_source_read",
            "enforcement": "required",
            "target_items": [
                {
                    "target_id": "data/source.txt",
                    "source_path": "data/source.txt",
                    "coverage_kind": "full_source_read",
                }
            ],
        },
    }

    agent.run(
        "完整读完 data/source.txt 后写报告。",
        save=True,
        request_id="req-contract-coverage",
        run_id="run-contract-coverage",
        task_id="task-contract-coverage",
        source="cli_run",
        delivery_contract=contract,
    )

    fact_path = next((tmp_path / "home").rglob("runtime_facts/req-contract-coverage/task.json"))
    payload = json.loads(fact_path.read_text(encoding="utf-8"))

    assert payload["delivery_contract"]["target_coverage_contract"]["coverage_requirement"] == "full_source_read"
    assert payload["target_coverage"]["target_items"][0]["source_path"] == "data/source.txt"


def test_auto_resume_context_is_disabled_by_default(tmp_path):
    """LLM: Tests that auto resume context is disabled by default in agent config."""
    agent = SimpleAgent(_test_config(tmp_path, model_backend="echo"), tmp_path)
    agent.run("README 恢复上下文任务", save=True)

    result = agent.run("继续 README", save=False)

    assert result.memory_resume_context_injected is False
    assert "### Auto Recovery Context" not in result.prompt


def test_continue_inside_new_cli_task_does_not_resume_old_task(tmp_path):
    """LLM: Tests that ordinary 'continue working' phrasing does not leak old CLI task memory."""
    agent = SimpleAgent(
        _test_config(tmp_path, model_backend="echo", memory_resume_auto_context_enabled=True),
        tmp_path,
    )
    agent.run(
        "我这里有个大文本文件：/tmp/huge-md5-100mb.txt。请找出里面的 MD5。",
        save=True,
        request_id="request-old-md5",
    )

    result = agent.run("请整理这个目录，内容很多也要继续往下做，最后写报告。", save=False)

    assert has_resume_trigger("请整理这个目录，内容很多也要继续往下做，最后写报告。") is False
    assert result.memory_resume_context_injected is False
    assert "huge-md5-100mb" not in result.prompt


def test_auto_resume_context_injects_when_always_mode_enabled(tmp_path):
    """LLM: Tests that auto resume context injects recovery context in always mode."""
    agent = SimpleAgent(
        _test_config(
            tmp_path,
            model_backend="echo",
            memory_resume_auto_context_enabled=True,
            memory_resume_auto_context_mode="always",
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
    agent = SimpleAgent(_test_config(tmp_path, model_backend="echo"), tmp_path)
    agent.run("README 临时恢复开关任务", save=True, request_id="request-auto-override")

    result = agent.run("继续 README", save=False, resume_context=True)

    assert result.memory_resume_context_injected is True
    assert result.memory_resume_context_token_estimate > 0
    assert result.prompt_token_estimate >= result.memory_resume_context_token_estimate


def test_auto_resume_context_can_be_disabled_per_run(tmp_path):
    """LLM: Tests that auto resume context can be disabled per-run with resume_context=False override."""
    agent = SimpleAgent(
        _test_config(tmp_path, model_backend="echo", memory_resume_auto_context_enabled=True),
        tmp_path,
    )
    agent.run("README 禁用恢复开关任务", save=True)

    result = agent.run("继续 README", save=False, resume_context=False)

    assert result.memory_resume_context_injected is False
    assert "### Auto Recovery Context" not in result.prompt
