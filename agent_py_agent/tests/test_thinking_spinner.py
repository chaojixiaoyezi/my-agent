"""思考指示器测试。"""

import sys
import time

from agent_py_agent.cli.thinking_phrases import PHRASES, random_phrase
from agent_py_agent.cli.thinking_spinner import ThinkingSpinner


def test_random_phrase_returns_string():
    phrase = random_phrase()
    assert isinstance(phrase, str)
    assert 2 <= len(phrase) <= 15


def test_all_phrase_groups_have_entries():
    for group, phrases in PHRASES.items():
        assert len(phrases) > 0, f"group {group} is empty"
        for p in phrases:
            assert isinstance(p, str) and len(p) >= 2, f"bad phrase in {group}: {p!r}"


def test_spinner_disabled_when_not_tty(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    spinner = ThinkingSpinner()
    assert spinner._enabled is False
    spinner.start()
    assert spinner._running is False
    spinner.stop()


def test_spinner_explicitly_disabled():
    spinner = ThinkingSpinner(enabled=False)
    spinner.start()
    assert spinner._running is False
    spinner.stop()


def test_spinner_stop_is_idempotent():
    spinner = ThinkingSpinner(enabled=False)
    spinner.stop()
    spinner.stop()


def test_spinner_context_manager(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    with ThinkingSpinner() as spinner:
        assert spinner._enabled is False
