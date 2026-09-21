import pytest
from agent.command_catalog import system_slash_command_name
from agent.conversation.control_commands import (
    ConversationControlResult,
    ConversationTaskStatus,
    conversation_task_attributes,
    parse_conversation_control,
    parse_conversation_task_command,
    render_conversation_task_status,
)


def test_parse_conversation_controls_are_explicit() -> None:
    status = parse_conversation_control("/status")
    steer = parse_conversation_control("/btw 先不要改代码，先核对事实")
    stop = parse_conversation_control("/stop")

    assert status is not None and status.kind == "status" and status.valid
    assert steer is not None and steer.kind == "steer" and steer.valid
    assert steer.value == "先不要改代码，先核对事实"
    assert stop is not None and stop.kind == "stop" and stop.valid
    assert parse_conversation_control("先停一下") is None


def test_interrupt_is_typed_turn_stop_not_goal_pause() -> None:
    from agent_py_agent.cli.chat_parts.control_runtime import _command_text

    command = parse_conversation_control("/interrupt")
    assert command.valid and command.kind == "stop" and command.operation == "interrupt"
    assert _command_text(command) == "/interrupt"
    assert _command_text(parse_conversation_control("/stop")) == "/stop"
    assert not parse_conversation_control("/interrupt someone").valid
    assert parse_conversation_control("打断一下，我补充要求") is None


def test_parse_context_compact_and_effort_commands() -> None:
    context = parse_conversation_control("/context")
    compact = parse_conversation_control("/compact 优先保留未完成事项")
    effort = parse_conversation_control("/effort high")
    effort_status = parse_conversation_control("/effort status")
    invalid_effort = parse_conversation_control("/effort extreme")

    assert context is not None and context.kind == "context" and context.valid
    assert compact is not None and compact.kind == "compact" and compact.valid
    assert compact.value == "优先保留未完成事项"
    assert effort is not None and effort.kind == "effort" and effort.value == "high"
    assert effort.operation == "set" and effort.valid
    assert effort_status is not None and effort_status.operation == "view"
    assert invalid_effort is not None and invalid_effort.valid is False
    assert parse_conversation_control("/context extra").valid is False
    assert parse_conversation_control("请帮我 compact 一下") is None


def test_parse_goal_lifecycle_commands() -> None:
    view = parse_conversation_control("/goal")
    create = parse_conversation_control("/goal 连续整理一周资料")
    pause = parse_conversation_control("/goal pause")
    resume = parse_conversation_control("/goal resume")
    clear = parse_conversation_control("/goal clear")
    edit = parse_conversation_control("/goal edit 改为每天整理一次")

    assert view is not None and view.kind == "goal" and view.operation == "view"
    assert create is not None and create.operation == "create" and create.value == "连续整理一周资料"
    assert pause is not None and pause.operation == "pause" and pause.valid
    assert resume is not None and resume.operation == "resume" and resume.valid
    assert clear is not None and clear.operation == "clear" and clear.valid
    assert edit is not None and edit.operation == "edit" and edit.value == "改为每天整理一次"
    assert parse_conversation_control("/goal edit").valid is False
    assert parse_conversation_control("我有一个 goal") is None


def test_verbose_and_unknown_slash_commands_are_typed_system_inputs() -> None:
    verbose = parse_conversation_control("/verbose full")
    alias = parse_conversation_control("/v")
    unknown = parse_conversation_control("/future-mode on", reject_unknown_slash=True)

    assert verbose is not None and verbose.kind == "verbose" and verbose.value == "full"
    assert alias is not None and alias.kind == "verbose" and alias.operation == "view"
    assert unknown is not None and unknown.kind == "unsupported" and not unknown.valid
    assert system_slash_command_name("/usr/bin/python") == ""
    assert system_slash_command_name("请运行 /stop") == ""


@pytest.mark.parametrize("raw", [
    "/plugins@", "/plugins@Demo", "/PLUGINS@Demo help",
    '/plugins@demo run --path "C:\\new folder\\中文.txt" -- -x | literal',
    "/plugins@../../invalid", "/plugins@@demo", "/plugins@demo\n参数",
])
def test_reserved_plugin_namespace_never_becomes_chat_or_stop(raw: str) -> None:
    assert system_slash_command_name(raw).startswith("plugins@")
    command = parse_conversation_control(raw, reject_unknown_slash=True)
    assert command is not None
    assert command.kind == "unsupported" and not command.valid
    assert "尚未开放" in command.usage
    assert parse_conversation_task_command(raw) is None


@pytest.mark.parametrize("raw", [
    "请解释 /plugins@Demo 的用途", "/plugins/tools.py", "`` `/plugins@Demo` ``", "/usr/bin/python",
])
def test_plugin_namespace_does_not_capture_ordinary_text_or_paths(raw: str) -> None:
    assert system_slash_command_name(raw) == ""
    assert parse_conversation_control(raw, reject_unknown_slash=True) is None


@pytest.mark.parametrize(("raw", "kind", "value", "valid"), [
    ("/compact保留末尾\n以及引用", "compact", "保留末尾\n以及引用", True),
    (r'/COMPACT  保留 "C:\new folder\中文.txt" | $literal --', "compact",
     r'保留 "C:\new folder\中文.txt" | $literal --', True),
    (r'/goal 整理 "C:\new folder" -- 不解释 | 符号', "goal",
     r'整理 "C:\new folder" -- 不解释 | 符号', True),
    (r'/btw 保留 "a b" 和 C:\new\file -- |', "steer",
     r'保留 "a b" 和 C:\new\file -- |', True),
    ("/btw 第一行\n第二行", "steer", "", False),
    ("/btw-extra", "steer", "", False),
    ("/effort HIGH", "effort", "high", True),
    ("/V FULL", "verbose", "full", True),
])
def test_core_command_body_boundaries_stay_unchanged(
    raw: str, kind: str, value: str, valid: bool,
) -> None:
    command = parse_conversation_control(raw)
    assert command is not None
    assert (command.kind, command.value, command.valid) == (kind, value, valid)


def test_audit_prepare_strips_protocol_without_activating_guarantee() -> None:
    command = parse_conversation_task_command("/audit 安全巡检 prepare 逐条检查这些来源")

    assert command is not None and command.valid
    assert command.prompt == "逐条检查这些来源"
    assert conversation_task_attributes(command.to_request_payload()) == {
        "conversation_audit_prepare": True,
        "conversation_work_kind": "audit",
        "conversation_work_name": "安全巡检",
        "conversation_cancellation_scope": "foreground",
    }
    assert parse_conversation_task_command("/audit") is None
    invalid = parse_conversation_control("/audit")
    assert invalid is not None and invalid.valid is False


def test_named_audit_and_goal_commands_have_exact_structured_selectors() -> None:
    audit = parse_conversation_control(
        "/audit 30d 安全巡检 逐条检查这五路来源"
    )
    audit_clear = parse_conversation_control("/audit 安全巡检 clear")
    audit_status = parse_conversation_control("/audit 安全巡检 status")
    audit_resume = parse_conversation_control("/audit 安全巡检 resume")
    audit_help = parse_conversation_control("/audit help")
    goal = parse_conversation_control(
        "/goal 7d 周报整理 整理并核对本周资料"
    )
    goal_clear = parse_conversation_control("/goal 周报整理 clear")

    assert audit is not None and audit.valid and audit.value == "逐条检查这五路来源"
    assert audit.kind == "audit" and audit.operation == "start"
    assert audit.name == "安全巡检"
    assert audit.duration_seconds == 30 * 86400
    assert parse_conversation_task_command(
        "/audit 30d 安全巡检 逐条检查这五路来源"
    ) is None
    assert audit_clear is not None and audit_clear.kind == "audit"
    assert audit_clear.operation == "clear" and audit_clear.name == "安全巡检"
    assert audit_status is not None and audit_status.operation == "status"
    assert audit_status.name == "安全巡检"
    assert audit_resume is not None and audit_resume.operation == "resume"
    assert audit_resume.name == "安全巡检"
    assert audit_help is not None and audit_help.operation == "help"
    assert goal is not None and goal.operation == "create"
    assert goal.name == "周报整理" and goal.duration_seconds == 7 * 86400
    assert goal.value == "整理并核对本周资料"
    assert goal_clear is not None and goal_clear.operation == "clear"
    assert goal_clear.name == "周报整理"
    reserved_name_clear = parse_conversation_control("/goal pause clear")
    assert reserved_name_clear is not None
    assert reserved_name_clear.operation == "clear"
    assert reserved_name_clear.name == "pause"


def test_parse_btw_has_no_list_or_clear_mode() -> None:
    missing = parse_conversation_control("/btw")
    removed = parse_conversation_control("/btw-clear")

    assert missing is not None and not missing.valid
    assert removed is not None and not removed.valid
    assert missing.usage == "用法：/btw 你的补充要求"
    assert removed.usage == missing.usage


def test_status_rejects_trailing_arguments() -> None:
    status = parse_conversation_control("/status 最近引导")
    stop = parse_conversation_control("/stop all")

    assert status is not None and not status.valid
    assert stop is not None and not stop.valid


def test_render_status_has_runtime_facts_without_guidance_history() -> None:
    rendered = render_conversation_task_status(
        ConversationTaskStatus(
            state="running",
            task="整理今天的资料",
            elapsed_seconds=125,
            queued_count=1,
            recent_progress="刚完成一个执行步骤",
            subagent_total=2,
            subagent_running=1,
            subagent_done=1,
            model_name="MiniMax-M2.7",
            compact_generation=2,
            verbose_level="on",
        )
    )

    assert "状态：运行中" in rendered
    assert "已运行：2分5秒" in rendered
    assert "子代理 2（运行 1，完成 1，异常 0）" in rendered
    assert "上下文：已压缩 2 次" in rendered
    assert "引导" not in rendered


def test_render_status_redacts_host_paths_at_the_shared_control_boundary() -> None:
    rendered = render_conversation_task_status(
        ConversationTaskStatus(
            state="running",
            task="继续处理 /root/.my-agent/owners/alice/tasks/report/output.csv",
            recent_progress="刚写入 /Users/alice/project/private/result.json",
        )
    )

    assert "/root/" not in rendered
    assert "/Users/" not in rendered
    assert "output.csv" in rendered
    assert "result.json" in rendered


def test_control_result_redacts_structured_status_at_the_shared_boundary() -> None:
    result = ConversationControlResult(
        "status",
        True,
        "状态：运行中",
        status=ConversationTaskStatus(
            state="running",
            task="继续处理 /root/.my-agent/owners/alice/tasks/report/output.csv",
            recent_progress="刚写入 /Users/alice/project/private/result.json",
        ),
    )

    payload = result.to_dict()
    task_status = payload["task_status"]

    assert isinstance(task_status, dict)
    assert "/root/" not in str(task_status["task"])
    assert "/Users/" not in str(task_status["recent_progress"])
    assert "output.csv" in str(task_status["task"])
    assert "result.json" in str(task_status["recent_progress"])
