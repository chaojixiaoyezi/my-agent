"""skill 同步进 home 的测试:内置镜像(shared/builtin) + 用户自定义扫描(shared/skills)
合并写 skills.jsonl 索引。内置和自定义都自动可见、可检索;fingerprint 幂等;shared/builtin
零中间态文件。
"""
from __future__ import annotations

import json
import shutil

from agent_py_agent.agent.capability import builtin_seed
from agent_py_agent.agent.capability.builtin_seed import sync_skill_index


def _make_skill(root, category, name, *, desc="测试技能"):
    skill_dir = root / category / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n\n正文内容。\n",
        encoding="utf-8",
    )
    return skill_dir


def _paths(tmp_path):
    """(shared_builtin_dir, shared_skills_dir, skills_index_jsonl, fingerprint_file)。"""
    shared = tmp_path / "home" / "shared"
    return (
        shared / "builtin",
        shared / "skills",
        shared / "indexes" / "skills.jsonl",
        tmp_path / "home" / "cache" / "skill_index.fingerprint",
    )


def test_mirrors_builtin_and_indexes(tmp_path, monkeypatch):
    src = tmp_path / "src_builtin"
    _make_skill(src, "security", "deep-scan")
    _make_skill(src, "planning", "define-goal")
    monkeypatch.setattr(builtin_seed, "_BUILTIN_SRC", src)
    builtin_dir, skills_dir, index, fingerprint = _paths(tmp_path)

    count = sync_skill_index(builtin_dir, skills_dir, index, fingerprint)
    assert count == 2
    assert (builtin_dir / "security" / "deep-scan" / "SKILL.md").is_file()
    rows = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert {r["name"] for r in rows} == {"deep-scan", "define-goal"}
    assert all(r["source"] == "builtin" and r["kind"] == "skill" for r in rows)


def test_custom_skill_auto_indexed(tmp_path, monkeypatch):
    """用户丢进 shared/skills 的自定义 skill 自动进索引(source=custom),无需手动注册。"""
    src = tmp_path / "src_builtin"
    _make_skill(src, "meta", "writing")
    monkeypatch.setattr(builtin_seed, "_BUILTIN_SRC", src)
    builtin_dir, skills_dir, index, fingerprint = _paths(tmp_path)
    _make_skill(skills_dir, "mycat", "my-skill", desc="我的技能")

    count = sync_skill_index(builtin_dir, skills_dir, index, fingerprint)
    assert count == 2  # 1 builtin + 1 custom
    by_name = {
        r["name"]: r
        for r in (json.loads(line) for line in index.read_text(encoding="utf-8").splitlines() if line.strip())
    }
    assert by_name["writing"]["source"] == "builtin"
    assert by_name["my-skill"]["source"] == "custom"
    # custom skill 原地索引,不被镜像进 shared/builtin
    assert not (builtin_dir / "mycat" / "my-skill").exists()


def test_custom_skill_indexed_in_place(tmp_path, monkeypatch):
    src = tmp_path / "src_builtin"
    monkeypatch.setattr(builtin_seed, "_BUILTIN_SRC", src)  # 空 builtin 源
    builtin_dir, skills_dir, index, fingerprint = _paths(tmp_path)
    custom = _make_skill(skills_dir, "x", "custom-only", desc="仅自定义")
    sync_skill_index(builtin_dir, skills_dir, index, fingerprint)
    assert custom.is_dir()  # 原地还在
    rows = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows[0]["path"] == str(custom / "SKILL.md")  # 索引指向原地路径


def test_idempotent_skips_unchanged(tmp_path, monkeypatch):
    src = tmp_path / "src_builtin"
    _make_skill(src, "meta", "writing")
    monkeypatch.setattr(builtin_seed, "_BUILTIN_SRC", src)
    builtin_dir, skills_dir, index, fingerprint = _paths(tmp_path)
    sync_skill_index(builtin_dir, skills_dir, index, fingerprint)
    mtime = index.stat().st_mtime_ns
    assert sync_skill_index(builtin_dir, skills_dir, index, fingerprint) == 1
    assert index.stat().st_mtime_ns == mtime  # 跳过,没重写


def test_rebuild_when_custom_added(tmp_path, monkeypatch):
    """用户新增自定义 skill 后,下次同步自动纳入(fingerprint 变)。"""
    src = tmp_path / "src_builtin"
    _make_skill(src, "meta", "writing")
    monkeypatch.setattr(builtin_seed, "_BUILTIN_SRC", src)
    builtin_dir, skills_dir, index, fingerprint = _paths(tmp_path)
    assert sync_skill_index(builtin_dir, skills_dir, index, fingerprint) == 1
    _make_skill(skills_dir, "new", "newly-added")  # 用户加了一个
    assert sync_skill_index(builtin_dir, skills_dir, index, fingerprint) == 2  # 自动纳入


def test_shared_builtin_no_intermediate_files(tmp_path, monkeypatch):
    src = tmp_path / "src_builtin"
    _make_skill(src, "meta", "writing")
    monkeypatch.setattr(builtin_seed, "_BUILTIN_SRC", src)
    builtin_dir, skills_dir, index, fingerprint = _paths(tmp_path)
    sync_skill_index(builtin_dir, skills_dir, index, fingerprint)
    non_skill = [p for p in builtin_dir.rglob("*") if p.is_file() and p.name != "SKILL.md"]
    assert non_skill == [], f"shared/builtin 含中间态文件: {non_skill}"
    assert fingerprint.is_file()  # fingerprint 在 cache,不进 builtin


def test_full_resync_removes_deleted_builtin(tmp_path, monkeypatch):
    src = tmp_path / "src_builtin"
    _make_skill(src, "security", "a")
    skill_b = _make_skill(src, "security", "b")
    monkeypatch.setattr(builtin_seed, "_BUILTIN_SRC", src)
    builtin_dir, skills_dir, index, fingerprint = _paths(tmp_path)
    assert sync_skill_index(builtin_dir, skills_dir, index, fingerprint) == 2
    shutil.rmtree(skill_b)
    assert sync_skill_index(builtin_dir, skills_dir, index, fingerprint) == 1
    assert not (builtin_dir / "security" / "b").exists()  # 镜像也删


def test_real_builtin_smoke():
    dirs = builtin_seed._skill_dirs(builtin_seed._BUILTIN_SRC)
    names = {d.name for d in dirs}
    assert len(dirs) >= 20
    assert "academic-word-pdf-layout" in names
    assert "deep-security-scan" in names


def test_ensure_home_seeds_and_indexes(tmp_path):
    """端到端:ensure_my_agent_home 后 home/shared/builtin 有内置 skill、索引非空、零中间态。"""
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    paths = ensure_my_agent_home(tmp_path / "myhome")
    assert len(list(paths.shared_builtin_dir.rglob("SKILL.md"))) >= 20
    non_skill = [p for p in paths.shared_builtin_dir.rglob("*") if p.is_file() and p.name != "SKILL.md"]
    assert non_skill == []
    lines = [
        line for line in paths.shared_indexes_skills_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(lines) >= 20
