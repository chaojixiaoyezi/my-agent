"""技能根按事实共享，工作区绑定与用户策略保持独立。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.capability import skill_service as skills
from agent_py_agent.agent.common.cache_freshness import CACHE_TRUST_AGE_SECONDS


def _skill(root: Path, name: str, description: str = "核对资料") -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n# 核对\n1. 比较输入。\n", encoding="utf-8")
    return path


def _service(tmp_path: Path, owner: str = "alice", **kwargs):
    home = SimpleNamespace(owner_home_dir=tmp_path / owner,
                           shared_skills_dir=tmp_path / "shared",
                           shared_builtin_dir=tmp_path / "builtin")
    policy = SimpleNamespace(owner_id=owner, skills_enabled=True,
                             enabled_skill_sources=("workspace", "owner", "shared", "builtin"),
                             disabled_skills=(), enabled_shared_skills=())
    return skills.SkillsService(home_paths=home, workspace_root=tmp_path / owner / "workspace",
                                policy_provider=lambda: policy, **kwargs)


@pytest.fixture
def clock(monkeypatch):
    current = [100.0]
    monkeypatch.setattr(skills, "time", SimpleNamespace(monotonic=lambda: current[0]))
    return current


def test_one_snapshot_slot_for_one_hundred_workspaces(tmp_path, monkeypatch):
    _skill(tmp_path / "builtin", "builtin-method")
    _skill(tmp_path / "alice" / "skills", "owner-method")
    service = _service(tmp_path)
    build = Mock(wraps=skills._build_snapshot)
    monkeypatch.setattr(skills, "_build_snapshot", build)
    snapshots = {}
    for _ in range(3):
        for index in range(100):
            workspace = tmp_path / f"workspace-{index}"
            snapshot = service.snapshot_for(workspace)
            snapshots[workspace] = snapshot
            assert snapshot.workspace_root == str(workspace.resolve())
            assert {entry.name for entry in snapshot.entries} == {"builtin-method", "owner-method"}
    assert build.call_count == 1
    assert len(service._cache) == 1
    assert len(service._contents) == 1
    # 对照真实冷建，连同指纹、错误、包和冻结的资产引用一起逐字段核对。
    monkeypatch.setattr(skills, "_build_snapshot", build._mock_wraps)
    for workspace, snapshot in snapshots.items():
        fresh = _service(tmp_path).snapshot_for(workspace)
        assert {field.name: getattr(snapshot, field.name) for field in fields(snapshot)} == {
            field.name: getattr(fresh, field.name) for field in fields(fresh)}


def test_workspace_root_creation_and_new_skill_are_immediately_visible(tmp_path, clock):
    _skill(tmp_path / "builtin", "common")
    service = _service(tmp_path)
    left, right = tmp_path / "left", tmp_path / "right"
    common = service.snapshot_for(right)
    root = left / ".agent" / "skills"
    _skill(root, "local-one")
    first = service.snapshot_for(left)
    assert first.resolve("local-one") is not None
    assert service.snapshot_for(right) == common
    clock[0] += CACHE_TRUST_AGE_SECONDS / 4
    _skill(root, "local-two")
    second = service.snapshot_for(left)
    assert second.resolve("local-two") is not None
    assert service.snapshot_for(right).resolve("local-two") is None


def test_hot_root_validation_stats_each_root_once(tmp_path, monkeypatch, clock):
    for root in (tmp_path / "builtin", tmp_path / "shared", tmp_path / "alice" / "skills"):
        for index in range(12):
            _skill(root, f"method-{index}")
    service = _service(tmp_path)
    first = service.snapshot_for()
    roots = service._skill_roots(service.workspace_root, service.policy_provider())
    stat = Mock(wraps=skills._path_stat_signature)
    monkeypatch.setattr(skills, "_path_stat_signature", stat)
    clock[0] += CACHE_TRUST_AGE_SECONDS / 4
    assert service.snapshot_for() == first
    assert stat.call_count <= len(roots)
    assert {call.args[0] for call in stat.call_args_list} <= {root.path for root in roots}


def test_nested_change_is_visible_at_validation_deadline(tmp_path, clock):
    path = _skill(tmp_path / "alice" / "skills", "method", "旧说明")
    service = _service(tmp_path)
    first = service.snapshot_for()
    path.write_text(path.read_text(encoding="utf-8").replace("旧说明", "新说明"), encoding="utf-8")
    clock[0] += CACHE_TRUST_AGE_SECONDS / 2
    assert service.snapshot_for() == first
    clock[0] += CACHE_TRUST_AGE_SECONDS / 2
    assert service.snapshot_for().resolve("method").description == "新说明"


def test_process_root_cache_contains_only_builtin_for_eight_owners(tmp_path, clock):
    _skill(tmp_path / "builtin", "builtin-method")
    _skill(tmp_path / "shared", "shared-method")
    services = []
    with skills._PROCESS_SKILL_CACHE_LOCK:
        skills._PROCESS_BUILTIN_ROOT_CACHE.clear()
    for index in range(8):
        owner = f"owner-{index}"
        _skill(tmp_path / owner / "skills", f"private-{index}")
        service = _service(tmp_path, owner)
        services.append(service)
        snapshot = service.snapshot_for()
        assert snapshot.resolve(f"private-{index}") is not None
        assert all(snapshot.resolve(f"private-{other}") is None for other in range(8) if other != index)
        assert str(tmp_path / owner / "skills") in service._root_states
        assert str(tmp_path / "shared") in service._root_states
    with skills._PROCESS_SKILL_CACHE_LOCK:
        assert skills._PROCESS_BUILTIN_ROOT_CACHE
        assert all(Path(key[0]).is_relative_to(tmp_path / "builtin")
                   for key in skills._PROCESS_BUILTIN_ROOT_CACHE)
    assert len({id(service._root_states) for service in services}) == 8


def test_force_reload_and_clear_bypass_root_validation_window(tmp_path, clock):
    path = _skill(tmp_path / "builtin", "method", "第一版")
    service = _service(tmp_path)
    assert service.snapshot_for().resolve("method").description == "第一版"
    path.write_text(path.read_text(encoding="utf-8").replace("第一版", "第二版"), encoding="utf-8")
    assert service.snapshot_for(force_reload=True).resolve("method").description == "第二版"
    path.write_text(path.read_text(encoding="utf-8").replace("第二版", "第三版"), encoding="utf-8")
    service.clear_cache()
    assert service.snapshot_for().resolve("method").description == "第三版"


def test_capacity_provider_bounds_all_service_caches_and_can_grow(tmp_path, monkeypatch, clock):
    configured = [3]
    provider = SimpleNamespace(owner_agents=lambda floor: max(floor, configured[0]))
    monkeypatch.setattr(skills, "cache_capacity", provider)
    monkeypatch.setattr(skills, "_SERVICE_CACHE_MAX_ENTRIES", 2)
    service = _service(tmp_path)
    for index in range(8):
        workspace = tmp_path / f"workspace-{index}"
        _skill(workspace / ".agent" / "skills", f"method-{index}")
        assert service.snapshot_for(workspace).resolve(f"method-{index}") is not None
        assert len(service._cache) <= 3
        assert len(service._manifests) <= 3
        assert len(service._root_states) <= 3
    configured[0] = 6
    for index in range(8, 12):
        workspace = tmp_path / f"workspace-{index}"
        _skill(workspace / ".agent" / "skills", f"method-{index}")
        service.snapshot_for(workspace)
    assert service._capacity == len(service._cache) == len(service._manifests) == 6
    assert len(service._root_states) <= 6
    configured[0] = 3
    service.snapshot_for(workspace)
    assert len(service._cache) <= 3
    assert len(service._manifests) <= 3
    assert len(service._root_states) <= 3


def test_capacity_fallback_keeps_standalone_service_usable(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "cache_capacity", None)
    service = _service(tmp_path, cache_entries=2)
    assert service._capacity == 2
    assert service.snapshot_for().entries == ()


def test_guard_resource_change_rebinds_snapshot_after_deadline(tmp_path, clock):
    path = _skill(tmp_path / "shared", "method")
    helper = path.parent / "scripts" / "helper.sh"
    helper.parent.mkdir()
    helper.write_text("echo safe\n", encoding="utf-8")
    service = _service(tmp_path)
    first = service.snapshot_for()
    assert first.resolve("method") is not None
    helper.write_text("eval('x')\n", encoding="utf-8")
    clock[0] += CACHE_TRUST_AGE_SECONDS
    second = service.snapshot_for()
    assert second is not first
    assert second.resolve("method") is None
    assert any(error.code == "SKILL_GUARD_DENIED" for error in second.errors)


def test_explicit_extra_roots_keep_immediate_validation(tmp_path, clock):
    root = tmp_path / "mounted-skills"
    path = _skill(root, "method", "旧说明")
    service = _service(tmp_path)
    service.set_extra_roots([root])
    first = service.snapshot_for()
    path.write_text(path.read_text(encoding="utf-8").replace("旧说明", "新说明"), encoding="utf-8")
    assert service.snapshot_for().resolve("method").description == "新说明"
    assert first.resolve("method").description == "旧说明"


def test_explicit_guard_hook_keeps_immediate_validation(tmp_path, monkeypatch, clock):
    original = skills.evaluate_skill_guard_gate

    def guard(*args, **kwargs):
        return original(*args, **kwargs)

    monkeypatch.setattr(skills, "evaluate_skill_guard_gate", guard)
    path = _skill(tmp_path / "alice" / "skills", "method", "旧说明")
    service = _service(tmp_path)
    service.snapshot_for()
    path.write_text(path.read_text(encoding="utf-8").replace("旧说明", "新说明"), encoding="utf-8")
    assert service.snapshot_for().resolve("method").description == "新说明"


def test_concurrent_workspace_eviction_preserves_each_snapshot(tmp_path, clock):
    _skill(tmp_path / "builtin", "common")
    workspaces = [tmp_path / f"workspace-{index}" for index in range(8)]
    for index, workspace in enumerate(workspaces):
        _skill(workspace / ".agent" / "skills", f"local-{index}")
    service = _service(tmp_path, cache_entries=2)
    with ThreadPoolExecutor(max_workers=8) as pool:
        snapshots = list(pool.map(service.snapshot_for, workspaces))
    for index, snapshot in enumerate(snapshots):
        assert {entry.name for entry in snapshot.entries} == {"common", f"local-{index}"}
        assert snapshot.workspace_root == str(workspaces[index].resolve())
    assert len(service._cache) <= 2
    assert len(service._manifests) <= 2
    assert len(service._root_states) <= 2
    assert not service._builds


def test_legacy_plugin_provider_keeps_immediate_validation(tmp_path, clock):
    path = _skill(tmp_path / "alice" / "skills", "method", "旧说明")
    service = _service(tmp_path, plugin_roots=lambda: [(tmp_path / "plugin", "plugin:legacy")])
    service.snapshot_for()
    path.write_text(path.read_text(encoding="utf-8").replace("旧说明", "新说明"), encoding="utf-8")
    assert service.snapshot_for().resolve("method").description == "新说明"


def test_current_package_host_keeps_root_validation_window(tmp_path, clock):
    path = _skill(tmp_path / "alice" / "skills", "method", "旧说明")
    service = _service(tmp_path, plugin_roots=lambda: (), package_provider=lambda: ())
    first = service.snapshot_for()
    path.write_text(path.read_text(encoding="utf-8").replace("旧说明", "新说明"), encoding="utf-8")
    assert service.snapshot_for() is first
    clock[0] += CACHE_TRUST_AGE_SECONDS
    assert service.snapshot_for().resolve("method").description == "新说明"


def test_learning_generation_invalidates_old_tree_before_deadline(tmp_path, monkeypatch, clock):
    from agent_py_agent.agent.capability import skill_learning_versions

    generation = ["version-one"]
    monkeypatch.setattr(skill_learning_versions, "learning_state_fingerprint", lambda home: generation[0])
    path = _skill(tmp_path / "alice" / "skills", "method", "旧说明")
    service = _service(tmp_path)
    service.snapshot_for()
    path.write_text(path.read_text(encoding="utf-8").replace("旧说明", "新说明"), encoding="utf-8")
    generation[0] = "version-two"
    assert service.snapshot_for().resolve("method").description == "新说明"
