from pathlib import Path

import pytest

from agent_py_agent.agent.skills import SkillRegistry


def _write_skill(
    root: Path,
    relative_dir: str,
    text: str,
) -> Path:
    skill_dir = root / relative_dir
    skill_dir.mkdir(parents=True)
    path = skill_dir / "SKILL.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_search_matches_short_task_without_loading_body(tmp_path: Path):
    _write_skill(
        tmp_path,
        "skills/python-debug",
        _skill_text(
            name="python-debug",
            description="Debug Python test failures and tracebacks",
            when_to_use="Use for failing pytest runs, exceptions, and stack traces",
            body="This body has secret body-only wording.",
        ),
    )
    registry = SkillRegistry([tmp_path / "skills"])
    registry.scan()

    hits = registry.search("pytest traceback", limit=1)

    assert [card.name for card in hits] == ["python-debug"]
    assert registry.search("secret body-only wording") == []
    assert "python-debug" in registry.render_catalog("traceback")


def test_scan_keeps_deterministic_override_order_across_multiple_dirs(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_skill(first, "api-check", _skill_text(name="shared", description="old shared skill"))
    _write_skill(second, "aaa", _skill_text(name="alpha", description="alpha skill"))
    _write_skill(second, "bbb-shared", _skill_text(name="shared", description="same dir earlier shared skill"))
    expected_path = _write_skill(second, "shared", _skill_text(name="shared", description="new shared skill"))
    _write_skill(second, "zzz", _skill_text(name="omega", description="omega skill"))

    registry = SkillRegistry([first, second])
    cards = registry.scan()

    assert [card.name for card in cards] == ["shared", "alpha", "omega"]
    assert registry.get("shared").description == "new shared skill"
    assert registry.resolve_path("shared") == expected_path


def test_scan_discovers_nested_plugin_group_skills(tmp_path: Path):
    skill_path = _write_skill(
        tmp_path,
        "skills/github/gh-fix-ci",
        _skill_text(
            name="gh-fix-ci",
            description="Diagnose GitHub Actions CI failures",
            when_to_use="Use when a pull request check is failing",
        ),
    )
    registry = SkillRegistry([tmp_path / "skills"])

    registry.scan()

    assert registry.get("gh-fix-ci").path == skill_path
    assert [card.name for card in registry.search("pull request check")] == ["gh-fix-ci"]


def test_load_body_supports_max_chars_and_unknown_skill_errors(tmp_path: Path):
    body = "A" * 120
    _write_skill(tmp_path, "skills/long", _skill_text(name="long", description="Long body skill", body=body))
    registry = SkillRegistry([tmp_path / "skills"])
    registry.scan()

    truncated = registry.load_body("long", max_chars=20)

    assert truncated.startswith("---\nname: long")
    assert truncated.endswith("\n... 已截断")
    assert len(truncated) < len(registry.load_body("long"))
    with pytest.raises(KeyError):
        registry.load_body("../long")
    with pytest.raises(KeyError):
        registry.resolve_path("missing")


def _skill_text(
    *,
    name: str,
    description: str,
    body: str = "Short body.",
    when_to_use: str = "",
) -> str:
    metadata = [
        "---",
        f"name: {name}",
        f"description: {description}",
    ]
    if when_to_use:
        metadata.append(f"when_to_use: {when_to_use}")
    metadata.extend(["---", "", body])
    return "\n".join(metadata)
