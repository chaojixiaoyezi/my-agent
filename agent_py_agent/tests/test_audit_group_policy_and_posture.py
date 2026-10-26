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


# LLM: 只在临时来源owner里构造政策，不改变caller；库存核对在夹具写完之后开始。
# 函数用途: 写入一次测试用技能启停策略并返回准确路径。
def write_skill_policy(root, payload):
    path = root / "owners/local/main/data/skill_policy.json"
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


def test_skill_revision_includes_canonical_policy_and_metadata_disable_still_wins(tmp_path):
    invocation_assets(tmp_path)
    reader = ReadonlyAssetStore(tmp_path, "local/main")
    original = reader.get("skill", "source_skill")
    assert original.get("enabled", True) is True
    write_skill_policy(tmp_path, '{"disabled_skills":["other-b","other-a","other-b"]}')
    first = reader.get("skill", "source_skill")
    assert first["revision"] != original["revision"] and first["enabled"] is True
    write_skill_policy(tmp_path, '{"disabled_skills":["other-a","other-b"]}')
    assert reader.get("skill", "source_skill")["revision"] == first["revision"]
    write_skill_policy(tmp_path, '{"disabled_skills":[]}')
    asset(tmp_path, "skill", "source_skill", enabled=False)
    assert reader.get("skill", "source_skill")["enabled"] is False


def test_caller_skill_policy_does_not_override_source_owner(tmp_path):
    from agent_py_agent.agent.audit_group.source_snapshot import SourceAssetSnapshot

    invocation_assets(tmp_path)
    caller_policy = tmp_path / "owners/local/alice/data/skill_policy.json"
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
