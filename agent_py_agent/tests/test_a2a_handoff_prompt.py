# LLM: 锁定 A2A-LOSSLESS v1 交接事实条款的渲染边界：按实际动作取舍的软引导、普通文本标签行、协调者截断读取规则、
#   system 里的翻译规则与根会话转述规则；宿主不解析这些行，也不新增结果块或第二个 json 代码块。
# 模块用途: 验证子代理与根代理提示词中的 A2A 交接事实条款和人类边界条款。
from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runner import prompts
from agent_py_agent.agent.gateway_parts.request_prompt import _append_subagent_completions_prompt
from agent_py_agent.agent.subagents import SubAgentExecutionContext
from agent_py_agent.agent.subagents.runner_completion_payload import _completion_message

_FIELDS = (
    "delta_map",
    "decisions_and_assumptions",
    "unchecked_surfaces",
    "blast_radius",
    "invariants_verified",
    "falsification_check",
)
_BLAST_RADII = ("LOW", "MEDIUM", "HIGH-STATE-MACHINE", "HIGH-HOT-PATH", "HIGH-SCOPE-BOUNDARY")
_TRUNCATION_MARK = "完成消息已截断"


# 函数用途: 构造一个有直属父级的普通执行子代理上下文。
def _context(**overrides: object) -> SubAgentExecutionContext:
    values: dict[str, object] = {
        "run_id": "run-a2a-child",
        "generated_at": 1.0,
        "goal": "修复解析器并补测试",
        "thought": "",
        "plan": [],
        "agent_name": "worker",
        "role": "worker",
        "parent_id": "run-a2a-parent",
        "root_id": "run-a2a-root",
        "depth": 1,
        "task_dir": "/tmp/a2a-task",
        "allowed_tools": ["read_file", "write_file", "run_command"],
    }
    values.update(overrides)
    return SubAgentExecutionContext(**values)


# 函数用途: 构造一个可创建直属下级的协调子代理上下文。
def _coordinator_context() -> SubAgentExecutionContext:
    return _context(
        agent_name="lead",
        role="coordinator",
        role_template={"can_spawn_children": True},
        allowed_tools=["create_subagents", "list_agents", "read_file", "write_file"],
    )


def test_handoff_clause_lists_each_field_once_and_scales_by_actual_work() -> None:
    contract = prompts.prepare_subagent_runner_prompt(_context()).execution_contract

    assert "A2A-LOSSLESS v1" in contract
    for field in _FIELDS:
        assert contract.count(field) == 1, field
    for level in _BLAST_RADII:
        assert level in contract
    assert "在最终回复开头" in contract
    assert "只做了读取、检索或简单问答时直接作答，不写这些项" in contract
    assert "只写有内容的项" in contract


def test_handoff_clause_never_reintroduces_result_blocks() -> None:
    context = _context()
    contract = prompts.prepare_subagent_runner_prompt(context).execution_contract
    prompt = prompts._build_subagent_runner_prompt(context)

    for forbidden in ("SUBAGENT_RESULT", "状态 JSON", "```", "record_lesson"):
        assert forbidden not in contract
    assert prompt.count("SUBAGENT_RESULT") == 1
    assert prompt.count("```json\n") == 1
    runner_contract = prompt.index("## Runner Contract")
    clause = prompt.index("A2A-LOSSLESS v1")
    assert runner_contract < clause < prompt.index("## Context Bundle Gate")


def test_only_coordinators_get_the_truncated_completion_read_rule() -> None:
    worker = "\n".join(prompts._a2a_handoff_lines(_context()))
    coordinator = "\n".join(prompts._a2a_handoff_lines(_coordinator_context()))

    assert _TRUNCATION_MARK not in worker
    assert "final_report_ref" not in worker
    assert _TRUNCATION_MARK in coordinator
    assert "final_report_ref" in coordinator
    assert _TRUNCATION_MARK in prompts.prepare_subagent_runner_prompt(_coordinator_context()).execution_contract


def test_truncation_mark_matches_completion_projection_and_keeps_the_head() -> None:
    head = "delta_map：parser.py:10-20#parse(text: str) -> Result\n"
    task = SimpleNamespace(result=head + "很长的正文。" * 3000, latest_summary="")

    text, truncated, original_tokens = _completion_message(task, SimpleNamespace(message=""), {})

    assert truncated is True and original_tokens > 1000
    assert text.startswith(head)
    assert _TRUNCATION_MARK in text
    assert _TRUNCATION_MARK in "\n".join(prompts._a2a_handoff_lines(_coordinator_context()))


def test_system_prompt_explains_internal_records_in_natural_language_first() -> None:
    system = prompts.subagent_runner_system_prompt(_context())
    steer = "先在普通 assistant 回复中直接回答或确认用户"
    translate = "用户要求翻译或解释交接事实、工具结果等内部记录时，同样先用自然语言讲清楚，再回到原任务。"

    assert steer in system
    assert translate in system
    assert system.index(steer) < system.index(translate)


def test_prompt_budget_holds_with_handoff_clause() -> None:
    for context in (_context(), _coordinator_context()):
        assert len(prompts._build_subagent_runner_prompt(context)) < 12000


def test_root_completion_inputs_tell_root_to_read_full_report_and_translate() -> None:
    lines: list[str] = []
    _append_subagent_completions_prompt(lines, {"items": [{"task_id": "child-1", "completion_message": "done"}]})
    section = "\n".join(lines)

    assert _TRUNCATION_MARK in section
    assert "final_report_ref" in section
    assert "自然语言" in section
    for forbidden in ("output_json", "runner_result_json", "/private/"):
        assert forbidden not in section

    untouched: list[str] = []
    _append_subagent_completions_prompt(untouched, {})
    assert untouched == []
