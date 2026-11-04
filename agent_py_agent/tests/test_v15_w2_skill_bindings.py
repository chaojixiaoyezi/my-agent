"""P1.1-b：目录与快照共享绑定并集，元数据消费不提前读取Skill正文。"""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.assets import skill_adapter as adapter
from agent_py_agent.agent.capability.skill_snapshot import (
    SkillSnapshot,
    SkillSnapshotEntry,
    SkillSnapshotError,
)


# LLM: 所有样本仅写入pytest隔离目录，不使用真实Owner目录。
# 函数用途: 创建含结构化头部和可选侧文件的技能样本。
def _skill(root: Path, name: str, front: dict, side: dict | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    fields = {"name": name, "description": "绑定测试", **front}
    header = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
    path = directory / "SKILL.md"
    path.write_text(f"---\n{header}\n---\n# 不应提前输出的正文\n", encoding="utf-8")
    if side is not None:
        (directory / "skill_meta.json").write_text(json.dumps(side), encoding="utf-8")
    return path


# LLM: 四类引用沿AssetRefs既有字典形状，不发明新的资产种类。
# 函数用途: 给断言补齐所有空引用列表。
def _refs(values: dict) -> dict:
    return {"workflows": [], "scripts": [], "skills": [], "monitors": [], **values}


# LLM: 禁止路径一旦调用就断言失败，不以空返回掩盖提前读取。
# 函数用途: 阻止metadata路径预读正文或重新发现候选。
def _forbidden_read(first, second=None, third=None, *, max_chars=0):
    pytest.fail("元数据路径不得提前读取Skill正文或重新发现候选")


# LLM: frontmatter与侧文件分别归一后求并集，不能让侧文件覆盖整组引用。
# 函数用途: 覆盖单来源、重叠及重复，核对catalog/list/冻结entry相等。
@pytest.mark.parametrize(
    ("front", "side", "expected"),
    [
        ({"scripts": ["b", "a"]}, None, {"scripts": ["a", "b"]}),
        ({}, {"workflows": ["w-b", "w-a"]}, {"workflows": ["w-a", "w-b"]}),
        ({"scripts": ["b", "a"], "workflows": ["w-a"]},
         {"scripts": ["c", "b"], "workflows": ["w-b"]},
         {"scripts": ["a", "b", "c"], "workflows": ["w-a", "w-b"]}),
        ({"scripts": ["b", "b", "a", "a"]}, {"scripts": ["b", "c", "c"]},
         {"scripts": ["a", "b", "c"]}),
    ],
    ids=["frontmatter-only", "sidecar-only", "overlap", "dedup"],
)
def test_refs_union_catalog_list_snapshot(tmp_path, skill_catalog_factory, front, side, expected):
    home = tmp_path / "catalog"
    path = _skill(home / "owner/skills", "bound", {"refs": front}, None if side is None else {"refs": side})
    catalog = skill_catalog_factory(home)
    wanted = _refs(expected)
    assert adapter.extract_skill_descriptor(path)["refs"] == wanted
    assert adapter.list_owner_skill_descriptors(catalog.home.owner_home_dir)[0]["refs"] == wanted
    rows = adapter.list_owner_skill_descriptors(catalog.home.owner_home_dir, skill_snapshot=catalog.snapshot)
    assert rows[0]["refs"] == wanted
    assert getattr(catalog.snapshot.resolve("owner:bound"), "refs", {}) == wanted


# LLM: 限额作用于排序去重后的总并集，不先截断任意一个来源。
# 函数用途: 验证各类最多64且稳定排序。
def test_refs_union_has_per_kind_sorted_cap(tmp_path, skill_catalog_factory):
    kinds = ("workflows", "scripts", "skills", "monitors")
    front = {kind: [f"z-{index:03d}" for index in reversed(range(80))] for kind in kinds}
    side = {kind: [f"a-{index:03d}" for index in reversed(range(20))] * 2 for kind in kinds}
    expected = {kind: [f"a-{index:03d}" for index in range(20)] + [f"z-{index:03d}" for index in range(44)]
                for kind in kinds}
    home = tmp_path / "catalog"
    path = _skill(home / "owner/skills", "capped", {"refs": front}, {"refs": side})
    catalog = skill_catalog_factory(home)
    assert adapter.extract_skill_descriptor(path)["refs"] == expected
    assert catalog.snapshot.resolve("capped").refs == expected
    assert adapter.list_owner_skill_descriptors(catalog.home.owner_home_dir, skill_snapshot=catalog.snapshot)[0]["refs"] == expected


# LLM: 新字段不能改变既有位置参数，缺省容器不得跨实例共享。
# 函数用途: 固定keyword-only默认值和公开序列化的身份边界。
def test_snapshot_entry_fields_are_defaulted_keyword_only():
    signature = inspect.signature(SkillSnapshotEntry)
    assert "refs" in signature.parameters
    assert signature.parameters["refs"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["asset_metadata"].kind is inspect.Parameter.KEYWORD_ONLY
    first = SkillSnapshotEntry("owner:x", "x", "x", "", "owner", "general", "hash")
    second = SkillSnapshotEntry("owner:y", "y", "y", "", "owner", "general", "hash")
    assert first.refs == second.refs == {}
    assert first.refs is not second.refs
    assert "path" not in first.to_dict()
    assert first.to_ref() == {"stable_id": "owner:x", "name": "x", "source": "owner", "content_sha256": "hash"}


# LLM: 发现阶段结束后只消费冻结entry，不能为了目录渲染调用read_body或重新发现。
# 函数用途: 验证不读正文的元数据入口和冻结可见性元数据。
def test_metadata_projection_uses_frozen_refs_without_body_read(tmp_path, skill_catalog_factory, monkeypatch):
    home = tmp_path / "catalog"
    _skill(home / "owner/skills", "bound", {"refs": {"scripts": ["s"]}},
           {"refs": {"workflows": ["w"]}, "metadata": {"hidden": True, "member_of": ["workflow:parent"]}})
    catalog = skill_catalog_factory(home)
    monkeypatch.setattr(SkillSnapshot, "read_body", _forbidden_read)
    monkeypatch.setattr(adapter, "_catalog_paths", _forbidden_read)
    monkeypatch.setattr(adapter, "_text", _forbidden_read)
    entry = catalog.snapshot.resolve("bound")
    rows = adapter.list_owner_skill_descriptors(catalog.home.owner_home_dir, skill_snapshot=catalog.snapshot)
    detail = adapter.get_owner_skill_descriptor(catalog.home.owner_home_dir, "owner:bound", skill_snapshot=catalog.snapshot)
    assert rows[0]["refs"] == detail["refs"] == _refs({"scripts": ["s"], "workflows": ["w"]})
    assert entry.asset_metadata["hidden"] is True
    public = entry.to_dict()
    assert public["refs"] == detail["refs"]
    public["refs"]["scripts"].append("not-frozen")
    assert entry.refs["scripts"] == ["s"]
    assert detail["metadata"]["member_of"] == ["workflow:parent"]
    assert "body_markdown" not in detail and "path" not in detail
    assert "不应提前输出" not in json.dumps(detail, ensure_ascii=False)
    narrowed = catalog.snapshot.restricted(("owner:bound",))
    assert narrowed.resolve("bound").refs == entry.refs


# LLM: 旧快照不重新合入磁盘新绑定；显式正文读取仍核原hash。
# 函数用途: 验证侧文件事件重建、冻结引用与正文陈旧错误。
def test_snapshot_refs_stay_frozen_and_body_hash_still_enforced(tmp_path, skill_catalog_factory):
    home = tmp_path / "catalog"
    path = _skill(home / "owner/skills", "bound", {}, {"refs": {"scripts": ["before"]}})
    catalog = skill_catalog_factory(home)
    restricted = catalog.snapshot.restricted(("owner:bound",))
    path.with_name("skill_meta.json").write_text(json.dumps({"refs": {"scripts": ["after"]}}), encoding="utf-8")
    assert restricted.resolve("bound").refs == _refs({"scripts": ["before"]})
    refreshed = catalog.service.snapshot_for(force_reload=True)
    assert refreshed.resolve("bound").refs == _refs({"scripts": ["after"]})
    detail = adapter.get_owner_skill_descriptor(home / "owner", "bound", skill_snapshot=restricted, include_body=True)
    assert detail["refs"] == _refs({"scripts": ["before"]})
    assert detail["body_markdown"].endswith("# 不应提前输出的正文\n")
    path.write_text(path.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    with pytest.raises(SkillSnapshotError) as caught:
        adapter.get_owner_skill_descriptor(home / "owner", "bound", skill_snapshot=restricted, include_body=True)
    assert caught.value.error_code == "SKILL_SNAPSHOT_STALE"


# LLM: 当前canonical侧开关停用不能被旧授权或冻结refs复活。
# 函数用途: 验证禁用条目及空/受限快照不扩大候选集合。
def test_disabled_and_restricted_snapshots_do_not_discover_extra_refs(tmp_path, skill_catalog_factory, monkeypatch):
    home = tmp_path / "catalog"
    path = _skill(home / "owner/skills", "allowed", {}, {"refs": {"scripts": ["s"]}})
    _skill(home / "owner/skills", "disabled", {}, {"enabled": False, "refs": {"scripts": ["private"]}})
    catalog = skill_catalog_factory(home)
    restricted = catalog.snapshot.restricted(("owner:allowed",))
    monkeypatch.setattr(adapter, "_catalog_paths", _forbidden_read)
    assert [row["name"] for row in adapter.list_owner_skill_descriptors(home / "owner", skill_snapshot=restricted)] == ["allowed"]
    assert adapter.list_owner_skill_descriptors(home / "owner", skill_snapshot=replace(restricted, entries=())) == []
    path.with_name("skill_meta.json").write_text(json.dumps({"enabled": False}), encoding="utf-8")
    with pytest.raises(SkillSnapshotError) as caught:
        adapter.get_owner_skill_descriptor(home / "owner", "allowed", skill_snapshot=restricted)
    assert caught.value.error_code == "ASSET_DISABLED_BY_OWNER"


# LLM: 所有新增绑定读取都必须拒绝链接，不能等正文get才拒绝。
# 函数用途: 验证nofollow侧文件及hash不符的entry不会发表绑定。
def test_snapshot_ref_capture_rejects_sidecar_links_and_stale_body(tmp_path, skill_catalog_factory):
    home = tmp_path / "catalog"
    path = _skill(home / "owner/skills", "bound", {"refs": {"scripts": ["s"]}})
    catalog = skill_catalog_factory(home)
    external = tmp_path / "external.json"
    external.write_text(json.dumps({"refs": {"scripts": ["not-authorized"]}}), encoding="utf-8")
    path.with_name("skill_meta.json").symlink_to(external)
    fresh_entry = replace(catalog.snapshot.resolve("bound"), refs={}, asset_metadata={})
    linked = replace(catalog.snapshot, entries=(fresh_entry,))
    assert not linked.enabled_entries()
    assert linked.errors[-1].code == "SKILL_READ_FAILED"
    path.with_name("skill_meta.json").unlink()
    path.write_text(path.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    stale = replace(catalog.snapshot, entries=(fresh_entry,))
    assert not stale.enabled_entries()
    assert stale.errors[-1].code == "SKILL_SNAPSHOT_STALE"


# LLM: 历史正文hash换绑必须连同frontmatter绑定换版，不能沿用当前正文的refs。
# 函数用途: 验证原历史授权解析口产生的entry冻结正确历史头部与当前合法侧绑定。
def test_historical_snapshot_refs_follow_the_pinned_body(tmp_path, skill_catalog_factory, monkeypatch):
    from agent_py_agent.agent.capability import skill_learning_versions as versions
    from agent_py_agent.agent.capability.skill_snapshot import skill_content_sha256

    home = tmp_path / "catalog"
    _skill(home / "owner/skills", "bound", {"refs": {"scripts": ["current"]}},
           {"refs": {"workflows": ["side"]}})
    old_path = _skill(tmp_path / "history", "bound", {"refs": {"scripts": ["historical"]}})
    old_text = old_path.read_text(encoding="utf-8")
    old_hash = skill_content_sha256(old_text)
    catalog = skill_catalog_factory(home)

    # LLM: 只替代已有历史授权口的返回值，不扩大snapshot的候选集合。
    # 函数用途: 给同一身份提供准确旧hash条目。
    def resolve(entry, expected):
        assert expected == old_hash
        return replace(entry, content_sha256=old_hash, frozen_path=str(old_path))

    # LLM: 原历史读取仍须接收准确固定hash，测试正文来自隔离版本文件。
    # 函数用途: 提供已授权历史正文，不访问真实学习目录。
    def read_historical(entry):
        assert entry.content_sha256 == old_hash
        return old_text

    monkeypatch.setattr(versions, "resolve_learned_revision", resolve)
    monkeypatch.setattr(versions, "read_frozen_learned_body", read_historical)
    historical = catalog.snapshot.restricted(("owner:bound",), expected_sha256={"owner:bound": old_hash})
    assert historical.resolve("bound").refs == _refs({"scripts": ["historical"], "workflows": ["side"]})


# LLM: 去掉正文预读不能丢失原先从头部提取的IO/访问声明和版本字段。
# 函数用途: 验证冻结元数据路径保留既有提取键，不为refs修复制造字段回退。
def test_snapshot_projection_preserves_extraction_fields(tmp_path, skill_catalog_factory):
    home = tmp_path / "catalog"
    path = _skill(home / "owner/skills", "bound", {
        "version": "3.0.0",
        "refs": {"scripts": ["s"]},
        "access_targets": [{"target_id": "fixture", "type": "file", "endpoint": "fixture-input"}],
        "data_sources": [{"source_id": "fixture", "system": "fixture", "query_scope": {"limit": 1}}],
        "credential_refs": [{"credential_ref": "credential-fixture"}],
        "io_contract": {"inputs": ["indicator"], "outputs": ["report"]},
    })
    catalog = skill_catalog_factory(home)
    normal = adapter.extract_skill_descriptor(path)
    frozen = adapter.get_owner_skill_descriptor(home / "owner", "bound", skill_snapshot=catalog.snapshot)
    for key in ("version", "access_targets", "data_sources", "credential_refs", "io_contract"):
        assert frozen[key] == normal[key], key
