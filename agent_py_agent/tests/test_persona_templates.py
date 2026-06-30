"""人格模板种子 + 注入剥离注释 单测。"""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.prompting_parts.builder import _strip_injection_comments
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home


def test_strip_injection_comments_removes_comments_keeps_body():
    text = "# SOUL\n<!-- 引导注释,注入时该剥掉 -->\n## 身份\n正文内容\n"
    out = _strip_injection_comments(text)
    assert "<!--" not in out
    assert "## 身份" in out and "正文内容" in out


def test_new_home_seeds_structured_persona_templates(tmp_path):
    paths = ensure_my_agent_home(tmp_path)
    soul = Path(paths.owner_soul_md).read_text(encoding="utf-8")
    user = Path(paths.owner_user_md).read_text(encoding="utf-8")
    agents = Path(paths.owner_agents_md).read_text(encoding="utf-8")
    # 不再是空壳 # SOUL,而是结构化模板
    assert "## 身份" in soul and "## 语气" in soul
    assert "- 称呼:" in user and "## 偏好" in user
    # AGENTS 含我们要加的"响应优先"(派子代理保持及时回用户)
    assert "响应优先" in agents and "子代理" in agents


def test_upgrade_old_stub_root_templates_preserves_custom(tmp_path):
    paths = ensure_my_agent_home(tmp_path)
    # 模拟存量部署:根模板被写回旧空壳;USER 被管理员自定义
    Path(paths.soul_md).write_text("# SOUL\n\n", encoding="utf-8")
    Path(paths.user_md).write_text("# USER\n\n自定义画像模板,别动我", encoding="utf-8")
    ensure_my_agent_home(tmp_path)  # 再跑触发升级
    assert "## 身份" in Path(paths.soul_md).read_text(encoding="utf-8")  # 旧空壳 → 升级为新模板
    assert "自定义画像模板" in Path(paths.user_md).read_text(encoding="utf-8")  # 自定义 → 保留不动


def test_persona_template_injects_without_comments(tmp_path):
    paths = ensure_my_agent_home(tmp_path)
    soul = Path(paths.owner_soul_md).read_text(encoding="utf-8")
    assert "<!--" in soul  # 文件里有引导注释(Read 可见)
    injected = _strip_injection_comments(soul)
    assert "<!--" not in injected  # 注入时被剥掉(零 token)
    assert "## 身份" in injected  # 正文保留
