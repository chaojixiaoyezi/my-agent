"""文本输入协议回归：替身只验证事件和失败边界，真实桌面验收仍必须经过 TUI。"""

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.tooling import computer_text_input as input_module


def test_macos_unicode_preserves_utf16_and_explicit_replacement(monkeypatch):
    events, keys = [], []
    keyboard = SimpleNamespace(
        failSafeCheck=lambda: None,
        hotkey=lambda *args: keys.append(args),
        press=lambda key: keys.append((key,)),
    )
    quartz = SimpleNamespace(
        kCGHIDEventTap=0,
        CGEventCreateKeyboardEvent=lambda source, key, down: {"down": down},
        CGEventKeyboardSetUnicodeString=lambda event, units, char: event.update(units=units, char=char),
        CGEventPost=lambda tap, event: events.append(event),
    )
    monkeypatch.setattr(input_module.sys, "platform", "darwin")
    monkeypatch.setitem(input_module.sys.modules, "pyautogui", keyboard)
    monkeypatch.setitem(input_module.sys.modules, "Quartz", quartz)
    monkeypatch.setattr(input_module.time, "sleep", lambda seconds: None)
    result = input_module.type_desktop_text("A中🚀", clear_existing=True)
    assert keys == [("command", "a"), ("backspace",)]
    assert [(event["char"], event["units"]) for event in events if event["down"]] == [("A", 1), ("中", 1), ("🚀", 2)]
    assert len(events) == 6
    assert result == {"submitted_characters": 3, "clear_existing": True, "application_verified": False}


def test_other_platform_rejects_unsupported_text_before_side_effects(monkeypatch):
    calls = []
    keyboard = SimpleNamespace(KEYBOARD_KEYS=["a"], failSafeCheck=lambda: calls.append("check"))
    monkeypatch.setattr(input_module.sys, "platform", "linux")
    monkeypatch.setitem(input_module.sys.modules, "pyautogui", keyboard)
    with pytest.raises(ValueError, match="未清空或输入"):
        input_module.type_desktop_text("a中", clear_existing=True)
    assert calls == []


def test_other_platform_types_supported_text_with_interval(monkeypatch):
    calls = []
    keyboard = SimpleNamespace(
        KEYBOARD_KEYS=["a", "b"], failSafeCheck=lambda: None,
        write=lambda text, **kwargs: calls.append((text, kwargs)),
    )
    monkeypatch.setattr(input_module.sys, "platform", "linux")
    monkeypatch.setitem(input_module.sys.modules, "pyautogui", keyboard)
    input_module.type_desktop_text("Ab")
    assert calls == [("Ab", {"interval": 0.05})]


def test_failed_event_creation_does_not_report_success(monkeypatch):
    monkeypatch.setattr(input_module.sys, "platform", "darwin")
    monkeypatch.setitem(input_module.sys.modules, "pyautogui", SimpleNamespace(failSafeCheck=lambda: None))
    monkeypatch.setitem(input_module.sys.modules, "Quartz", SimpleNamespace(CGEventCreateKeyboardEvent=lambda *args: None))
    with pytest.raises(RuntimeError, match="输入可能未完成"):
        input_module.type_desktop_text("a")
