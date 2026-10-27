# LLM: 真实临时owner/只读资产/工具入口回归；执行替身仅防止测试启动隔离进程，不替代原生隔离验收。
# 模块用途: 验证来源技能策略的拒绝、修订指纹以及态势owner筛选和错误去重。
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.audit_group.access_policy import AuditGroupError
from agent_py_agent.agent.audit_group.blackbox_invoke_service import AuditGroupBlackboxInvokeService
from agent_py_agent.agent.audit_group.observation_service import AuditGroupObservationService
from agent_py_agent.agent.audit_group.readonly_store import ReadonlyAssetStore
from agent_py_agent.agent.tooling.audit_group_tools import AuditGroupQueryTool
from agent_py_agent.tests.test_audit_group_observation_and_invoke import (
    account,
    asset,
    inventory,
    invocation_assets,
    member,
)
from agent_py_agent.tests.test_dual_bot_sessions_and_rollover import business_call
from agent_py_agent.tests.test_dual_bot_sessions_and_rollover import wired_host as wired_host


# LLM: 只在临时来源owner里构造政策，不改变caller；库存核对在夹具写完之后开始。
# 函数用途: 写入一次测试用技能启停策略并返回准确路径。
def write_skill_policy(root, payload):
    path = root / "owners/local/main/skill_policy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path


@pytest.mark.parametrize("identifier", ["source_skill", "owner:source_skill"])
def test_disabled_source_skill_is_not_enabled_or_invocable_in_summary(tmp_path, identifier):
    invocation_assets(tmp_path)
    write_skill_policy(tmp_path, json.dumps({"disabled_skills": [identifier]}))
    source = tmp_path / "owners/local/main"
    before = inventory(source)
    result = AuditGroupObservationService(tmp_path).summarize_assets(
        "local/alice", owner_ids=["local/main"], kinds=["skill"])
    assert result["coverage"] == "complete"
    assert len(result["items"]) == 1
    assert result["items"][0]["enabled"] is False
    assert result["items"][0]["invocable"] is False
    assert inventory(source) == before


@pytest.mark.parametrize("identifier", ["source_skill", "owner:source_skill"])
def test_disabled_source_skill_invoke_rejects_before_execution(tmp_path, monkeypatch, identifier):
    from agent_py_agent.agent.audit_group import blackbox_invoke_service as module

    invocation_assets(tmp_path)
    write_skill_policy(tmp_path, json.dumps({"disabled_skills": [identifier]}))
    source = tmp_path / "owners/local/main"
    before, calls = inventory(source), []
    monkeypatch.setattr(module.IsolatedScriptUnit, "execute", lambda *_a, **_k: calls.append(True) or {"value": 1})
    with pytest.raises(AuditGroupError) as denied:
        AuditGroupBlackboxInvokeService(tmp_path).invoke_asset(
            "local/alice", source_owner="local/main", asset_type="skill", asset_id="source_skill")
    assert (denied.value.code, denied.value.status_code) == ("ASSET_DISABLED_BY_OWNER", 409)
    assert calls == [] and inventory(source) == before


@pytest.mark.parametrize("payload", ["broken", "[]", "null", '{"disabled_skills":{}}',
                                    '{"disabled_skills":"source_skill"}', '{"disabled_skills":[1]}'])
def test_corrupt_skill_policy_observation_is_partial_and_readonly(tmp_path, payload):
    invocation_assets(tmp_path)
    write_skill_policy(tmp_path, payload)
    source = tmp_path / "owners/local/main"
    before = inventory(source)
    result = AuditGroupObservationService(tmp_path).summarize_assets(
        "local/alice", target_owner="local/main", kinds=["skill"])
    assert result["coverage"] == "partial"
    assert result["items"] == []
    assert result["source_errors"] == [{"owner_id": "local/main", "source": "skill", "code": "SOURCE_UNAVAILABLE"}]
    assert inventory(source) == before


@pytest.mark.parametrize("payload", ["broken", "[]", "null", '{"disabled_skills":{}}',
                                    '{"disabled_skills":[false]}'])
def test_corrupt_skill_policy_invoke_returns_source_unavailable(tmp_path, monkeypatch, payload):
    from agent_py_agent.agent.audit_group import blackbox_invoke_service as module

    invocation_assets(tmp_path)
    write_skill_policy(tmp_path, payload)
    source = tmp_path / "owners/local/main"
    before, calls = inventory(source), []
    monkeypatch.setattr(module.IsolatedScriptUnit, "execute", lambda *_a, **_k: calls.append(True) or {"value": 1})
    with pytest.raises(AuditGroupError) as denied:
        AuditGroupBlackboxInvokeService(tmp_path).invoke_asset(
            "local/alice", source_owner="local/main", asset_type="skill", asset_id="source_skill")
    assert (denied.value.code, denied.value.status_code) == ("SOURCE_UNAVAILABLE", 503)
    assert calls == [] and inventory(source) == before


# LLM: 修订绑定规范政策原字节，不以禁用集合等价掩盖来源版本变化；元数据禁用仍优先。
# 函数用途: 验证政策内容变化改变修订且不意外重启已禁用技能。
def test_skill_revision_includes_canonical_policy_and_metadata_disable_still_wins(tmp_path):
    invocation_assets(tmp_path)
    reader = ReadonlyAssetStore(tmp_path, "local/main")
    original = reader.get("skill", "source_skill")
    assert original.get("enabled", True) is True
    write_skill_policy(tmp_path, '{"disabled_skills":["other-b","other-a","other-b"]}')
    first = reader.get("skill", "source_skill")
    assert first["revision"] != original["revision"] and first["enabled"] is True
    write_skill_policy(tmp_path, '{"disabled_skills":["other-a","other-b"]}')
    assert reader.get("skill", "source_skill")["revision"] != first["revision"]
    assert reader.get("skill", "source_skill")["enabled"] is True
    write_skill_policy(tmp_path, '{"disabled_skills":[]}')
    asset(tmp_path, "skill", "source_skill", enabled=False)
    assert reader.get("skill", "source_skill")["enabled"] is False


# LLM: caller规范技能政策不能冒充source政策，冻结必须始终读取准确来源owner。
# 函数用途: 验证改用根政策后仍保留来源隔离。
def test_caller_skill_policy_does_not_override_source_owner(tmp_path):
    from agent_py_agent.agent.audit_group.source_snapshot import SourceAssetSnapshot

    invocation_assets(tmp_path)
    caller_policy = tmp_path / "owners/local/alice/skill_policy.json"
    caller_policy.write_text('{"disabled_skills":["owner:source_skill"]}')
    before = inventory(tmp_path / "owners/local/main")
    snapshot = SourceAssetSnapshot.freeze(tmp_path, "local/main", "skill", "source_skill")
    assert snapshot.record("skill", "source_skill")["enabled"] is True
    assert inventory(tmp_path / "owners/local/main") == before


def test_policy_disable_during_execution_withholds_result(tmp_path, monkeypatch):
    from agent_py_agent.agent.audit_group import blackbox_invoke_service as module

    invocation_assets(tmp_path)

    def execute(*_args, **_kwargs):
        write_skill_policy(tmp_path, '{"disabled_skills":["owner:source_skill"]}')
        return {"value": 1}

    monkeypatch.setattr(module.IsolatedScriptUnit, "execute", execute)
    with pytest.raises(AuditGroupError) as denied:
        AuditGroupBlackboxInvokeService(tmp_path).invoke_asset(
            "local/alice", source_owner="local/main", asset_type="skill", asset_id="source_skill")
    assert (denied.value.code, denied.value.status_code) == ("ASSET_DISABLED_BY_OWNER", 409)
    assert not list((tmp_path / "owners/local/alice/data/audit_group_exec").rglob("result.json"))


# LLM: 账号与资产都用真实登记；三owner各有不同种类，筛选错误会改变公开计数。
# 函数用途: 准备可区分全局、单用户、空选择的态势数据。
def posture_assets(root):
    lifecycle = member(root)
    account(root, "bob")
    asset(root, "script", "main-script")
    for owner, kind in (("alice", "skill"), ("bob", "workflow")):
        folder = root / "owners/local" / owner / ("skills" if kind == "skill" else "workflows") / "local-asset"
        folder.mkdir(parents=True)
        if kind == "skill":
            (folder / "SKILL.md").write_text("---\nname: local-asset\ndescription: fixture\n---\nbody")
        else:
            (folder / "workflow.json").write_text(json.dumps({"id": "local-asset", "kind": kind, "enabled": True}))
    return lifecycle


@pytest.mark.parametrize("entry", ["service", "tool"])
@pytest.mark.parametrize("filters,owners,counts", [
    ({}, 3, {"script": 1, "skill": 1, "workflow": 1, "monitor": 0}),
    ({"owner_ids": ["local/main", "local/main"]}, 1, {"script": 1, "skill": 0, "workflow": 0, "monitor": 0}),
    ({"target_owner": "local/alice"}, 1, {"script": 0, "skill": 1, "workflow": 0, "monitor": 0}),
    ({"owner_ids": []}, 0, {"script": 0, "skill": 0, "workflow": 0, "monitor": 0}),
    ({"owner_ids": ["local/bob"], "target_owner": "local/main"}, 1,
     {"script": 1, "skill": 0, "workflow": 0, "monitor": 0}),
])
def test_posture_service_and_query_tool_use_same_owner_filters(tmp_path, entry, filters, owners, counts):
    posture_assets(tmp_path)
    before = inventory(tmp_path / "owners")
    if entry == "service":
        result = AuditGroupObservationService(tmp_path).posture_overview("local/alice", **filters)
    else:
        outcome = AuditGroupQueryTool(tmp_path, "local/alice").execute({"action": "posture", **filters})
        assert outcome.ok, outcome.reported_error_code
        result = json.loads(outcome.output)
    assert result["owners_total"] == result["enabled_owners"] == owners
    assert result["assets_by_kind"] == counts
    assert result["coverage"] == "complete" and result["source_errors"] == []
    assert inventory(tmp_path / "owners") == before


def test_filtered_posture_does_not_duplicate_registered_owner_errors(tmp_path):
    posture_assets(tmp_path)
    (tmp_path / "owners/local/bob/owner_status.json").write_text("[]")
    result = AuditGroupObservationService(tmp_path).posture_overview("local/alice", owner_ids=["local/main"])
    assert result["owners_total"] == 1 and result["coverage"] == "partial"
    assert result["source_errors"] == [{"owner_id": "local/bob", "source": "owner_status", "code": "SOURCE_UNAVAILABLE"}]


@pytest.mark.parametrize("filters,code,status", [({"owner_ids": ["local/missing"]}, "OWNER_NOT_FOUND", 404),
    ({"target_owner": "local/missing"}, "OWNER_NOT_FOUND", 404),
    ({"owner_ids": "local/main"}, "INVALID_OWNER_SELECTION", 400),
    ({"owner_ids": [True]}, "INVALID_OWNER_SELECTION", 400)])
def test_posture_filters_keep_existing_owner_validation(tmp_path, filters, code, status):
    member(tmp_path)
    with pytest.raises(AuditGroupError) as denied:
        AuditGroupObservationService(tmp_path).posture_overview("local/alice", **filters)
    assert (denied.value.code, denied.value.status_code) == (code, status)


def test_filtered_posture_does_not_bypass_membership_revocation(tmp_path):
    lifecycle = member(tmp_path)
    service = AuditGroupObservationService(tmp_path)
    tool = AuditGroupQueryTool(tmp_path, "local/alice")
    lifecycle.set_audit_group_membership("alice", in_audit_group=False)
    with pytest.raises(AuditGroupError, match="AUDIT_GROUP_ACCESS_DENIED"):
        service.posture_overview("local/alice", target_owner="local/main")
    assert tool.execute({"action": "posture", "target_owner": "local/main"}).reported_error_code == "AUDIT_GROUP_ACCESS_DENIED"


# LLM: 原始策略字节与来源路径都属于冻结修订，两个路径必须分别贡献禁用项，不能只合并已去重的禁用列表。
# 函数用途: 覆盖规范/旧路径的并集、空白变化及非禁用字段变化的修订失效。
def test_r3_skill_policy_paths_union_and_both_revision_inputs(tmp_path):
    invocation_assets(tmp_path)
    source = tmp_path / "owners/local/main"
    canonical = write_skill_policy(tmp_path, '{"disabled_skills":["canonical-only"]}')
    legacy = source / "data/skill_policy.json"
    legacy.write_text('{"disabled_skills":["owner:source_skill"]}')
    reader = ReadonlyAssetStore(tmp_path, "local/main")
    both = reader.get("skill", "source_skill")
    assert both["enabled"] is False
    legacy.write_text('{"disabled_skills":[]}')
    only_canonical = reader.get("skill", "source_skill")
    assert only_canonical["enabled"] is True and only_canonical["revision"] != both["revision"]
    canonical.write_text('{"disabled_skills":["source_skill"]}')
    assert reader.get("skill", "source_skill")["enabled"] is False
    original = reader.get("skill", "source_skill")["revision"]
    canonical.write_text('{ "disabled_skills" : ["source_skill"] }')
    spaced = reader.get("skill", "source_skill")["revision"]
    assert spaced != original
    legacy.write_text('{"disabled_skills":[],"updated_by":"policy-revision"}')
    assert reader.get("skill", "source_skill")["revision"] != spaced


# LLM: 任一已存在路径损坏均拒绝，另一个有效策略不能覆盖它；观察和冻结不能改变原件。
# 函数用途: 验证两来源的坏JSON、坏schema、链接及不可读路径均封闭拒绝。
@pytest.mark.parametrize("layout", ["canonical", "legacy"])
@pytest.mark.parametrize("damage", ["json", "schema", "duplicate", "nan", "symlink", "unreadable"])
def test_r3_either_skill_policy_path_fail_closes(tmp_path, layout, damage):
    invocation_assets(tmp_path)
    source = tmp_path / "owners/local/main"
    canonical = write_skill_policy(tmp_path, '{"disabled_skills":[]}')
    legacy = source / "data/skill_policy.json"
    legacy.write_text('{"disabled_skills":[]}')
    path = canonical if layout == "canonical" else legacy
    if damage == "symlink":
        path.unlink()
        path.symlink_to(legacy if layout == "canonical" else canonical)
    elif damage == "unreadable":
        path.unlink()
        path.mkdir()
    else:
        path.write_text({"json": "broken", "schema": '{"disabled_skills":[false]}',
                         "duplicate": '{"disabled_skills":[],"disabled_skills":[]}',
                         "nan": '{"disabled_skills":[],"version":NaN}'}[damage])
    before = inventory(source)
    result = AuditGroupObservationService(tmp_path).summarize_assets("local/alice", target_owner="local/main", kinds=["skill"])
    assert result["coverage"] == "partial" and not result["items"]
    with pytest.raises(AuditGroupError) as denied:
        AuditGroupBlackboxInvokeService(tmp_path).invoke_asset("local/alice", source_owner="local/main",
                                                               asset_type="skill", asset_id="source_skill")
    assert (denied.value.code, denied.value.status_code) == ("SOURCE_UNAVAILABLE", 503)
    assert inventory(source) == before


# LLM: 使用真实Host授权、schema与领域转发，不能只验证服务已支持target_owner而忽略Host漏传。
# 函数用途: 验证Host的指定目标与双参数优先级，不把单目标查询扩大成全局态势。
@pytest.mark.parametrize("filters,counts", [
    ({"target_owner": "local/alice"}, {"script": 0, "skill": 1, "workflow": 0, "monitor": 0}),
    ({"owner_ids": ["local/main"], "target_owner": "local/bob"}, {"script": 0, "skill": 0, "workflow": 1, "monitor": 0}),
    ({"owner_ids": [], "target_owner": "local/main"}, {"script": 1, "skill": 0, "workflow": 0, "monitor": 0}),
])
def test_r3_host_posture_forwards_target_owner(wired_host, filters, counts):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    root = wired_host[2].root
    posture_assets(root)
    before = inventory(root / "owners")
    result = business_call(wired_host, "audit_group.posture.overview", filters, caller=OwnerIdentity.local_user("alice"))
    assert result.http_status == 200
    assert result.data["owners_total"] == 1 and result.data["assets_by_kind"] == counts
    assert inventory(root / "owners") == before
