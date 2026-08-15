from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

# Memory/Skill 独立 effective flag 四象限负向测试（C 项拍板方案）：
# 双开默认 / 只关 Memory / 只关 Skill / 双关 —— 断言 EffectiveOwnerPolicy 字段、
# skill 快照空/非空、memory 消费点短路行为、子代理 and 继承。


def _write_policy_json(path: Path, *, enabled: bool) -> None:
    path.write_text(
        json.dumps({"schema_version": "memory-policy.v1", "enabled": enabled}, sort_keys=True),
        encoding="utf-8",
    )


def _effective_home(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    return ensure_my_agent_home(tmp_path)


def _resolve(home):
    from agent_py_agent.agent.user_space.owner_policy import resolve_effective_owner_policy

    return resolve_effective_owner_policy(home)


def _parent_policy(**overrides) -> SimpleNamespace:
    defaults = {
        "filesystem_access_mode": "workspace-write",
        "shell_access_mode": "workspace-write",
        "network_enabled": True,
        "max_active_agents": 8,
        "max_subagents": 4,
        "max_depth": 3,
        "max_disk_mb": 1024,
        "disabled_tools": (),
        "enabled_shared_skills": (),
        "memory_enabled": True,
        "skills_enabled": True,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _write_owner_skill(root: Path, name: str, description: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n按步骤执行。\n",
        encoding="utf-8",
    )
    return path


def test_quadrant_both_enabled_by_default(tmp_path: Path) -> None:
    home = _effective_home(tmp_path)

    assert home.owner_memory_policy_json.is_file()
    assert home.owner_skill_policy_json.is_file()
    assert json.loads(home.owner_memory_policy_json.read_text(encoding="utf-8"))["enabled"] is True
    assert json.loads(home.owner_skill_policy_json.read_text(encoding="utf-8"))["enabled"] is True

    policy = _resolve(home)
    assert policy.memory_enabled is True
    assert policy.skills_enabled is True
    assert policy.to_dict()["memory"] == {"enabled": True}
    assert policy.to_dict()["skills"]["enabled"] is True


def test_quadrant_memory_off_skill_on(tmp_path: Path) -> None:
    home = _effective_home(tmp_path)
    _write_policy_json(home.owner_memory_policy_json, enabled=False)

    policy = _resolve(home)
    assert policy.memory_enabled is False
    assert policy.skills_enabled is True


def test_quadrant_skill_off_memory_on(tmp_path: Path) -> None:
    home = _effective_home(tmp_path)
    _write_policy_json(home.owner_skill_policy_json, enabled=False)

    policy = _resolve(home)
    assert policy.skills_enabled is False
    assert policy.memory_enabled is True


def test_quadrant_both_off(tmp_path: Path) -> None:
    home = _effective_home(tmp_path)
    _write_policy_json(home.owner_memory_policy_json, enabled=False)
    _write_policy_json(home.owner_skill_policy_json, enabled=False)

    policy = _resolve(home)
    assert policy.memory_enabled is False
    assert policy.skills_enabled is False


def test_missing_policy_file_counts_as_enabled(tmp_path: Path) -> None:
    home = _effective_home(tmp_path)
    home.owner_memory_policy_json.unlink()
    home.owner_skill_policy_json.unlink()

    policy = _resolve(home)
    assert policy.memory_enabled is True  # 老 owner 兼容:缺失视为开启
    assert policy.skills_enabled is True


def test_child_and_inherits_parent_switches(tmp_path: Path) -> None:
    home = _effective_home(tmp_path)
    from agent_py_agent.agent.user_space.owner_policy import resolve_effective_owner_policy

    # 父闸关 → 子文件开着也继承为关
    closed_parent = resolve_effective_owner_policy(
        home, parent_policy=_parent_policy(memory_enabled=False, skills_enabled=False)
    )
    assert closed_parent.memory_enabled is False
    assert closed_parent.skills_enabled is False

    # 父 Memory 关、Skill 开 → 子分别继承
    memory_only_off = resolve_effective_owner_policy(
        home, parent_policy=_parent_policy(memory_enabled=False, skills_enabled=True)
    )
    assert memory_only_off.memory_enabled is False
    assert memory_only_off.skills_enabled is True

    # 父全开、子文件也开 → 全开
    all_open = resolve_effective_owner_policy(home, parent_policy=_parent_policy())
    assert all_open.memory_enabled is True
    assert all_open.skills_enabled is True


def test_skill_snapshot_empty_when_skills_disabled(tmp_path, skill_catalog_factory) -> None:
    root = tmp_path / "home"
    catalog = skill_catalog_factory(root)
    _write_owner_skill(Path(catalog.home.owner_home_dir) / "skills", "alpha", "owner skill")

    assert catalog.service.snapshot_for(catalog.workspace, force_reload=True).entries

    catalog.policy.skills_enabled = False
    snapshot = catalog.service.snapshot_for(catalog.workspace, force_reload=True)
    assert snapshot.entries == ()
    assert snapshot.errors == ()  # 空快照不带任何 load 错误(短路而非扫描失败)


def test_wake_discovery_memory_flag_short_circuit(tmp_path: Path) -> None:
    from agent_py_agent.agent.owner_wake_discovery import _owner_memory_enabled

    home = _effective_home(tmp_path)
    assert _owner_memory_enabled(home.owner_home_dir) is True  # 默认开启

    _write_policy_json(home.owner_memory_policy_json, enabled=False)
    assert _owner_memory_enabled(home.owner_home_dir) is False

    home.owner_memory_policy_json.unlink()
    assert _owner_memory_enabled(home.owner_home_dir) is True  # 缺失视为开启

    home.owner_memory_policy_json.write_text("{bad-json}\n", encoding="utf-8")
    assert _owner_memory_enabled(home.owner_home_dir) is True  # 坏文件不误杀


def test_has_pending_curator_work_short_circuits_when_disabled(tmp_path: Path) -> None:
    from agent_py_agent.agent.owner_wake_discovery import _has_pending_memory_curator_work

    home = _effective_home(tmp_path)
    (home.owner_memory_curator_dir / "state.json").write_text(
        json.dumps({"pending_reasons": ["session_close"]}), encoding="utf-8"
    )
    assert _has_pending_memory_curator_work(home.owner_home_dir) is True  # 有 pending reason

    _write_policy_json(home.owner_memory_policy_json, enabled=False)
    assert _has_pending_memory_curator_work(home.owner_home_dir) is False  # 总闸关闭即短路


def test_memory_recall_short_circuits_when_disabled(tmp_path: Path) -> None:
    from agent_py_agent.agent.memory_push import push_relevant_memories_report

    disabled_agent = SimpleNamespace(owner_policy=SimpleNamespace(memory_enabled=False))
    memories, errors = push_relevant_memories_report(
        disabled_agent, "failure", {"task_id": "t-1", "goal": "g"}
    )
    assert memories == []
    assert errors == []

    # 拿不到 policy 的桩视为开启(与缺失文件语义一致),走真实查询:
    # 桩缺记忆路由 authority → fail-closed 报错(与短路返回的干净空元组可区分)
    bare_agent = SimpleNamespace()
    memories, errors = push_relevant_memories_report(
        bare_agent, "failure", {"task_id": "t-1", "goal": "g"}
    )
    assert memories == []
    assert errors  # 未短路:真实查询失败以结构化错误暴露,而非被总闸吞掉


def test_remember_tool_unavailable_when_memory_disabled() -> None:
    from agent_py_agent.agent.capability.memory_tool import RememberTool

    disabled = RememberTool(SimpleNamespace(owner_policy=SimpleNamespace(memory_enabled=False)))
    assert disabled.availability().available is False

    enabled = RememberTool(SimpleNamespace(owner_policy=SimpleNamespace(memory_enabled=True)))
    assert enabled.availability().available is True


def test_curator_scheduling_flag_short_circuit() -> None:
    from agent_py_agent.cli.gateway_loops import _agent_memory_enabled

    assert _agent_memory_enabled(SimpleNamespace()) is True  # 无 policy 视为开启
    assert _agent_memory_enabled(SimpleNamespace(owner_policy=None)) is True
    assert (
        _agent_memory_enabled(
            SimpleNamespace(owner_policy=SimpleNamespace(memory_enabled=False))
        )
        is False
    )


def test_session_curator_request_short_circuits_when_disabled() -> None:
    from agent_py_agent.agent.memory_store.lifecycle import (
        request_memory_curator_for_session,
    )

    disabled_agent = SimpleNamespace(owner_policy=SimpleNamespace(memory_enabled=False))
    result = request_memory_curator_for_session(disabled_agent, event="close")
    assert result.requested is False
    assert result.pending_reasons == ("memory_policy_disabled",)

    enabled_agent = SimpleNamespace(
        owner_policy=SimpleNamespace(memory_enabled=True),
        memory_curator=SimpleNamespace(
            request=lambda reason: {"requested": True, "pending_reasons": [reason], "requested_at": "t"}
        ),
    )
    result = request_memory_curator_for_session(enabled_agent, event="close")
    assert result.requested is True  # 总闸放行:请求登记正常走原逻辑
    assert result.pending_reasons == ("session_close",)
