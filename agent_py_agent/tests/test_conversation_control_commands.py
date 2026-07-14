from agent.conversation.control_commands import (
    ConversationTaskStatus,
    parse_conversation_control,
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
            compact_generation=1,
            verbose_level="on",
        )
    )

    assert "状态：运行中" in rendered
    assert "已运行：2分5秒" in rendered
    assert "子代理 2（运行 1，完成 1，异常 0）" in rendered
    assert "上下文：已压缩 1 次" in rendered
    assert "引导" not in rendered
