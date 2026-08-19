from __future__ import annotations

from dataclasses import replace

from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_terminal import TuiTerminalTitleController
from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore


class _FakeOutput:
    def __init__(self) -> None:
        self.titles: list[str] = []
        self.clear_count = 0

    def set_title(self, value: str) -> None:
        self.titles.append(value)

    def clear_title(self) -> None:
        self.clear_count += 1


def test_terminal_title_uses_first_user_prompt_and_running_animation() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("title", clock=lambda: 10.0)
    store.publish(
        seq.emit(
            "user_message",
            "completed",
            "user",
            {"text": "first prompt\nsecond line\x1b]2;bad"},
        )
    )
    output = _FakeOutput()
    controller = TuiTerminalTitleController("my-agent", clock=lambda: 0.0)

    idle = controller.update(output, store.snapshot())
    running_snapshot = replace(
        store.snapshot(),
        status=replace(store.snapshot().status, phase="running"),
    )
    running = controller.update(output, running_snapshot)

    assert idle == "✳ first prompt"
    assert running == "⠂ first prompt"
    assert output.titles == [idle, running]


def test_terminal_title_deduplicates_and_clears() -> None:
    store = TuiStateStore()
    output = _FakeOutput()
    controller = TuiTerminalTitleController("my-agent", clock=lambda: 0.0)

    assert controller.update(output, store.snapshot()) == "✳ my-agent"
    controller.update(output, store.snapshot())
    controller.clear(output)

    assert output.titles == ["✳ my-agent"]
    assert output.clear_count == 1
