"""update_persona 工具钉子:用户表达长期人设/画像/称呼时,模型惯性走 remember 塞进 memory
(每轮不注入=不生效)。补 UpdatePersonaTool 后:直接写 owner 的 SOUL/USER/AGENTS.md(每轮注入)。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.capability.persona_repository import (
    PersonaMutationRequest,
    PersonaRepository,
)
from agent_py_agent.agent.capability.persona_tool import UpdatePersonaTool


def _agent_with_paths(tmp_path):
    soul, user, agents = tmp_path / "SOUL.md", tmp_path / "USER.md", tmp_path / "AGENTS.md"
    for p, head in (
        (soul, "# SOUL\n"),
        (user, "# USER\n\n## 画像\n- 称呼:\n"),
        (agents, "# AGENTS\n"),
    ):
        p.write_text(head, encoding="utf-8")
    home = SimpleNamespace(owner_soul_md=soul, owner_user_md=user, owner_agents_md=agents)
    return SimpleNamespace(home_paths=home), soul, user, agents


def test_update_persona_writes_user_file(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    agent._current_user_prompt = "以后请叫我小王"
    result = UpdatePersonaTool(agent).execute(
        {"target": "user", "content": "称呼:小王", "source_quote": "以后请叫我小王"}
    )
    assert result.ok
    assert "称呼:小王" in user.read_text(encoding="utf-8")


def test_update_persona_targets_soul_and_agents_need_confirm(tmp_path):
    # SOUL/AGENTS 带 confirmed=true 才写(先问用户拿到同意后)
    agent, soul, _user, agents = _agent_with_paths(tmp_path)
    assert (
        UpdatePersonaTool(agent)
        .execute({"target": "soul", "content": "语气偏活泼", "confirmed": True})
        .ok
    )
    assert (
        UpdatePersonaTool(agent)
        .execute({"target": "agents", "content": "产物用 HTML", "confirmed": True})
        .ok
    )
    assert "语气偏活泼" in soul.read_text(encoding="utf-8")
    assert "产物用 HTML" in agents.read_text(encoding="utf-8")


def test_update_persona_soul_agents_refused_without_confirm(tmp_path):
    # 不带 confirmed:SOUL/AGENTS 拒写(护住长期人设不被随意自动改),文件不动
    agent, soul, _user, agents = _agent_with_paths(tmp_path)
    for target in ("soul", "agents"):
        r = UpdatePersonaTool(agent).execute({"target": target, "content": "别乱写"})
        assert r.ok is False
        assert r.error_code == "APPROVAL_REQUIRED"
    assert "别乱写" not in soul.read_text(encoding="utf-8")
    assert "别乱写" not in agents.read_text(encoding="utf-8")


def test_update_persona_user_no_confirm_needed(tmp_path):
    # USER(用户画像)不受确认限制,直接写
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    agent._current_user_prompt = "我是做电商的"
    assert (
        UpdatePersonaTool(agent)
        .execute({"target": "user", "content": "角色:电商", "source_quote": "我是做电商的"})
        .ok
    )
    assert "角色:电商" in user.read_text(encoding="utf-8")


def test_update_persona_idempotent(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    agent._current_user_prompt = "以后叫我小王"
    params = {"target": "user", "content": "称呼:小王", "source_quote": "以后叫我小王"}
    UpdatePersonaTool(agent).execute(params)
    UpdatePersonaTool(agent).execute(params)
    assert user.read_text(encoding="utf-8").count("称呼:小王") == 1


def test_update_persona_lists_replaces_and_removes_by_entry_id(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    tool = UpdatePersonaTool(agent)
    agent._current_user_prompt = "以后和我沟通要结论先行"
    added = tool.execute(
        {
            "target": "user",
            "content": "沟通偏好:结论先行",
            "source_quote": "以后和我沟通要结论先行",
        }
    )
    assert added.ok
    entry_id = json.loads(added.output)["entry_id"]

    listed = tool.execute({"action": "list", "target": "user"})
    assert listed.ok
    entries = json.loads(listed.output)["entries"]
    assert {row["entry_id"] for row in entries} >= {entry_id}

    agent._current_user_prompt = "请改成先给结论，再给依据"
    replaced = tool.execute(
        {
            "action": "replace",
            "target": "user",
            "entry_id": entry_id,
            "content": "沟通偏好:先给结论，再给依据",
            "source_quote": "请改成先给结论，再给依据",
        }
    )
    assert replaced.ok
    replacement_id = json.loads(replaced.output)["entry_id"]
    assert "沟通偏好:结论先行" not in user.read_text(encoding="utf-8")
    assert "沟通偏好:先给结论，再给依据" in user.read_text(encoding="utf-8")

    agent._current_user_prompt = "删除先给结论，再给依据这个偏好"
    removed = tool.execute(
        {
            "action": "remove",
            "target": "user",
            "entry_id": replacement_id,
            "source_quote": "删除先给结论，再给依据这个偏好",
        }
    )
    assert removed.ok
    assert "沟通偏好:先给结论，再给依据" not in user.read_text(encoding="utf-8")


def test_update_persona_missing_entry_does_not_claim_success(tmp_path):
    agent, *_ = _agent_with_paths(tmp_path)
    agent._current_user_prompt = "删除不存在的画像"
    result = UpdatePersonaTool(agent).execute(
        {
            "action": "remove",
            "target": "user",
            "entry_id": "persona-does-not-exist",
            "source_quote": "删除不存在的画像",
        }
    )
    assert result.ok is False
    assert result.error_code == "PERSONA_ENTRY_NOT_FOUND"


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


def test_update_persona_rejects_invalid_rollback_version_at_tool_boundary(tmp_path):
    agent, *_ = _agent_with_paths(tmp_path)
    result = UpdatePersonaTool(agent).execute(
        {"action": "rollback", "target": "user", "rollback_version": "not-an-integer"}
    )
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_update_persona_rejects_model_invented_user_value(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    agent._current_user_prompt = "记住，我喜欢青柠味"
    result = UpdatePersonaTool(agent).execute(
        {
            "target": "user",
            "content": "称呼:小王",
            "source_quote": "记住，我喜欢青柠味",
        }
    )
    assert result.ok is False
    assert result.error_code == "PERSONA_CONTENT_UNGROUNDED"
    assert "小王" not in user.read_text(encoding="utf-8")


def test_update_persona_rejects_source_not_in_current_user_message(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    agent._current_user_prompt = "记住，我喜欢青柠味"
    result = UpdatePersonaTool(agent).execute(
        {
            "target": "user",
            "content": "称呼:小王",
            "source_quote": "以后请叫我小王",
        }
    )
    assert result.ok is False
    assert result.error_code == "PERSONA_SOURCE_MISMATCH"
    assert "小王" not in user.read_text(encoding="utf-8")


def test_update_persona_rejects_multiple_user_facts_in_one_call(tmp_path):
    agent, _soul, user, _agents = _agent_with_paths(tmp_path)
    agent._current_user_prompt = "我喜欢青柠味"
    result = UpdatePersonaTool(agent).execute(
        {
            "target": "user",
            "content": "称呼:小王\n偏好:喜欢青柠味",
            "source_quote": "我喜欢青柠味",
        }
    )
    assert result.ok is False
    assert result.error_code == "PERSONA_CONTENT_NOT_ATOMIC"
    assert "青柠味" not in user.read_text(encoding="utf-8")


def test_append_persona_line_no_trailing_newline(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("# USER", encoding="utf-8")  # 无末尾换行
    repository = PersonaRepository(
        owner_home=tmp_path,
        soul_path=tmp_path / "SOUL.md",
        user_path=p,
        agents_path=tmp_path / "AGENTS.md",
    )
    repository.mutate(
        PersonaMutationRequest(
            target="user",
            action="add",
            content="称呼:小李",
            confirmed=True,
            source="test",
        )
    )
    txt = p.read_text(encoding="utf-8")
    assert txt == "# USER\n- 称呼:小李\n"


def test_update_persona_registered_in_agent_toolset(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.defaults import default_agent_config

    agent = SimpleAgent(default_agent_config(), tmp_path)
    names = [getattr(s, "name", "") for s in agent.tools.specs()]
    assert "update_persona" in names


# --------------------------------------------------------------------------- 飞书卡片确认流


def _feishu_agent(tmp_path, provider="feishu"):
    """owner=飞书用户的 agent:home_paths 带 owner_provider/owner_id/root,config 带飞书凭据。"""
    soul, user, agents = tmp_path / "SOUL.md", tmp_path / "USER.md", tmp_path / "AGENTS.md"
    for p, head in (
        (soul, "# SOUL\n"),
        (user, "# USER\n\n## 画像\n- 称呼:\n"),
        (agents, "# AGENTS\n"),
    ):
        p.write_text(head, encoding="utf-8")
    home = SimpleNamespace(
        owner_soul_md=soul,
        owner_user_md=user,
        owner_agents_md=agents,
        owner_provider=provider,
        owner_kind="user",
        owner_id="ou_x",
        root=tmp_path,
    )
    config = SimpleNamespace(feishu_app_id="app", feishu_app_secret="sec")
    return SimpleNamespace(home_paths=home, config=config), soul, user, agents


def test_feishu_soul_sends_card_and_stores_pending_not_writing(tmp_path, monkeypatch):
    # 飞书改 SOUL:不直接写,存待确认记录 + 发卡片,工具立即返回(非阻塞)。无需 confirmed。
    import json

    from agent_py_agent.agent.adapter import feishu_card as card_mod
    from agent_py_agent.agent.capability import persona_pending

    agent, soul, _user, _agents = _feishu_agent(tmp_path)
    sent: list = []
    monkeypatch.setattr(
        card_mod,
        "send_interactive_card",
        lambda aid, sec, oid, card: sent.append((aid, sec, oid, card)) or True,
    )
    # 即使模型自行塞 confirmed=true，飞书也必须等真人点卡片，不能直接写。
    r = UpdatePersonaTool(agent).execute(
        {"target": "soul", "content": "语气偏活泼", "confirmed": True}
    )
    assert r.ok
    payload = json.loads(r.output)
    assert payload["pending"] is True
    assert "语气偏活泼" not in soul.read_text(encoding="utf-8")  # 没直接写,等确认
    assert len(sent) == 1 and sent[0][0] == "app" and sent[0][2] == "ou_x"  # 卡片发到 owner open_id
    files = list((tmp_path / "pending_persona").glob("*.json"))
    assert len(files) == 1
    rec = persona_pending.load(tmp_path, files[0].stem)
    assert rec.target == "soul" and rec.content == "语气偏活泼" and rec.owner_provider == "feishu"


def test_feishu_card_send_fail_cleans_pending_and_no_write(tmp_path, monkeypatch):
    # 凭据缺失/发不出去 → fail-open:清掉悬挂记录 + 回落"就地不写 + 提示用户"(不崩)。
    import json

    from agent_py_agent.agent.adapter import feishu_card as card_mod

    agent, soul, _user, _agents = _feishu_agent(tmp_path)
    monkeypatch.setattr(card_mod, "send_interactive_card", lambda *a, **k: False)
    r = UpdatePersonaTool(agent).execute({"target": "agents", "content": "产物用 HTML"})
    assert r.ok  # fail-open,非错误态
    payload = json.loads(r.output)
    assert payload["pending"] is False and "没写" in payload["note"]
    assert "产物用 HTML" not in soul.read_text(encoding="utf-8")
    assert list((tmp_path / "pending_persona").glob("*.json")) == []  # 悬挂记录已清


def test_feishu_user_target_still_direct_write(tmp_path, monkeypatch):
    # target=user 完全不变:飞书下也直接写,不走卡片。
    from agent_py_agent.agent.adapter import feishu_card as card_mod

    agent, _soul, user, _agents = _feishu_agent(tmp_path)
    called: list = []
    monkeypatch.setattr(card_mod, "send_interactive_card", lambda *a, **k: called.append(1) or True)
    agent._current_user_prompt = "以后请叫我小王"
    r = UpdatePersonaTool(agent).execute(
        {"target": "user", "content": "称呼:小王", "source_quote": "以后请叫我小王"}
    )
    assert r.ok and "称呼:小王" in user.read_text(encoding="utf-8")
    assert called == []


def test_feishu_soul_remove_waits_for_card_and_carries_structured_operation(tmp_path, monkeypatch):
    import json

    from agent_py_agent.agent.adapter import feishu_card as card_mod
    from agent_py_agent.agent.capability import persona_pending

    agent, soul, _user, _agents = _feishu_agent(tmp_path)
    soul.write_text("# SOUL\n- 语气偏活泼\n", encoding="utf-8")
    listed = UpdatePersonaTool(agent).execute({"action": "list", "target": "soul"})
    entry_id = json.loads(listed.output)["entries"][0]["entry_id"]
    sent: list = []
    monkeypatch.setattr(
        card_mod,
        "send_interactive_card",
        lambda aid, sec, oid, card: sent.append(card) or True,
    )

    result = UpdatePersonaTool(agent).execute(
        {"action": "remove", "target": "soul", "entry_id": entry_id}
    )

    assert result.ok
    assert "语气偏活泼" in soul.read_text(encoding="utf-8")
    record_path = next((tmp_path / "pending_persona").glob("*.json"))
    record = persona_pending.load(tmp_path, record_path.stem)
    assert record is not None and record.action == "remove" and record.entry_id == entry_id
    assert "确认删除" in sent[0]["header"]["title"]["content"]


def test_non_feishu_soul_still_confirmed_gate(tmp_path):
    # 非飞书通道(local):保持现有 confirmed 闸——无 confirmed 拒写,有 confirmed 直接写。
    agent, soul, _user, _agents = _feishu_agent(tmp_path, provider="local")
    refused = UpdatePersonaTool(agent).execute({"target": "soul", "content": "语气偏活泼"})
    assert refused.ok is False and refused.error_code == "APPROVAL_REQUIRED"
    assert "pending_persona" not in {p.name for p in tmp_path.iterdir()}  # 非飞书不建待确认存储
    ok = UpdatePersonaTool(agent).execute(
        {"target": "soul", "content": "语气偏活泼", "confirmed": True}
    )
    assert ok.ok and "语气偏活泼" in soul.read_text(encoding="utf-8")
