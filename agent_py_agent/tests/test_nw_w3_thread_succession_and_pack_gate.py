"""Next-Wave W3 (#10, #11, #18) 回归单测：会话继任链上限与元数据保留、能力包晋升双门禁。"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.asset_promotion import (
    bind_promotion_host,
    promote_verified_asset,
)
from agent_py_agent.agent.capability.package_provider import _owner_from_home
from agent_py_agent.agent.capability.skill_learning_publish import SkillLearningGateError
from agent_py_agent.agent.conversation.models import ChannelBinding, ConversationThread
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.store_threads import (
    resolve_thread_successor_chain,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.scheduler.execution import delivery_thread
from agent_py_agent.agent.scheduler.notification_binding import SCHEMA, bound_notification_thread
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner


def _write_capability_config(root: Path, *, capability_pack_enabled: bool) -> Path:
    cfg_dir = root / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "capability_config.yaml"
    val = "true" if capability_pack_enabled else "false"
    cfg_path.write_text(f"capability_pack_self_install_enabled: {val}\n", encoding="utf-8")
    return cfg_path


def _package_candidate(name: str = "nw-w3-pack") -> dict[str, object]:
    return {
        "kind": "capability_package",
        "name": name,
        "description": "确定性能力包晋升门禁测试包",
        "body": "# NW W3 Pack\n确定性能力正文说明。\n",
        "entry_document": "CAPABILITY.md",
        "files": {"README.md": "runtime package notes"},
        "steps": ["step-1"],
        "sample_inputs": [{"input": "sample"}],
    }


def test_w3_10_thread_successor_chain_supports_25_hops_and_keeps_bounds(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conv")
    owner_home = str(tmp_path / "owner_home")
    root = current = store.threads.create(
        canonical_user_id="u_w3_10",
        owner_id="local:main",
        owner_home=owner_home,
    )
    for idx in range(25):
        current = store.threads.succeed(current.thread_id, title=f"hop-{idx + 1}")

    assert resolve_thread_successor_chain(store, root.thread_id) == current.thread_id
    assert resolve_thread_successor_chain(store, root.thread_id, max_hops=4097) == current.thread_id
    assert resolve_thread_successor_chain(store, current.thread_id, max_hops=0) == current.thread_id

    for bad_max in (5, 0, -1, True, False):
        with pytest.raises(ValueError, match="THREAD_SUCCESSOR_HOP_LIMIT"):
            resolve_thread_successor_chain(store, root.thread_id, max_hops=bad_max)


def test_w3_10_thread_successor_chain_traverses_beyond_4100_hops_for_scheduler_and_notification_binding(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    agent = SimpleAgent(
        AgentConfig(
            my_agent_home=str(home),
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        workspace,
    )
    store = agent.conversation_store
    owner_id = agent.home_paths.owner_id
    owner_home = str(agent.home_paths.owner_home_dir)
    scope = ["feishu", "ou_w3_long", "oc_w3_long"]
    scope_key = "\x1f".join(scope)
    identity = bind_admin_channel_identity(home, "feishu", "ou_w3_long")

    root = store.threads.get_or_create(
        {
            "canonical_user_id": "u_w3_long",
            "channel": "feishu",
            "channel_user_id": "ou_w3_long",
            "channel_conversation_id": "oc_w3_long",
            "owner_id": owner_id,
            "owner_home": owner_home,
            "cwd": str(workspace),
            "now": 900,
        }
    )
    hop_ids = [root.thread_id] + [f"thread_w3_hop_{idx:04d}" for idx in range(1, 4105)]
    for idx in range(1, 4105):
        tid = hop_ids[idx]
        nxt = hop_ids[idx + 1] if idx + 1 < len(hop_ids) else None
        meta = {
            "predecessor_thread_id": hop_ids[idx - 1],
            "predecessor_binding": list(scope),
            "succession_binding": list(scope),
            "succession_published": True,
        }
        if nxt is not None:
            meta["successor_thread_id"] = nxt
            meta["succeeded_at"] = 1000.0 + idx
            meta["succession_by_binding"] = {scope_key: nxt}
        row = ConversationThread(
            thread_id=tid,
            canonical_user_id="u_w3_long",
            owner_id=owner_id,
            owner_home=owner_home,
            title=f"hop-{idx}",
            channel_bindings=(
                ChannelBinding(
                    channel="feishu",
                    channel_user_id="ou_w3_long",
                    channel_conversation_id="oc_w3_long",
                    canonical_user_id="u_w3_long",
                    thread_id=tid,
                    last_active_at=1000.0 + idx,
                ),
            ),
            status="archived" if nxt is not None else "active",
            metadata=meta,
            created_at=1000.0 + idx,
            updated_at=1000.0 + idx,
        )
        store.storage.thread_path(tid).write_text(json.dumps(row.to_dict()), encoding="utf-8")

    store.threads.update_atomic(
        root.thread_id,
        lambda item: replace(
            item,
            status="archived",
            metadata={
                **item.metadata,
                "successor_thread_id": hop_ids[1],
                "succeeded_at": 1000.5,
                "succession_binding": list(scope),
                "succession_by_binding": {scope_key: hop_ids[1]},
            },
        ),
    )
    tail = store.threads.succeed(hop_ids[-1], title="hop-4105-real")

    assert resolve_thread_successor_chain(store, root.thread_id, binding=scope) == tail.thread_id
    assert (
        store.threads.resolve(
            channel="feishu",
            channel_conversation_id="oc_w3_long",
            channel_user_id="ou_w3_long",
        ).thread_id
        == tail.thread_id
    )

    with pytest.raises(ValueError, match="THREAD_SUCCESSOR_HOP_LIMIT"):
        resolve_thread_successor_chain(store, root.thread_id, max_hops=4096, binding=scope)

    job, _ = agent.scheduler_repository.create_job(
        SchedulerJobCreateRequest(
            name="长继任链绑定任务",
            prompt="check",
            thread_id=root.thread_id,
            schedule={"kind": "every", "every_seconds": 600, "anchor_at": 1000, "timezone": "UTC"},
            now=900,
        )
    )
    notif_binding = {
        "schema_version": SCHEMA,
        "version": 1,
        "owner": agent.scheduler_repository.owner,
        "source_thread_id": root.thread_id,
        "binding": {"channel": "feishu", "actor_id": "ou_w3_long", "conversation_id": "oc_w3_long"},
        "admin_bound_at": identity.bound_at,
        "request_id": "w3-long-chain-notif",
    }
    agent.scheduler_repository.update_notification_binding(
        job["job_id"], notif_binding, expected_version=job["version"]
    )
    assert bound_notification_thread(agent, notif_binding).thread_id == tail.thread_id

    run = {
        "run_id": "srun_w3_long",
        "thread_id": "thread-exec-long",
        "owner": agent.scheduler_repository.owner,
        "delivery": {"mode": "origin", "follow_latest_channel_session": True},
        "execution_context": {
            "origin_thread_id": root.thread_id,
            "origin_bindings": [
                {
                    "channel": "feishu",
                    "channel_user_id": "ou_w3_long",
                    "channel_conversation_id": "oc_w3_long",
                    "canonical_user_id": "u_w3_long",
                    "thread_id": root.thread_id,
                    "last_active_at": 900.0,
                }
            ],
        },
    }
    agent_view = SimpleNamespace(
        conversation_store=store,
        scheduler_repository=SimpleNamespace(
            get_active_run=lambda _tid: run,
            event_run_is_current=lambda _tid: True,
        ),
    )
    assert delivery_thread(agent_view, root, SimpleNamespace(task_id="srun_w3_long", thread_id="thread-exec-long")).thread_id == tail.thread_id

    # 即使在长链末端注入环路，visited 集合仍必须稳定拦截 THREAD_SUCCESSOR_CYCLE。
    store.threads.update_atomic(
        tail.thread_id,
        lambda item: replace(
            item,
            metadata={
                **item.metadata,
                "successor_thread_id": hop_ids[10],
                "succession_by_binding": {scope_key: hop_ids[10]},
            },
        ),
    )
    with pytest.raises(ValueError, match="THREAD_SUCCESSOR_CYCLE"):
        resolve_thread_successor_chain(store, root.thread_id, binding=scope)


def test_w3_10_thread_successor_chain_detects_cycle(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conv_cycle")
    owner_home = str(tmp_path / "owner_home")
    t0 = store.threads.create(
        canonical_user_id="u_w3_cycle",
        owner_id="local:main",
        owner_home=owner_home,
    )
    t1 = store.threads.succeed(t0.thread_id)
    t2 = store.threads.succeed(t1.thread_id)

    def _inject_cycle(thread_obj):
        meta = dict(thread_obj.metadata)
        scope = ["", "", ""]
        by_binding = dict(meta.get("succession_by_binding") or {})
        by_binding["\x1f".join(scope)] = t1.thread_id
        meta["successor_thread_id"] = t1.thread_id
        meta["succession_binding"] = list(scope)
        meta["succession_by_binding"] = by_binding
        return replace(thread_obj, metadata=meta)

    store.threads.update_atomic(t2.thread_id, _inject_cycle)

    with pytest.raises(ValueError, match="THREAD_SUCCESSOR_CYCLE"):
        resolve_thread_successor_chain(store, t0.thread_id)


def test_w3_18_new_succeed_preserves_route_mode_and_session_space_without_aliasing(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conv_meta")
    owner_home = str(tmp_path / "owner_home")
    t0 = store.threads.create(
        canonical_user_id="u_w3_18",
        owner_id="local:main",
        owner_home=owner_home,
    )
    route_mode_val = {
        "owner_id": "local:main",
        "actors": {"ou_1": {"mode": "main", "updated_at": "2026-11-14T00:00:00Z"}},
    }
    session_space_val = {
        "owner_id": "local:main",
        "actors": {"ou_1": {"space_id": "space_alpha", "updated_at": "2026-11-14T00:00:00Z"}},
    }

    def _seed_meta(thread_obj):
        meta = dict(thread_obj.metadata)
        meta.update(
            {
                "provider": "anthropic",
                "model": "claude-test",
                "permission_mode": "default",
                "packs": ["pack_a"],
                "verbose_level_source": "user",
                "route_mode.v1": route_mode_val,
                "session_space.v1": session_space_val,
                "untrusted_runtime_authority": {"role": "elevated"},
            }
        )
        return replace(thread_obj, metadata=meta)

    store.threads.update_atomic(t0.thread_id, _seed_meta)
    t1 = store.threads.succeed(t0.thread_id)

    assert t1.metadata["route_mode.v1"] == route_mode_val
    assert t1.metadata["session_space.v1"] == session_space_val
    assert t1.metadata["provider"] == "anthropic"
    assert t1.metadata["packs"] == ["pack_a"]
    assert "untrusted_runtime_authority" not in t1.metadata

    # deepcopy 隔离：原地修改新线程嵌套元数据不得污染已归档旧线程。
    t1.metadata["route_mode.v1"]["actors"]["ou_1"]["mode"] = "isolated"
    t1.metadata["session_space.v1"]["actors"]["ou_1"]["space_id"] = "space_beta"
    archived = store.threads.require(t0.thread_id)
    assert archived.metadata["route_mode.v1"]["actors"]["ou_1"]["mode"] == "main"
    assert archived.metadata["session_space.v1"]["actors"]["ou_1"]["space_id"] == "space_alpha"


def test_w3_11_capability_package_gate_enforces_switch_and_local_admin(tmp_path: Path) -> None:
    root = tmp_path / "agent_root"
    home_paths = ensure_my_agent_home(root)
    admin_home = home_paths.owner_home_dir
    cand = _package_candidate()

    # 1) 默认关闭（显式 false）：拒绝且零写入
    _write_capability_config(root, capability_pack_enabled=False)
    with pytest.raises(SkillLearningGateError) as disabled_exc:
        promote_verified_asset(admin_home, cand)
    assert disabled_exc.value.code == "CAPABILITY_PACK_SELF_INSTALL_DISABLED"
    assert disabled_exc.value.detail["error_code"] == "CAPABILITY_PACK_SELF_INSTALL_DISABLED"
    assert disabled_exc.value.detail["effect_outcome"] == "not_started"
    assert (
        "/settings set capability_pack_self_install_enabled true"
        in str(disabled_exc.value.detail["next_action"])
    )
    assert PluginInstallStore(_owner_from_home(admin_home)).snapshot() == ()

    # 2) 开关开启但为托管 IM 非管理员 owner 目录：拒绝且零写入
    _write_capability_config(root, capability_pack_enabled=True)
    im_home = root / "owners" / "providers" / "feishu" / "users" / "ou_non_admin"
    im_home.mkdir(parents=True, exist_ok=True)
    with pytest.raises(SkillLearningGateError) as im_exc:
        promote_verified_asset(im_home, cand)
    assert im_exc.value.code == "CAPABILITY_PACK_ADMIN_REQUIRED"
    assert im_exc.value.detail["error_code"] == "CAPABILITY_PACK_ADMIN_REQUIRED"
    assert im_exc.value.detail["effect_outcome"] == "not_started"
    assert str(im_exc.value.detail["next_action"]).strip()
    assert PluginInstallStore(_owner_from_home(im_home)).snapshot() == ()

    # 3) 开关开启但显式传入非管理员宿主上下文：拒绝且零写入
    non_admin_host = SimpleNamespace(
        root=root,
        owner_provider="feishu",
        owner_kind="user",
        owner_id="ou_non_admin",
    )
    with pytest.raises(SkillLearningGateError) as host_exc:
        promote_verified_asset(admin_home, cand, host=non_admin_host)
    assert host_exc.value.code == "CAPABILITY_PACK_ADMIN_REQUIRED"
    assert host_exc.value.detail["effect_outcome"] == "not_started"
    assert PluginInstallStore(_owner_from_home(admin_home)).snapshot() == ()

    # 3b) 开关开启但宿主 home_paths 缺少完整管理员身份字段（未 scoped）：同样拒绝且零写入（fail closed）
    incomplete_host = SimpleNamespace(root=root, home_paths=home_paths)
    with pytest.raises(SkillLearningGateError) as incomplete_exc:
        promote_verified_asset(admin_home, cand, host=incomplete_host)
    assert incomplete_exc.value.code == "CAPABILITY_PACK_ADMIN_REQUIRED"
    assert incomplete_exc.value.detail["effect_outcome"] == "not_started"
    assert PluginInstallStore(_owner_from_home(admin_home)).snapshot() == ()

    # 4) 开关开启 + 完整本机主管理员（local/main）：允许晋升并真实安装启用
    admin_paths = home_paths_with_owner(home_paths, _owner_from_home(admin_home))
    admin_host = SimpleNamespace(root=root, home_paths=admin_paths)
    promoted = promote_verified_asset(admin_home, cand, host=admin_host)
    assert isinstance(promoted, dict)
    assert promoted["carrier"] == "capability_package"
    assert promoted["asset_id"] == "nw-w3-pack"
    snap = PluginInstallStore(_owner_from_home(admin_home)).snapshot()
    assert len(snap) == 1
    assert snap[0].manifest.plugin_id == "nw-w3-pack"
    assert snap[0].enabled is True

    # 4b) 开关开启 + 不显式传 host 时，按 admin_home 自身根目录配置与 local/main 身份同样可通过
    root_no_host = tmp_path / "agent_root_no_host"
    admin_home_no_host = ensure_my_agent_home(root_no_host).owner_home_dir
    _write_capability_config(root_no_host, capability_pack_enabled=True)
    promoted_no_host = promote_verified_asset(
        admin_home_no_host,
        _package_candidate("nw-w3-pack-default"),
    )
    assert isinstance(promoted_no_host, dict)
    assert promoted_no_host["asset_id"] == "nw-w3-pack-default"
    snap_no_host = PluginInstallStore(_owner_from_home(admin_home_no_host)).snapshot()
    assert len(snap_no_host) == 1
    assert snap_no_host[0].manifest.plugin_id == "nw-w3-pack-default"
    assert snap_no_host[0].enabled is True

    # 5) 开关重新关闭后再次调用不得借 reuse 快捷路径放行
    _write_capability_config(root, capability_pack_enabled=False)
    with pytest.raises(SkillLearningGateError) as re_disabled_exc:
        promote_verified_asset(admin_home, cand, host=admin_host)
    assert re_disabled_exc.value.code == "CAPABILITY_PACK_SELF_INSTALL_DISABLED"
    assert re_disabled_exc.value.detail["effect_outcome"] == "not_started"


def test_w3_11_commit_gate_rechecks_live_switch_and_bound_host(tmp_path: Path) -> None:
    root = tmp_path / "commit_root"
    home_paths = ensure_my_agent_home(root)
    admin_home = home_paths.owner_home_dir
    admin_paths = home_paths_with_owner(home_paths, _owner_from_home(admin_home))
    admin_host = SimpleNamespace(root=root, home_paths=admin_paths)
    cand = _package_candidate("nw-w3-commit-pack")

    # 1) 预检前开启，但在进入 commit 回调后开关切回 false：commit 内 install_and_enable 仍必须拒绝且零写入
    _write_capability_config(root, capability_pack_enabled=True)

    def _toggle_off_in_commit(op):
        _write_capability_config(root, capability_pack_enabled=False)
        return op()

    with pytest.raises(SkillLearningGateError) as commit_exc:
        promote_verified_asset(admin_home, cand, commit=_toggle_off_in_commit, host=admin_host)
    assert commit_exc.value.code == "CAPABILITY_PACK_SELF_INSTALL_DISABLED"
    assert commit_exc.value.detail["effect_outcome"] == "not_started"
    assert PluginInstallStore(_owner_from_home(admin_home)).snapshot() == ()

    # 2) skill_learning._publish 使用 bind_promotion_host 时，无需显式传 host 参数即可读取宿主开关/管理员身份
    disabled_cfg = tmp_path / "override_capability_config.yaml"
    disabled_cfg.write_text("capability_pack_self_install_enabled: false\n", encoding="utf-8")
    _write_capability_config(root, capability_pack_enabled=True)
    host_with_override = SimpleNamespace(
        root=root,
        home_paths=admin_paths,
        capability_config_path=str(disabled_cfg),
    )
    with bind_promotion_host(host_with_override):
        with pytest.raises(SkillLearningGateError) as bound_exc:
            promote_verified_asset(admin_home, cand)
    assert bound_exc.value.code == "CAPABILITY_PACK_SELF_INSTALL_DISABLED"
    assert bound_exc.value.detail["effect_outcome"] == "not_started"
    assert PluginInstallStore(_owner_from_home(admin_home)).snapshot() == ()

    non_admin_bound = SimpleNamespace(
        root=root,
        owner_provider="feishu",
        owner_kind="user",
        owner_id="ou_im_1",
    )
    with bind_promotion_host(non_admin_bound):
        with pytest.raises(SkillLearningGateError) as non_admin_bound_exc:
            promote_verified_asset(admin_home, cand)
    assert non_admin_bound_exc.value.code == "CAPABILITY_PACK_ADMIN_REQUIRED"
    assert non_admin_bound_exc.value.detail["effect_outcome"] == "not_started"
    assert PluginInstallStore(_owner_from_home(admin_home)).snapshot() == ()
