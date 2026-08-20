"""S2-14 修复回归：log-incident-triage skill 可发现、可加载、含核验关键规则。

真机实锤：1 万条假 SECURITY BREACH 告警把模型带偏（判定"极可能已沦陷"），
会话运行时 用指标化核验框架正确判定"未被入侵"。修复：新增 builtin skill
log-incident-triage（噪声识别 + 成功指标逐项核验 + 告警数量≠严重度 + 结论分级），
让日志/告警流调查任务有核验清单引导。
"""

from __future__ import annotations

from pathlib import Path


def _write_skill(root: Path, name: str, description: str, body: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _real_skill_body() -> str:
    return (
        Path(__file__).resolve().parent.parent
        / "skills"
        / "builtin"
        / "security"
        / "log-incident-triage"
        / "SKILL.md"
    ).read_text(encoding="utf-8")


def _load_skill_body(tmp_path: Path) -> str:
    # 真实 SKILL.md 原样写入测试 builtin 目录（skill_catalog_factory 的 builtin 在 home/builtin）
    target = tmp_path / "home" / "builtin" / "log-incident-triage" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_real_skill_body(), encoding="utf-8")
    return target.read_text(encoding="utf-8")


def test_log_incident_triage_skill_frontmatter_valid(tmp_path):
    """frontmatter 合法（name/description 齐全），能被 skill 系统解析。"""
    from agent_py_agent.agent.capability.skills import parse_skill_file

    _load_skill_body(tmp_path)
    skill_path = (
        tmp_path
        / "home"
        / "builtin"
        / "log-incident-triage"
        / "SKILL.md"
    )
    card = parse_skill_file(skill_path, source="builtin")
    assert card.name == "log-incident-triage"
    assert "日志" in card.description or "告警" in card.description


def test_log_incident_triage_skill_discovers_and_resolves(tmp_path, skill_catalog_factory):
    """新 skill 在 builtin 源中被自动发现并可按名 resolve。"""
    _load_skill_body(tmp_path)
    catalog = skill_catalog_factory(tmp_path / "home")
    snapshot = catalog.service.snapshot_for(catalog.workspace, force_reload=True)
    entry = snapshot.resolve("log-incident-triage")
    assert entry is not None, [item.name for item in snapshot.entries]
    assert entry.source == "builtin"
    assert entry.stable_id == "builtin:log-incident-triage"


def test_log_incident_triage_skill_contains_core_disciplines(tmp_path, skill_catalog_factory):
    """核验关键规则必须出现在 skill 正文（防止未来删减核心纪律）。"""
    body = _load_skill_body(tmp_path)
    for required in (
        "重复模式",
        "告警数量 ≠ 严重度",
        "成功指标",
        "已确认",
        "未证实",
        "证据",
    ):
        assert required in body, f"skill 缺少核心规则: {required}"
