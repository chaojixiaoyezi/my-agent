"""skill 同步进 home 的测试:内置镜像(shared/builtin) + 用户自定义扫描(shared/skills)
合并写 skills.jsonl 索引。内置和自定义都自动可见、可检索;fingerprint 幂等;shared/builtin
零中间态文件。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

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


# LLM: 镜像完不能留中间态，也不能漏拷或多拷。断言分三层：① 除了 SKILL.md，其它文件只能落在某个
#   技能目录的 references/、templates/、scripts/ 子目录里（按 AGENTS.md 的 Skill 设计，这几层是按需
#   读取的附属资料）；② 每个文件都和内置源逐字节一致；③ 不允许临时/隐藏的半截文件。
# 函数用途: 核对镜像目录的文件集合与内容与内置源一致，没有多余文件也没有残留中间态。
def _assert_builtin_mirror_is_complete(mirror_dir: Path, src_dir: Path) -> None:
    allowed_containers = {"references", "templates", "scripts"}
    temp_suffixes = (".tmp", ".part", ".partial", ".swp", ".swx", ".bak", ".orig", "~")
    for path in mirror_dir.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(mirror_dir)
        assert not path.name.startswith("."), f"镜像目录里出现隐藏文件: {relative}"
        assert not path.name.endswith(temp_suffixes), f"镜像目录里出现临时/半截文件: {relative}"
        if path.name == "SKILL.md":
            continue
        # 非 SKILL.md 的文件必须落在某个技能目录下的 references/、templates/、scripts/ 子树里
        # （这几层是按需读取的附属资料，允许再往下分层，例如 templates/python/src/…）。
        assert allowed_containers & {part.name for part in relative.parents}, (
            f"非 SKILL.md 文件位置不合规: {relative}"
        )
        source = src_dir / relative
        assert source.is_file(), f"镜像里有源里不存在的文件: {relative}"
        assert path.read_bytes() == source.read_bytes(), f"镜像文件与源不一致: {relative}"
    # 反向核对：源里的每个文件都要镜像到，不能漏拷。
    for source in src_dir.rglob("*"):
        if source.is_file():
            assert (mirror_dir / source.relative_to(src_dir)).is_file(), f"源文件没被镜像: {source.name}"


def test_ensure_home_seeds_and_indexes(tmp_path):
    """端到端:ensure_my_agent_home 后 home/shared/builtin 有内置 skill、索引非空、零中间态。"""
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    paths = ensure_my_agent_home(tmp_path / "myhome")
    assert len(list(paths.shared_builtin_dir.rglob("SKILL.md"))) >= 20
    _assert_builtin_mirror_is_complete(paths.shared_builtin_dir, builtin_seed._BUILTIN_SRC)
    lines = [
        line for line in paths.shared_indexes_skills_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(lines) >= 20
