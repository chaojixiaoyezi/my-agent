# LLM: 三项整体审查的资产回归只用临时owner和真实存储；不执行技能、脚本或计划，不改部署账号。
# 模块用途: 验证管理员改名安装的技能身份/初始禁用和监控依赖闭包映射。
import copy
import json
from hashlib import sha256

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.skill_adapter import (
    extract_skill_descriptor,
    get_owner_skill_descriptor,
)
from agent_py_agent.agent.capability.skills import skill_enabled, split_skill_frontmatter
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.user_space import managed_user_governance as governance
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore


# LLM: 与正式本地账号初始化共用原服务，所有写入限pytest临时根。
# 函数用途: 建立管理员和一个受管用户的真实资产治理环境。
@pytest.fixture
def managed(tmp_path):
    root = tmp_path / "host"
    users = LocalUserStore(root)
    LocalUserLifecycle(users).create("alice", idempotency_key="holistic_alice_0001")
    return root, governance.ManagedUserGovernanceService(root), users.owner_home("alice")


# LLM: 合成技能不含秘密和可执行代码，源旧enabled刻意为True以捕捉初始禁用窗口。
# 函数用途: 写入一个可带历史侧元数据的技能及嵌套资源。
def seed_skill(home, name="demo", *, sidecar=True):
    directory = home / "skills" / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Test skill\n---\n# Test body\n")
    (directory / "resources").mkdir()
    (directory / "resources/note.txt").write_text("fixture resource")
    if sidecar:
        (directory / "skill_meta.json").write_text(json.dumps({"id": name, "name": name,
            "enabled": True, "metadata": {"original": "preserved"}}))
    return directory


# LLM: 观察真实atomic写前的侧文件字节，不替换SkillAdapter或治理写入，避免只检查最终值漏掉首次错误发布。
# 函数用途: 回归冲突改名、缺侧文件兼容、初始启停与旧技能不变。
@pytest.mark.parametrize("sidecar", [True, False])
@pytest.mark.parametrize("enabled", [False, True])
def test_holistic_renamed_skill_identity_and_initial_state(managed, monkeypatch, sidecar, enabled):
    root, service, home = managed
    source = seed_skill(root / "owners/local/main", sidecar=sidecar)
    original = seed_skill(home.home_dir)
    before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    writes, real_write = [], governance.write_bytes_atomic_beneath

    # LLM: 只记录Skill根下的首次写入，仍调用原nofollow原子写。
    # 函数用途: 抓取安装过程公开的初始侧元数据。
    def observe(base, parts, data):
        if "skills" in parts and parts[-1] == "skill_meta.json":
            writes.append(json.loads(data))
        return real_write(base, parts, data)

    monkeypatch.setattr(governance, "write_bytes_atomic_beneath", observe)
    result = service.provision_assets("alice", "skill", "demo", auto_enable=enabled, conflict_strategy="keep_both_rename")
    renamed = result["asset_id"]
    directory = home.home_dir / "skills" / renamed
    assert renamed.startswith("demo-admin-") and renamed != "demo"
    assert writes and (writes[0]["id"], writes[0]["name"], writes[0]["enabled"]) == (renamed, renamed, enabled)
    meta = json.loads((directory / "skill_meta.json").read_text())
    assert (meta["id"], meta["name"], meta["enabled"], meta["metadata"]["enabled"]) == (renamed, renamed, enabled, enabled)
    assert meta["metadata"]["delivery_provenance"]["provisioned_by_admin"] is True
    assert not sidecar or meta["metadata"]["original"] == "preserved"
    assert split_skill_frontmatter((directory / "SKILL.md").read_text())[0]["name"] == renamed
    assert extract_skill_descriptor(directory)["id"] == renamed and skill_enabled(directory / "SKILL.md") is enabled
    assert (directory / "resources/note.txt").read_text() == "fixture resource"
    assert skill_enabled(original / "SKILL.md") is True
    assert {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()} == before


# LLM: 合法成员故意排在坏路径前，必须在任意成员写盘前整体验证；失败不能留下可发现的半个技能。
# 函数用途: 回归全部资源路径先验校验而不是边写边检查。
@pytest.mark.parametrize("bad", ["../escape.txt", "", "/absolute.txt", "dir/../../escape.txt"])
def test_holistic_skill_paths_validate_before_any_write(managed, bad):
    _root, service, home = managed
    files = {"SKILL.md": "---\nname: unsafe\ndescription: Test\n---\nBody", "safe.txt": b"fixture", bad: b"invalid"}
    with pytest.raises(ValueError):
        service.provision_assets("alice", "skill", "unsafe", payload_override={"files": files})
    assert not (home.home_dir / "skills/unsafe").exists()
    assert not (home.home_dir / "data/provisioned_assets/skill-unsafe.json").exists()


# LLM: 损坏侧文件不能先发布正文再靠启停失败退出，元数据校验必须在首次写入前完成。
# 函数用途: 覆盖非法JSON、非对象侧文件/metadata及实际适配器拒绝的语义字段。
@pytest.mark.parametrize("meta", ["broken", "[]", '{"metadata":[]}', '{"refs":[]}', '{"description":[]}'])
def test_holistic_invalid_skill_metadata_leaves_no_partial_install(managed, meta):
    _root, service, home = managed
    with pytest.raises(ValueError):
        service.provision_assets("alice", "skill", "unsafe", payload_override={"files": {
            "SKILL.md": "---\nname: unsafe\ndescription: Test\n---\nBody", "skill_meta.json": meta}})
    assert not (home.home_dir / "skills/unsafe").exists()


# LLM: 监控定义仅作为安装输入，规则使用真实合法脚本引用，不触发任何调度运行。
# 函数用途: 构造含owner/裸名/builtin技能及脚本规则的源监控。
def monitor_payload():
    schedule = {"kind": "every", "every_seconds": 300, "anchor_at": 0.0}
    digest = sha256(b"---\nname: demo\ndescription: Test skill\n---\n# Test body\n").hexdigest()
    return {"name": "Closure monitor", "prompt": "Inspect local data", "schedule": schedule,
        "skill_refs": [{"stable_id": ref, "content_sha256": digest} for ref in ("owner:demo", "demo", "builtin:example", "owner:demo")],
        "rules": [{"id": "check", "name": "Check", "enabled": True, "use_llm": False, "schedule": schedule,
            "tool": {"name": "scripts", "arguments": {"action": "run", "script_id": "checker"}}}]}


# LLM: 两个依赖在目标都冲突；必须闭包安装、改名并重绑，builtin不得复制，caller负载与旧目标零修改。
# 函数用途: 验证监控技能refs和规则script_id沿同一映射，依赖与监控启停一致。
@pytest.mark.parametrize("enabled", [False, True])
def test_holistic_monitor_dependency_closure_and_rename_mapping(managed, enabled):
    root, service, home = managed
    source = root / "owners/local/main"
    seed_skill(source)
    seed_skill(home.home_dir)
    ScriptStore(source).save_script(AssetDescriptor("checker", "script", "Source"), 'print("{}")')
    ScriptStore(home.home_dir).save_script(AssetDescriptor("checker", "script", "Original"), 'print("old")')
    payload = monitor_payload()
    original = copy.deepcopy(payload)
    result = service.provision_assets("alice", "monitor", "job_closure", auto_enable=enabled,
        conflict_strategy="keep_both_rename", payload_override=payload)
    installed = {item["asset_kind"]: item["asset_id"] for item in result["installed"]}
    assert set(installed) == {"skill", "script", "monitor"}
    assert installed["skill"].startswith("demo-admin-") and installed["script"].startswith("checker-admin-")
    job = governance._managed_scheduler(home).get_job(result["asset_id"])
    assert {ref["stable_id"] for ref in job["skill_refs"]} == {"owner:" + installed["skill"], installed["skill"], "builtin:example"}
    digest = sha256((home.home_dir / "skills" / installed["skill"] / "SKILL.md").read_bytes()).hexdigest()
    assert all(ref["content_sha256"] == digest for ref in job["skill_refs"] if not ref["stable_id"].startswith("builtin:"))
    assert next(ref for ref in job["skill_refs"] if ref["stable_id"].startswith("builtin:")) == original["skill_refs"][2]
    assert job["rules"][0]["tool"]["arguments"]["script_id"] == installed["script"]
    assert job["enabled"] is enabled and job["status"] == ("active" if enabled else "paused")
    assert ScriptStore(home.home_dir).get_script(installed["script"])["enabled"] is enabled
    assert skill_enabled(home.home_dir / "skills" / installed["skill"] / "SKILL.md") is enabled
    assert ScriptStore(home.home_dir).get_script("checker")["code"] == 'print("old")'
    assert payload == original


# LLM: 缺技能/脚本必须在收集阶段准确拒绝；不写线程、调度账或可执行依赖。
# 函数用途: 验证监控闭包先校验齐全，再进行目标安装。
@pytest.mark.parametrize("missing", ["skill", "script"])
def test_holistic_monitor_missing_dependency_rejects_before_install(managed, missing):
    root, service, home = managed
    seed_skill(root / "owners/local/main")
    ScriptStore(root / "owners/local/main").save_script(AssetDescriptor("checker", "script", "Source"), 'print("{}")')
    payload = monitor_payload()
    if missing == "skill":
        payload["skill_refs"] = [{"stable_id": "owner:missing", "content_sha256": "a" * 64}]
    else:
        payload["rules"][0]["tool"]["arguments"]["script_id"] = "missing"
    before = {str(p.relative_to(home.home_dir)): p.read_bytes() for p in home.home_dir.rglob("*") if p.is_file()}
    with pytest.raises(HostAPIError, match="ASSET_NOT_FOUND"):
        service.provision_assets("alice", "monitor", "job_missing", payload_override=payload)
    assert {str(p.relative_to(home.home_dir)): p.read_bytes() for p in home.home_dir.rglob("*") if p.is_file()} == before


# LLM: 非owner来源由原发现适配器确认；同名私人技能不应遮蔽稳定来源，也不能被当成监控依赖复制。
# 函数用途: 回归shared/workspace/plugin引用的来源和版本保持，含同名owner遮蔽变体。
@pytest.mark.parametrize("prefix", ["shared", "workspace", "plugin:fixture"])
@pytest.mark.parametrize("shadow", [False, True])
def test_holistic_monitor_preserves_nonowner_skill_identity(managed, prefix, shadow):
    root, service, home = managed
    source = root / "owners/local/main"
    external = root / "fixture-public"
    directory = seed_skill(external)
    (directory / "SKILL.md").write_text((directory / "SKILL.md").read_text() + "Public source.\n")
    if shadow:
        seed_skill(source)
        seed_skill(home.home_dir)
    descriptor = get_owner_skill_descriptor(source, prefix + ":demo", extra_roots=((external / "skills", prefix),))
    reference = {"stable_id": descriptor["metadata"]["stable_id"], "content_sha256": descriptor["metadata"]["content_hash"]}
    payload = monitor_payload()
    payload.pop("rules")
    payload["skill_refs"] = [reference]
    before = {str(p.relative_to(home.home_dir)): p.read_bytes() for p in (home.home_dir / "skills").rglob("*") if p.is_file()}
    result = service.provision_assets("alice", "monitor", "job_public", conflict_strategy="keep_both_rename", payload_override=payload)
    assert [item["asset_kind"] for item in result["installed"]] == ["monitor"]
    assert governance._managed_scheduler(home).get_job(result["asset_id"])["skill_refs"] == [reference]
    assert {str(p.relative_to(home.home_dir)): p.read_bytes() for p in (home.home_dir / "skills").rglob("*") if p.is_file()} == before


# LLM: 运行时ConversationStore位于workspace/runtime/workspaces/<scope>/conversations；治理派单、列表和审核须双向互通。
# 函数用途: 验证运行时线程可直接提交审核、接收派单通知，且新建派单在运行时库已存在时同步镜像。
def test_holistic_managed_conversations_syncs_with_runtime_workspace(managed):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.user_space.governance_review_store import GovernanceReviewStore
    from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent

    root, service, home = managed
    scoped = SimpleNamespace(owner_home_dir=home.home_dir, owner_workspace_dir=home.workspace_dir)
    rt_path = runtime_paths_for_agent(SimpleNamespace(), home.home_dir, scoped)["conversation_workspace"]
    rt_store = ConversationStore(rt_path, initialize=True)
    rt_thread = rt_store.threads.create(owner_id=home.owner_id, owner_home=str(home.home_dir),
                                        workspace_root=str(home.workspace_dir), cwd=str(home.workspace_dir))
    dispatched = service.dispatch_task("alice", "Inspect runtime thread", thread_id=rt_thread.thread_id)
    assert dispatched["thread_id"] == rt_thread.thread_id
    assert any(row["task_id"] == dispatched["task_id"] for row in service.list_tasks("alice")["tasks"])
    assert any(wake.root_task_id == dispatched["task_id"] for wake in rt_store.wakes.pending())
    reviews = GovernanceReviewStore(root)
    item = reviews.submit_review("alice", "high_risk_command", {"command": "ls"}, applicant_thread_id=rt_thread.thread_id)
    decided = reviews.decide_review(item.review_id, "reject_with_feedback", "Use read_file", expected_revision=1, target_owner_id="alice")
    assert decided["status"] == "rejected" and decided["notification_status"] == "queued"
    assert rt_store.messages.recent(rt_thread.thread_id)[-1].metadata["governance_review"]["review_id"] == item.review_id
