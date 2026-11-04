"""验证 L1 绑定、顶层选择与只读描述符边界，不执行任何固化方法。"""
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.capability import decision_candidates as candidates
from agent_py_agent.agent.capability.router import CapabilityRouter
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.capability.skill_snapshot import (
    SkillSnapshot,
    SkillSnapshotEntry,
    skill_content_sha256,
)


# LLM: 快照字段来自生产冻结条目；显式空引用也按原格式冻结，不从正文推断绑定。
# 函数用途: 创建可真实读取正文、具备固定 owner 的最小方法环境。
def _surface(tmp_path, *, refs=None, metadata=None, name="review"):
    owner, workspace = tmp_path / "owner", tmp_path / "workspace"
    path = owner / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    workspace.mkdir()
    body = f"---\nname: {name}\ndescription: inventory review\n---\n审阅流程正文\n"
    path.write_text(body, encoding="utf-8")
    entry = SkillSnapshotEntry(f"owner:{name}", name, "inventory review", str(path), "owner", "review", skill_content_sha256(body))
    frozen = replace(entry, refs=refs or {"scripts": [], "workflows": [], "skills": [], "monitors": []},
                     asset_metadata=metadata or {})
    snapshot = SkillSnapshot((frozen,), (), "fixed", "owner-test", str(workspace))
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner, owner_id="owner-test"),
                            tools=SimpleNamespace(workspace_root=workspace),
                            _current_run_params=SimpleNamespace(context_scope="default", parent_id="", task_attributes={}),
                            _current_skill_snapshot=snapshot)
    agent.current_skill_snapshot = lambda: agent._current_skill_snapshot
    agent.capability_router = CapabilityRouter(skill_snapshot_provider=agent.current_skill_snapshot)
    return agent


# LLM: 保存仅用于 fixture 准备；读取阶段另设防线，不将测试数据写进真实 home。
# 函数用途: 通过原 Store 建立脚本描述符与可识别代码附件。
def _script(agent, identifier, *, metadata=None, enabled=True, workspace=False):
    store = ScriptStore(agent.home_paths.owner_home_dir, workspace_root=agent.tools.workspace_root)
    return store.save_script(AssetDescriptor(id=identifier, kind="script", name=identifier,
        summary=f"inventory {identifier}", metadata=metadata or {}, enabled=enabled), "CODE-MUST-NOT-BE-READ",
        target_scope="workspace" if workspace else "owner")


# LLM: refs.scripts 从原工作流步骤汇总，额外工作流引用仍按描述符保留。
# 函数用途: 用现有 Store 建立可见或隐藏工作流。
def _workflow(agent, identifier, *, scripts=(), metadata=None, enabled=True, workflows=()):
    steps = tuple(WorkflowStepSpec(f"step-{index}", "review", "deterministic_script", script_id=script)
                  for index, script in enumerate(scripts))
    descriptor = AssetDescriptor(id=identifier, kind="workflow", name=identifier, summary=f"inventory {identifier}",
        refs={"workflows": list(workflows)}, metadata=metadata or {}, enabled=enabled)
    return WorkflowStore(agent.home_paths.owner_home_dir).save_workflow(
        WorkflowDefinition(descriptor, steps, body_markdown="WORKFLOW-SIDE-FILE-MUST-NOT-BE-READ"))


# LLM: 只构造原 Tool snapshot 的必要属性；实际选择仍调用生产候选及映射函数。
# 函数用途: 为公开方法候选绑定当前 agent。
def _tool_snapshot(agent):
    handler = SkillSearchTool(agent)
    runtime = SimpleNamespace(handler=handler, model_spec=handler.model_spec,
                              exposure=SimpleNamespace(model_visible=True))
    return SimpleNamespace(runtimes=(runtime,))


# LLM: 新字段必须携带真实直接绑定信息，不能以 bound_count 或递归后代冒充。
# 函数用途: 验证 get 合并显式 refs 与 member_of 并按 kind/id 排序。
def test_get_returns_sorted_direct_bound_methods(tmp_path):
    agent = _surface(tmp_path, refs={"scripts": ["z-child", "z-child"], "workflows": ["nested"]})
    _script(agent, "z-child")
    _script(agent, "a-child", metadata={"member_of": ["skill:owner:review", {"kind": "skill", "id": "review"}]})
    _script(agent, "grandchild")
    _workflow(agent, "nested", scripts=("grandchild",))
    result = SkillSearchTool(agent).execute({"action": "get", "skill_id": "owner:review"})
    assert result.ok
    payload = json.loads(result.output)
    assert payload["body"].endswith("审阅流程正文\n")
    assert payload["bound_methods"] == [
        {"kind": "script", "id": "a-child", "name": "a-child", "summary": "inventory a-child"},
        {"kind": "script", "id": "z-child", "name": "z-child", "summary": "inventory z-child"},
        {"kind": "workflow", "id": "nested", "name": "nested", "summary": "inventory nested"},
    ]


# LLM: 搜索全集仍保留嵌套子项；每个可见父只出现一次，监控不构成父。
# 函数用途: 验证脚本与工作流 search 命中携带准确 parents。
def test_search_preserves_children_and_visible_parents(tmp_path):
    agent = _surface(tmp_path, refs={"scripts": ["shared-child"], "workflows": ["nested"]})
    _script(agent, "shared-child")
    _workflow(agent, "nested", scripts=("shared-child",))
    result = SkillSearchTool(agent).execute({"action": "search", "query": "inventory", "limit": 20})
    assert result.ok
    matches = {row["stable_id"]: row for row in json.loads(result.output)["matches"] if row["kind"] != "skill"}
    assert matches["script:owner:shared-child"]["parents"] == [
        {"kind": "skill", "id": "owner:review"}, {"kind": "workflow", "id": "nested"}]
    assert matches["workflow:owner:nested"]["parents"] == [{"kind": "skill", "id": "owner:review"}]
    assert matches["script:owner:shared-child"]["next_read"]["arguments"] == {"action": "get", "script_id": "shared-child"}


# LLM: 子项只能被当前可见父折叠；隐藏、停用和不在快照里的声明不能消失子项。
# 函数用途: 验证不可见父被忽略，隐藏子本身也不进入搜索卡。
def test_invisible_parents_promote_children_and_hidden_methods_are_not_searchable(tmp_path):
    agent = _surface(tmp_path)
    _script(agent, "orphan", metadata={"member_of": ["workflow:hidden", "workflow:disabled", "skill:missing"]})
    _script(agent, "secret", metadata={"hidden": True})
    _workflow(agent, "hidden", scripts=("orphan",), metadata={"hidden": True})
    _workflow(agent, "disabled", scripts=("orphan",), enabled=False)
    cards = candidates.asset_cards_for_agent(agent)
    assert [card.id for card in cards] == ["script:owner:orphan"]
    result = SkillSearchTool(agent).execute({"action": "search", "query": "orphan"})
    row = json.loads(result.output)["matches"][0]
    assert not row.get("parents")
    rows = candidates.capability_candidates(_tool_snapshot(agent), agent.current_skill_snapshot(), categories=[], skills_discoverable=True)
    assert {row["ref"] for row in rows} == {"owner:review", "script:owner:orphan"}


# LLM: 子项不单独出题；宿主只在父被明确采用后展开既有精确引用，不改变授权。
# 函数用途: 验证顶层候选、传递选择继承与共享子去重。
def test_only_top_level_candidates_and_descendants_inherit_selection(tmp_path):
    agent = _surface(tmp_path, refs={"workflows": ["nested"], "scripts": ["shared"]})
    _script(agent, "shared")
    _script(agent, "deep")
    _workflow(agent, "nested", scripts=("shared", "deep"))
    _script(agent, "standalone")
    rows = candidates.capability_candidates(_tool_snapshot(agent), agent.current_skill_snapshot(), categories=[], skills_discoverable=True)
    assert {row["ref"] for row in rows} == {"owner:review", "script:owner:standalone"}
    questions, _ = candidates.selection_questions(rows)
    assert len(questions) == len(rows)
    assert all("inherited_candidates" not in question["instructions"]["candidate"] for question in questions.values())
    answers = tuple(SimpleNamespace(question_id=key, error_code="", kind="choice",
        value="include" if rows[index]["ref"] == "owner:review" else "not_needed")
        for index, key in enumerate(questions))
    selected, reason = candidates.selected_capabilities(SimpleNamespace(answers=answers), questions, rows)
    assert reason == ""
    assert {row["ref"] for row in selected} == {"owner:review", "workflow:owner:nested", "script:owner:shared", "script:owner:deep"}
    assert len(selected) == len({row["ref"] for row in selected})
    assert {card.id for card in candidates.asset_cards_for_agent(agent)} == {
        "workflow:owner:nested", "script:owner:shared", "script:owner:deep", "script:owner:standalone"}


# LLM: 使用真实 Store 的只读视图；调用全量 get 或读取附件均立即失败。
# 函数用途: 验证共享 loader 仅列描述符并保留 workspace 优先与隐藏 composition 元数据。
def test_loader_is_readonly_descriptor_only_and_workspace_wins(tmp_path, monkeypatch):
    agent = _surface(tmp_path, name="中文方法", refs={"scripts": ["same"]})
    _script(agent, "same")
    _script(agent, "same", workspace=True)
    _workflow(agent, "monitor-job", metadata={"hidden": True, "composition": {"base_workflow_id": "base"}})
    before = {str(path): path.stat().st_mtime_ns for path in tmp_path.rglob("*")}
    original = ScriptStore._text

    # LLM: 准备阶段已经保存完毕，读取阶段不允许碰脚本和工作流附件。
    # 函数用途: 钉住冷清单的文件访问边界。
    def checked_text(self, asset_id, filename):
        assert filename not in {"main.py", "WORKFLOW.md"}
        return original(self, asset_id, filename)

    # LLM: 描述符路径不能绕到会读取脚本代码的详情路径。
    # 函数用途: 拒绝脚本 get 调用。
    def forbidden_get(self, script_id):
        raise AssertionError("descriptor list cannot get script code")

    monkeypatch.setattr(ScriptStore, "_text", checked_text)
    monkeypatch.setattr(WorkflowStore, "_text", checked_text)
    monkeypatch.setattr(ScriptStore, "_get", forbidden_get)
    monkeypatch.setattr(ScriptStore, "get_script", forbidden_get)
    descriptors = candidates.method_descriptors_for_agent(agent)
    by_key = {(row["kind"], row["id"]): row for row in descriptors}
    assert by_key[("script", "same")]["source_scope"] == "workspace"
    assert by_key[("workflow", "monitor-job")]["metadata"]["composition"]["base_workflow_id"] == "base"
    skill = next(row for row in descriptors if row["kind"] == "skill")
    assert skill["metadata"]["stable_id"] == "owner:中文方法" and skill["metadata"]["source"] == "owner"
    assert ":" not in skill["id"] and skill["id"].isascii()
    assert {str(path): path.stat().st_mtime_ns for path in tmp_path.rglob("*")} == before


# LLM: 范围裁决必须先于任何固定资产 I/O，已有冻结技能描述符可继续来自原快照。
# 函数用途: 验证受限范围和 owner 不符时不枚举 Store/monitor。
@pytest.mark.parametrize("scope,parent,runner,owner", [
    ("task_local", "", "", "owner-test"), ("isolated", "", "", "owner-test"),
    ("control_plane", "", "", "owner-test"), ("default", "parent", "", "owner-test"),
    ("default", "", "child", "owner-test"), ("default", "", "", "other-owner"),
])
def test_loader_rejects_fixed_inventory_before_io(tmp_path, monkeypatch, scope, parent, runner, owner):
    from agent_py_agent.agent.assets import monitor_adapter, workspace_assets
    agent = _surface(tmp_path)
    agent._current_run_params = SimpleNamespace(context_scope=scope, parent_id=parent, task_attributes={})
    agent._current_subagent_run_id = runner
    agent._current_skill_snapshot = replace(agent.current_skill_snapshot(), owner_id=owner)

    # LLM: 所有固定资产入口都禁止调用，不靠空磁盘掩盖范围校验缺失。
    # 函数用途: 拒绝目录视图建立。
    def forbidden_store(root, kind):
        raise AssertionError("fixed inventory forbidden")

    # LLM: 监控入口也必须服从同一范围边界。
    # 函数用途: 拒绝监控枚举。
    def forbidden_monitors(owner_home, scheduler_jobs=()):
        raise AssertionError("monitor inventory forbidden")

    monkeypatch.setattr(workspace_assets, "_store_view", forbidden_store)
    monkeypatch.setattr(monitor_adapter, "list_monitor_descriptors", forbidden_monitors)
    descriptors = candidates.method_descriptors_for_agent(agent)
    assert not any(row["kind"] in {"script", "workflow", "monitor"} for row in descriptors)
    assert candidates.asset_cards_for_agent(agent) == []


# LLM: 冷查询缺失根不能通过 mkdir 或锁文件使自己成功。
# 函数用途: 验证空目录清单没有写盘副作用。
def test_loader_missing_roots_do_not_create_directories(tmp_path):
    snapshot = SkillSnapshot((), (), "empty", "owner-test", str(tmp_path / "missing-workspace"))
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path / "missing-owner", owner_id="owner-test"),
                            tools=SimpleNamespace(workspace_root=tmp_path / "missing-workspace"),
                            current_skill_snapshot=lambda: snapshot)
    assert candidates.method_descriptors_for_agent(agent) == []
    assert list(tmp_path.iterdir()) == []


# LLM: 新旧监控 refs 都只保留为徽标输入，不给脚本建立可见父或单独候选。
# 函数用途: 验证 loader 沿监控公开列表读取，搜索和选择均不把监控当父。
@pytest.mark.parametrize("refs", [{"scripts": ["solo"]}, [{"kind": "script", "id": "solo"}]])
def test_monitors_are_badges_not_method_parents(tmp_path, monkeypatch, refs):
    from agent_py_agent.agent.assets import monitor_adapter
    agent = _surface(tmp_path)
    _script(agent, "solo")
    calls = []

    # LLM: 通过唯一公共监控清单接口投影两代 refs，不读取绑定 raw 文件补图。
    # 函数用途: 提供精确监控描述符并记录 owner 来源。
    def monitors(owner_home, scheduler_jobs=()):
        calls.append(owner_home)
        return [{"kind": "monitor", "id": "watch", "name": "watch", "summary": "inventory watch",
                 "refs": refs, "metadata": {"alias": "watch"}}]

    monkeypatch.setattr(monitor_adapter, "list_monitor_descriptors", monitors)
    descriptors = candidates.method_descriptors_for_agent(agent)
    assert next(row for row in descriptors if row["kind"] == "monitor")["refs"] == refs
    assert calls == [agent.home_paths.owner_home_dir]
    rows = candidates.capability_candidates(_tool_snapshot(agent), agent.current_skill_snapshot(), categories=[], skills_discoverable=True)
    assert {row["ref"] for row in rows} == {"owner:review", "script:owner:solo"}
    result = SkillSearchTool(agent).execute({"action": "search", "query": "solo"})
    row = json.loads(result.output)["matches"][0]
    assert row["kind"] == "script" and not row.get("parents")


# LLM: 从当前 snapshot 移除或禁用父后，子项保持公开可查，不能靠旧的 member_of 折叠或隐藏。
# 函数用途: 验证冻结技能可见性改变会同步解除绑定。
@pytest.mark.parametrize("disabled", [False, True])
def test_snapshot_absent_or_disabled_skill_cannot_bind_child(tmp_path, disabled):
    agent = _surface(tmp_path, refs={"scripts": ["orphan"]})
    _script(agent, "orphan", metadata={"member_of": [{"kind": "skill", "id": "review"}]})
    snapshot = agent.current_skill_snapshot()
    entries = (replace(snapshot.entries[0], enabled=False),) if disabled else ()
    agent._current_skill_snapshot = replace(snapshot, entries=entries)
    result = SkillSearchTool(agent).execute({"action": "search", "query": "orphan"})
    assert result.ok
    assert not json.loads(result.output)["matches"][0].get("parents")
    rows = candidates.capability_candidates(_tool_snapshot(agent), agent.current_skill_snapshot(), categories=[], skills_discoverable=True)
    assert [row["ref"] for row in rows] == ["script:owner:orphan"]


# LLM: SCC 代表由共享纯树选择；选择继承遍历不得因自环或互引无限展开。
# 函数用途: 验证工作流循环只有最小根出题，采用时覆盖整个环。
def test_cycle_has_one_candidate_and_inherits_without_duplicates(tmp_path):
    agent = _surface(tmp_path)
    _workflow(agent, "a-cycle", workflows=("b-cycle",))
    _workflow(agent, "b-cycle", workflows=("a-cycle",))
    rows = candidates.capability_candidates(_tool_snapshot(agent), agent.current_skill_snapshot(), categories=[], skills_discoverable=True)
    assert {row["ref"] for row in rows} == {"owner:review", "workflow:owner:a-cycle"}
    questions, _ = candidates.selection_questions(rows)
    answers = tuple(SimpleNamespace(question_id=key, error_code="", kind="choice", value="include") for key in questions)
    selected, reason = candidates.selected_capabilities(SimpleNamespace(answers=answers), questions, rows)
    assert reason == ""
    assert {row["ref"] for row in selected} == {"owner:review", "workflow:owner:a-cycle", "workflow:owner:b-cycle"}
    assert len(selected) == len({row["ref"] for row in selected})


# LLM: 引用本身不使停用或隐藏方法复活，完整搜索也不能通过另一个父绕过可见性。
# 函数用途: 验证 get 的直接绑定只含可见启用子项。
def test_get_excludes_disabled_and_hidden_children(tmp_path):
    agent = _surface(tmp_path, refs={"scripts": ["visible", "disabled", "secret"], "workflows": ["hidden-flow"]})
    _script(agent, "visible")
    _script(agent, "disabled", enabled=False)
    _script(agent, "secret", metadata={"hidden": True})
    _workflow(agent, "hidden-flow", metadata={"hidden": True})
    result = SkillSearchTool(agent).execute({"action": "get", "skill_id": "owner:review"})
    assert result.ok
    assert json.loads(result.output)["bound_methods"] == [
        {"kind": "script", "id": "visible", "name": "visible", "summary": "inventory visible"}]


# LLM: loader 消费的是冻结 refs，不重新打开 SKILL 或 sidecar；metadata 中的伪造来源也不能覆盖冻结身份。
# 函数用途: 验证描述符来源准确且不在清单入口预读技能正文。
def test_loader_uses_frozen_skill_refs_and_authoritative_source(tmp_path, monkeypatch):
    agent = _surface(tmp_path, refs={"scripts": ["frozen"]}, metadata={"stable_id": "fake", "source": "workspace"})
    original = Path.read_text

    # LLM: 快照构造已结束，清单调用不得再次预读技能正文或扩展 sidecar。
    # 函数用途: 拦截不属于描述符清单的技能文件读取。
    def checked_read(path, encoding=None, errors=None):
        assert path.name not in {"SKILL.md", "skill_meta.json"}
        return original(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", checked_read)
    descriptors = candidates.method_descriptors_for_agent(agent)
    skill = next(row for row in descriptors if row["kind"] == "skill")
    assert skill["refs"] == {"scripts": ["frozen"]}
    assert skill["metadata"]["stable_id"] == "owner:review"
    assert skill["metadata"]["source"] == "owner"
