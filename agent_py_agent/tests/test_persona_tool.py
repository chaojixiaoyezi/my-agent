"""update_persona 工具钉子:用户表达长期人设/画像/称呼时,模型惯性走 remember 塞进 memory
(每轮不注入=不生效)。补 UpdatePersonaTool 后:直接写 owner 的 SOUL/USER/AGENTS.md(每轮注入)。"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.capability.persona_tool import UpdatePersonaTool, _append_persona_line


def _agent_with_paths(tmp_path):
    soul, user, agents = tmp_path / "SOUL.md", tmp_path / "USER.md", tmp_path / "AGENTS.md"
    for p, head in ((soul, "# SOUL\n"), (user, "# USER\n\n## 画像\n- 称呼:\n"), (agents, "# AGENTS\n")):
        p.write_text(head, encoding="utf-8")
    home = SimpleNamespace(owner_soul_md=soul, owner_user_md=user, owner_agents_md=agents)
    return SimpleNamespace(home_paths=home), soul, user, agents


def test_update_persona_writes_user_file(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    result = UpdatePersonaTool(agent).execute({"target": "user", "content": "称呼:小王"})
    assert result.ok
    assert "称呼:小王" in user.read_text(encoding="utf-8")


def test_update_persona_targets_soul_and_agents(tmp_path):
    agent, soul, _user, agents = _agent_with_paths(tmp_path)
    assert UpdatePersonaTool(agent).execute({"target": "soul", "content": "语气偏活泼"}).ok
    assert UpdatePersonaTool(agent).execute({"target": "agents", "content": "产物用 HTML"}).ok
    assert "语气偏活泼" in soul.read_text(encoding="utf-8")
    assert "产物用 HTML" in agents.read_text(encoding="utf-8")


def test_update_persona_idempotent(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    UpdatePersonaTool(agent).execute({"target": "user", "content": "称呼:小王"})
    UpdatePersonaTool(agent).execute({"target": "user", "content": "称呼:小王"})
    assert user.read_text(encoding="utf-8").count("称呼:小王") == 1


def test_update_persona_invalid_target(tmp_path):
    agent, *_ = _agent_with_paths(tmp_path)
    result = UpdatePersonaTool(agent).execute({"target": "memory", "content": "x"})
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_update_persona_missing_content(tmp_path):
    agent, *_ = _agent_with_paths(tmp_path)
    result = UpdatePersonaTool(agent).execute({"target": "user", "content": "  "})
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_append_persona_line_no_trailing_newline(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("# USER", encoding="utf-8")  # 无末尾换行
    _append_persona_line(p, "称呼:小李")
    txt = p.read_text(encoding="utf-8")
    assert txt == "# USER\n- 称呼:小李\n"


def test_update_persona_registered_in_agent_toolset(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.defaults import default_agent_config

    agent = SimpleAgent(default_agent_config(), tmp_path)
    names = [getattr(s, "name", "") for s in agent.tools.specs()]
    assert "update_persona" in names
