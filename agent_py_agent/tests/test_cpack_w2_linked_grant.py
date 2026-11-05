"""文档授权联动工具，但父级快照、只读 preset 与精确工具表始终是上界。"""
from __future__ import annotations

import builtins
import importlib
import importlib.util
import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.create_policy import _role_policy
from agent_py_agent.agent.agent_core.orchestration.tools.capability import (
    ResolveCapabilityRequestsTool,
    _ResolveContext,
)
from agent_py_agent.agent.capability.package_snapshot import CapabilityPackageSnapshot
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot, SkillSnapshotError
from agent_py_agent.agent.capability_package_manifest import CapabilityFile
from agent_py_agent.agent.subagents.capability_scope import (
    DIRECT_PARENT_TOOL_AUTHORITY_ATTR,
    bind_creation_tool_authority,
    current_creation_tool_authority,
)
from agent_py_agent.agent.tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    ToolModelSpec,
    ToolRuntime,
    ToolRuntimePolicy,
    ToolRuntimeSnapshot,
)


def _package(name="sample", runtime="on_demand", names=("plugin__sample__one", "plugin__sample__two")):
    return CapabilityPackageSnapshot(name, "1", "样本", "方法", ("检查",), "CAPABILITY.md", "a" * 64, "b" * 64,
        (CapabilityFile("CAPABILITY.md", "c" * 64),), lambda _: pytest.fail("联动不允许读文档"),
        mcp_runtime=runtime, mcp_tool_names=names if runtime == "on_demand" else ())


def _snapshot(*packages):
    return SkillSnapshot((), (), "fixture", "local_main", "/fixture", packages)


def _authority(names):
    class Tool(BaseTool):
        def __init__(self, name):
            self.model_spec = ToolModelSpec(name, "测试父权限", {"type": "object", "properties": {}})
            self.runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("dangerous"))

        def execute(self, params):
            pytest.fail("授权测试不应调用工具")

    tools = [Tool(name) for name in names]
    runtimes = tuple(ToolRuntime(tool.model_spec, tool.runtime_policy, tool) for tool in tools)
    return ToolRuntimeSnapshot("parent", runtimes, frozenset(names), (), frozenset(names))


def _helper():
    module = "agent_py_agent.agent.capability.pack_tool_grant"
    assert importlib.util.find_spec(module) is not None, "缺少联动授权纯函数"
    return importlib.import_module(module).pack_linked_tool_names


def test_linked_names_preserve_reference_order_deduplicate_and_never_read_files(monkeypatch):
    helper = _helper()
    snapshot = _snapshot(_package(), _package("other", names=("plugin__sample__two", "plugin__other__three")),
        _package("docs", "none"), _package("foreign", "owner_unsupported"))
    calls = []
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", lambda *a, **kw: calls.append("open"))
        patch.setattr(os, "stat", lambda *a, **kw: calls.append("stat"))
        names = helper(snapshot, ["builtin:method", "capability:other", "capability:sample", "capability:other",
            "capability:missing", "capability:docs", "capability:foreign", None])
    assert names == ("plugin__sample__two", "plugin__other__three", "plugin__sample__one")
    assert calls == []


@pytest.mark.parametrize("raw,expected", [({}, ["plugin__sample__one"]),
    ({"tool_preset": "read_only"}, []), ({"_exact_allowed_tools": True}, [])])
def test_creation_only_links_parent_held_names_and_never_expands_readonly_or_exact(raw, expected):
    agent = SimpleNamespace(current_skill_snapshot=lambda: _snapshot(_package()))
    with bind_creation_tool_authority(_authority(["plugin__sample__one"])):
        answer = _role_policy(agent, {"role": "worker", "allowed_skills": ["capability:sample"], **raw}, "检查", [])
    assert answer.allowed_tools == expected


@pytest.mark.parametrize("runtime", ["none", "owner_unsupported"])
def test_creation_does_not_link_docs_or_owner_unsupported(runtime):
    agent = SimpleNamespace(current_skill_snapshot=lambda: _snapshot(_package(runtime=runtime)))
    with bind_creation_tool_authority(_authority(["plugin__sample__one"])):
        assert _role_policy(agent, {"allowed_skills": ["capability:sample"]}, "检查", []).allowed_tools == []


@pytest.mark.parametrize("missing", [True, False])
def test_creation_snapshot_unavailable_fails_closed(missing):
    def unavailable():
        raise SkillSnapshotError("SKILL_READ_FAILED")
    agent = SimpleNamespace() if missing else SimpleNamespace(current_skill_snapshot=unavailable)
    with bind_creation_tool_authority(_authority(["plugin__sample__one"])):
        assert _role_policy(agent, {"allowed_skills": ["capability:sample"]}, "检查", []).allowed_tools == []


def _grant(agent, names, *, explicit=(), skills=("capability:sample",), raw=None):
    from agent_py_agent.agent.agent_core.orchestration.create_policy import create_task_attributes

    if not hasattr(agent, "subagents"):
        agent.subagents = SimpleNamespace()
    with bind_creation_tool_authority(_authority(names)):
        attrs = create_task_attributes(raw or {})
    task = SimpleNamespace(id="child", parent_id="parent", attributes=attrs)
    request = SimpleNamespace(id="request", requested_tools=list(explicit), requested_skills=list(skills))
    return ResolveCapabilityRequestsTool(agent)._mark_grant(_ResolveContext(task, "grant", {}, "授权"), request)


@pytest.mark.parametrize("raw", [{"tool_preset": "read_only"}, {"_exact_allowed_tools": True}])
def test_resolution_never_implicitly_expands_readonly_or_exact_child(raw):
    from agent_py_agent.agent.agent_core.orchestration.create_policy import create_task_attributes

    agent = SimpleNamespace(current_skill_snapshot=lambda: _snapshot(_package()))
    attrs = create_task_attributes({**raw, "attributes": {"pack_linked_tools_allowed": True}})
    assert attrs.get("pack_linked_tools_allowed") is False
    result = _grant(agent, ["plugin__sample__one"], raw=raw)
    assert result["ok"] is True and result["skills"] == ["capability:sample"]
    assert result["tools"] == []
    explicit = _grant(agent, ["plugin__sample__one"], raw=raw, explicit=["plugin__sample__one"])
    assert explicit["ok"] is True and explicit["tools"] == ["plugin__sample__one"]


def test_resolution_links_only_grantable_names_and_keeps_the_original_record_schema():
    agent = SimpleNamespace(current_skill_snapshot=lambda: _snapshot(_package()))
    result = _grant(agent, ["plugin__sample__one"])
    assert result["ok"] is True and result["tools"] == ["plugin__sample__one"]
    assert result["mcp_tools"] == [] and result["skills"] == ["capability:sample"]
    assert result["capability_cards"][0]["activation_id"] == "b" * 64
    assert result["parent_tool_authority"]["grantable_tools"] == ["plugin__sample__one"]
    assert set(result) == {"ok", "request_id", "status", "path_scope", "tools", "mcp_tools", "skills",
                           "capability_cards", "parent_tool_authority"}


def test_legacy_child_without_creation_proof_gets_docs_but_no_implicit_tools():
    from agent_py_agent.agent.agent_core.orchestration.create_policy import create_task_attributes

    agent = SimpleNamespace(current_skill_snapshot=lambda: _snapshot(_package()), subagents=SimpleNamespace())
    with bind_creation_tool_authority(_authority(["plugin__sample__one"])):
        attrs = create_task_attributes({})
    attrs.pop("pack_linked_tools_allowed")
    task = SimpleNamespace(id="child", parent_id="parent", attributes=attrs)
    request = SimpleNamespace(id="request", requested_tools=[], requested_skills=["capability:sample"])
    result = ResolveCapabilityRequestsTool(agent)._mark_grant(_ResolveContext(task, "grant", {}, "授权"), request)
    assert result["ok"] is True and result["skills"] == ["capability:sample"]
    assert result["tools"] == []
    request.requested_tools = ["plugin__sample__one"]
    explicit = ResolveCapabilityRequestsTool(agent)._mark_grant(_ResolveContext(task, "grant", {}, "显式授权"), request)
    assert explicit["ok"] is True and explicit["tools"] == ["plugin__sample__one"]


def test_unavailable_explicit_tool_is_refused_but_unavailable_linked_tool_is_not():
    agent = SimpleNamespace(current_skill_snapshot=lambda: _snapshot(_package()))
    assert _grant(agent, [])["ok"] is True
    refused = _grant(agent, [], explicit=["plugin__sample__one"])
    assert refused["ok"] is False and refused["error_code"] == "MISSING_CAPABILITY"
    assert refused["unavailable_tools"] == ["plugin__sample__one"]


def _headless_sequence(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability.package_provider import enabled_capability_packages
    from agent_py_agent.tests.test_cpack_w2_enable_pin_catalog import _confirmed, _service
    from agent_py_agent.tests.test_resolve_capability_requests_tool import _agent_and_blocked_task

    service, _ = _service(tmp_path)
    enabled, _, _ = _confirmed(service)
    assert enabled["state"] == "succeeded", enabled
    packages = enabled_capability_packages(service.context.owner)
    assert packages and packages[0].mcp_tool_names
    parent_names = packages[0].mcp_tool_names
    snapshot = _snapshot(*packages)
    parent = SimpleNamespace(current_skill_snapshot=lambda: snapshot)
    with bind_creation_tool_authority(_authority(parent_names)):
        created = _role_policy(parent, {"allowed_skills": ["capability:w2-pack"]}, "检查", [])
    assert tuple(created.allowed_tools) == parent_names
    # 使用真实 lifecycle 保存申请与授权；W3 代理未在此分支，父工具由已有创建快照承运。
    agent, task, _ = _agent_and_blocked_task(str(tmp_path / "child"))
    monkeypatch.setattr(agent, "current_skill_snapshot", lambda: snapshot)
    with bind_creation_tool_authority(_authority(parent_names)):
        current = agent.subagents.load(task.id)
        current.parent_id = "parent"
        current.attributes[DIRECT_PARENT_TOOL_AUTHORITY_ATTR] = current_creation_tool_authority()
        from agent_py_agent.agent.agent_core.orchestration.create_policy import (
            create_task_attributes,
        )

        current.attributes.update(create_task_attributes({}))
        agent.subagents.save(current)
    from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityRequestParams

    request = agent.subagents.lifecycle.record_capability_request(task.id,
        RecordCapabilityRequestParams(problem="需要包工具", needed_capability="pack", requested_skills=["capability:w2-pack"]))
    granted = ResolveCapabilityRequestsTool(agent).execute({"run_id": task.id, "request_id": request.id,
        "decision": "grant", "reason": "同包联动"})
    assert granted.ok, granted.output
    saved = agent.subagents.load(task.id)
    assert set(parent_names).issubset(set(saved.capability_grants[-1].tools))
    disabled = service.command("/plugins disable w2-pack", revision=service.catalog().revision, request_id="disable")
    assert disabled["state"] == "succeeded", disabled
    snapshot = _snapshot(*enabled_capability_packages(service.context.owner))
    assert snapshot.packages == ()
    with bind_creation_tool_authority(_authority([])):
        assert _role_policy(parent, {"allowed_skills": ["capability:w2-pack"]}, "检查", []).allowed_tools == []
    assert _grant(parent, [], skills=["capability:w2-pack"])["ok"] is False


def test_headless_enable_create_disable_and_capability_request_sequence(tmp_path, monkeypatch):
    _headless_sequence(tmp_path, monkeypatch)


def test_component_headless_sequence_with_canonical_skill_grant(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_cpack_w2_enable_pin_catalog import _component_transport

    _component_transport(monkeypatch)
    _headless_sequence(tmp_path, monkeypatch)
