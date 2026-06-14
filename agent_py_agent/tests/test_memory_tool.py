"""remember 工具钉子(r19 网页搜索任务实锤:用户说"记住我的偏好",但工具表此前无任何
memory 写入工具,agent 想记也没工具)。补 RememberTool 后:用户指令式写 owner 长期记忆。"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.capability.memory_tool import RememberTool, _normalize_tags


class _FakeMemory:
    def __init__(self):
        self.added = []

    def add(self, role, content, *, kind="dialogue", tags=None):
        self.added.append({"role": role, "content": content, "kind": kind, "tags": tags or []})
        return SimpleNamespace(content=content)


def test_remember_writes_to_owner_memory():
    mem = _FakeMemory()
    tool = RememberTool(SimpleNamespace(memory=mem))
    result = tool.execute({"content": "用户看技术简报偏好'结论先行+要点列表'", "tags": ["preference", "format"]})
    assert result.ok
    assert len(mem.added) == 1
    rec = mem.added[0]
    assert rec["role"] == "user"
    assert rec["kind"] == "preference"
    assert "结论先行" in rec["content"]
    assert rec["tags"] == ["preference", "format"]


def test_remember_missing_content_errors():
    tool = RememberTool(SimpleNamespace(memory=_FakeMemory()))
    result = tool.execute({"content": "  "})
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_remember_unavailable_when_no_memory():
    tool = RememberTool(SimpleNamespace(memory=None))
    result = tool.execute({"content": "x"})
    assert result.ok is False
    assert result.error_code == "TOOL_UNAVAILABLE"


def test_normalize_tags():
    assert _normalize_tags(["a", " b ", ""]) == ["a", "b"]
    assert _normalize_tags("solo") == ["solo"]
    assert _normalize_tags(None) == []
    assert _normalize_tags(123) == []


def test_remember_registered_in_agent_toolset(tmp_path):
    # 端到端:remember 工具确实进了 agent 工具表(回归 r19 "无 memory 写入工具" 缺口)
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.defaults import default_agent_config

    agent = SimpleAgent(default_agent_config(), tmp_path)
    names = [getattr(s, "name", "") for s in agent.tools.specs()]
    assert "remember" in names
