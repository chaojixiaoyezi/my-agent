from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_loop.background_liveness import is_wake_capable_source


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("background_main_agent", True),
        ("gateway", True),
        ("chat", True),
        ("cli_run", False),
        ("run", False),
        ("", False),
    ],
)
def test_is_wake_capable_source(source: str, expected: bool) -> None:
    assert is_wake_capable_source(SimpleNamespace(source=source)) is expected
