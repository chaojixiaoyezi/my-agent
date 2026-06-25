"""内置 skill 镜像到 home(shared/builtin + skills.jsonl 索引)的同步测试。

需求:内置 skill 物理在源码,用户在 home 看不到。同步后内置 skill 像自定义 skill 一样
在 home/shared/builtin 可见、可被 capability 索引发现;全量同步(覆盖+删除源码已移除的),
fingerprint 幂等,用户自定义 shared/skills 不受影响。fingerprint 标记写在 cache、不进
shared/builtin——那里只留纯粹的 skill,零中间态文件。
"""
from __future__ import annotations

import json
import shutil

from agent_py_agent.agent.capability import builtin_seed
from agent_py_agent.agent.capability.builtin_seed import sync_builtin_skills_to_home


def _make_skill(src, category, name, *, desc="测试技能"):
    skill_dir = src / category / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n\n正文内容。\n",
        encoding="utf-8",
    )
    return skill_dir


def _paths(tmp_path):
    """(shared_builtin_dir, skills_index_jsonl, fingerprint_file)。"""
    shared = tmp_path / "home" / "shared"
    return (
        shared / "builtin",
        shared / "indexes" / "skills.jsonl",
        tmp_path / "home" / "cache" / "builtin_skills.fingerprint",
    )


def test_sync_mirrors_builtin_to_home(tmp_path, monkeypatch):
    src = tmp_path / "src_builtin"
    _make_skill(src, "security", "deep-scan")
    _make_skill(src, "planning", "define-goal")
    monkeypatch.setattr(builtin_seed, "_BUILTIN_SRC", src)

    home_builtin, index, fingerprint = _paths(tmp_path)
    count = sync_builtin_skills_to_home(home_builtin, index, fingerprint)

    assert count == 2
    assert (home_builtin / "security" / "deep-scan" / "SKILL.md").is_file()
    assert (home_builtin / "planning" / "define-goal" / "SKILL.md").is_file()
    rows = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 2
    assert {r["name"] for r in rows} == {"deep-scan", "define-goal"}
    for row in rows:
        assert row["kind"] == "skill"
        assert row["source"] == "builtin"
        assert row["path"].endswith("SKILL.md")


def test_shared_builtin_has_no_intermediate_files(tmp_path, monkeypatch):
    """shared/builtin 里只能有 SKILL.md,零中间态文件(fingerprint 标记不在这里)。"""
    src = tmp_path / "src_builtin"
    _make_skill(src, "meta", "writing")
    _make_skill(src, "quality", "tdd")
    monkeypatch.setattr(builtin_seed, "_BUILTIN_SRC", src)
    home_builtin, index, fingerprint = _paths(tmp_path)

    sync_builtin_skills_to_home(home_builtin, index, fingerprint)
    non_skill = [p for p in home_builtin.rglob("*") if p.is_file() and p.name != "SKILL.md"]
    assert non_skill == [], f"shared/builtin 应只有 SKILL.md,发现中间态文件: {non_skill}"
    assert not (home_builtin / ".builtin_fingerprint").exists()
    assert fingerprint.is_file()  # 标记落在 cache,不在 shared/builtin


def test_sync_idempotent_skips_when_unchanged(tmp_path, monkeypatch):
    src = tmp_path / "src_builtin"
    _make_skill(src, "meta", "writing")
    monkeypatch.setattr(builtin_seed, "_BUILTIN_SRC", src)
    home_builtin, index, fingerprint = _paths(tmp_path)

    sync_builtin_skills_to_home(home_builtin, index, fingerprint)
    fp_value = fingerprint.read_text(encoding="utf-8")
    mtime = index.stat().st_mtime_ns

    count = sync_builtin_skills_to_home(home_builtin, index, fingerprint)  # 内容没变 → 跳过
    assert count == 1
    assert fingerprint.read_text(encoding="utf-8") == fp_value
    assert index.stat().st_mtime_ns == mtime  # 没重写索引


def test_sync_full_resync_removes_deleted_skill(tmp_path, monkeypatch):
    src = tmp_path / "src_builtin"
    _make_skill(src, "security", "a")
    skill_b = _make_skill(src, "security", "b")
    monkeypatch.setattr(builtin_seed, "_BUILTIN_SRC", src)
    home_builtin, index, fingerprint = _paths(tmp_path)
    assert sync_builtin_skills_to_home(home_builtin, index, fingerprint) == 2

    shutil.rmtree(skill_b)  # 源码删掉 b
    count = sync_builtin_skills_to_home(home_builtin, index, fingerprint)
    assert count == 1
    assert not (home_builtin / "security" / "b").exists()  # 镜像也删了
    assert (home_builtin / "security" / "a" / "SKILL.md").is_file()


def test_sync_updates_changed_skill_content(tmp_path, monkeypatch):
    src = tmp_path / "src_builtin"
    skill_dir = _make_skill(src, "quality", "tdd", desc="旧描述")
    monkeypatch.setattr(builtin_seed, "_BUILTIN_SRC", src)
    home_builtin, index, fingerprint = _paths(tmp_path)
    sync_builtin_skills_to_home(home_builtin, index, fingerprint)

    (skill_dir / "SKILL.md").write_text(
        "---\nname: tdd\ndescription: 新描述\n---\n\n# tdd\n\n新正文。\n", encoding="utf-8"
    )  # 改源码 → fingerprint 变 → 重同步
    sync_builtin_skills_to_home(home_builtin, index, fingerprint)
    rows = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows[0]["description"] == "新描述"
    assert "新正文" in (home_builtin / "quality" / "tdd" / "SKILL.md").read_text(encoding="utf-8")


def test_sync_does_not_touch_user_custom_skills(tmp_path, monkeypatch):
    """全量覆盖只动 shared/builtin;用户自定义 shared/skills 不受影响。"""
    src = tmp_path / "src_builtin"
    _make_skill(src, "security", "builtin-skill")
    monkeypatch.setattr(builtin_seed, "_BUILTIN_SRC", src)
    home_builtin, index, fingerprint = _paths(tmp_path)
    user_skills = home_builtin.parent / "skills"
    user_skills.mkdir(parents=True)
    (user_skills / "my-own.md").write_text("我的自定义", encoding="utf-8")

    sync_builtin_skills_to_home(home_builtin, index, fingerprint)
    assert (user_skills / "my-own.md").read_text(encoding="utf-8") == "我的自定义"


def test_sync_real_builtin_smoke():
    """真实源码 builtin 能被定位(>=20 个,含 academic/deep-security-scan)。"""
    dirs = builtin_seed._builtin_skill_dirs(builtin_seed._BUILTIN_SRC)
    names = {d.name for d in dirs}
    assert len(dirs) >= 20
    assert "academic-word-pdf-layout" in names
    assert "deep-security-scan" in names


def test_ensure_home_seeds_builtin_skills(tmp_path):
    """端到端:ensure_my_agent_home 跑完,home/shared/builtin 有内置 skill、索引非空、零中间态。"""
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    paths = ensure_my_agent_home(tmp_path / "myhome")
    assert paths.shared_builtin_dir.is_dir()
    assert len(list(paths.shared_builtin_dir.rglob("SKILL.md"))) >= 20
    non_skill = [p for p in paths.shared_builtin_dir.rglob("*") if p.is_file() and p.name != "SKILL.md"]
    assert non_skill == [], f"shared/builtin 含中间态文件: {non_skill}"
    index_lines = [
        line for line in paths.shared_indexes_skills_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(index_lines) >= 20
