"""F11④ admin/主代理默认降权(owner-scoped)+ admin bypass 提权解除(防自授权 + 强制过期)。

main/admin(终端·主代理)现在也默认 owner-scoped:只看/写自己 owner home 子树 + .my-agent 顶层
公共区,别人 owner home 由 owner 墙拦掉;源码/工作区(在 .my-agent 之外)不受影响,降权不误伤
合法操作;未授权的绝对写入保持原目标语义并由写边界明确拒绝。

bypass(owner.full_access)授权读 my-agent home 根下的 **admin_grants** 目录(owner 写不到的上级
目录)而非 owner 自己的 temporary_grants → 堵自授权漏洞;且强制过期(无有效 expires_at = 无效)。
"""

from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_py_agent.agent.core import (
    _ADMIN_BYPASS_CAPABILITY,
    _has_admin_bypass_grant,
    _resolve_owner_scope_and_access,
)
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.temporary_grants import (
    CreateTemporaryGrant,
    create_temporary_grant,
    has_active_capability_grant,
)


def _home(tmp_path: Path, monkeypatch):
    home = tmp_path / ".my-agent"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    return ensure_my_agent_home(home)


def _iso(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _write_grant(directory: Path, capability: str, *, expires_at: str | None) -> None:
    """直接往授权目录写一张 grant JSON(admin_grants 由真人 admin 写,不经 create_temporary_grant)。
    expires_at=None 表示不带过期字段(测强制过期)。"""
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "owner-temporary-grant.v1",
        "grant_id": "grant_test",
        "status": "active",
        "granted_to": "main",
        "capability": capability,
        "path_prefix": "",
        "reason": "test",
    }
    if expires_at is not None:
        payload["expires_at"] = expires_at
    (directory / "grant_test.json").write_text(json.dumps(payload), encoding="utf-8")


def _agent(home):
    return types.SimpleNamespace(home_paths=home)


def _config(access_mode: str = "workspace-write"):
    return types.SimpleNamespace(access_mode=access_mode)


def test_main_defaults_to_owner_scoped(tmp_path, monkeypatch) -> None:
    """无 bypass 授权时,main 默认降权到自己 owner home;access_mode 不变。"""
    home = _home(tmp_path, monkeypatch)
    scope, access = _resolve_owner_scope_and_access(_agent(home), _config())
    assert scope == str(home.owner_home_dir)
    assert access == "workspace-write"


def test_admin_bypass_grant_lifts_scope(tmp_path, monkeypatch) -> None:
    """admin_grants 里有有效 owner.full_access → 解除降权:scope 清空 + access 提到 full-access。"""
    home = _home(tmp_path, monkeypatch)
    _write_grant(home.admin_grants_dir, _ADMIN_BYPASS_CAPABILITY, expires_at=_iso(1))
    assert _has_admin_bypass_grant(home) is True
    scope, access = _resolve_owner_scope_and_access(_agent(home), _config())
    assert scope == ""
    assert access == "full-access"


def test_expired_bypass_grant_keeps_scope(tmp_path, monkeypatch) -> None:
    """过期的 bypass 授权不算数 → 保持降权。"""
    home = _home(tmp_path, monkeypatch)
    _write_grant(home.admin_grants_dir, _ADMIN_BYPASS_CAPABILITY, expires_at=_iso(-1))
    assert _has_admin_bypass_grant(home) is False
    scope, access = _resolve_owner_scope_and_access(_agent(home), _config())
    assert scope == str(home.owner_home_dir)
    assert access == "workspace-write"


def test_bypass_requires_expiry(tmp_path, monkeypatch) -> None:
    """强制过期:bypass 授权缺 expires_at(永久授权)一律无效 → 保持降权。"""
    home = _home(tmp_path, monkeypatch)
    _write_grant(home.admin_grants_dir, _ADMIN_BYPASS_CAPABILITY, expires_at=None)
    assert _has_admin_bypass_grant(home) is False
    scope, _ = _resolve_owner_scope_and_access(_agent(home), _config())
    assert scope == str(home.owner_home_dir)


def test_unrelated_grant_does_not_bypass(tmp_path, monkeypatch) -> None:
    """admin_grants 里其他 capability 的授权不触发 bypass。"""
    home = _home(tmp_path, monkeypatch)
    _write_grant(home.admin_grants_dir, "some.other.capability", expires_at=_iso(1))
    assert has_active_capability_grant(
        home.admin_grants_dir, _ADMIN_BYPASS_CAPABILITY, require_expiry=True
    ) is False
    assert _has_admin_bypass_grant(home) is False


def test_self_authored_owner_grant_does_not_bypass(tmp_path, monkeypatch) -> None:
    """🔴 自授权防护:owner 往自己的 temporary_grants 写 owner.full_access(自提权尝试)→ 不生效,
    因为 bypass 只读 admin_grants(owner 上级目录),不读 owner 自己的授权目录。"""
    home = _home(tmp_path, monkeypatch)
    create_temporary_grant(
        home,
        CreateTemporaryGrant(
            granted_to="main",
            capability=_ADMIN_BYPASS_CAPABILITY,
            path_prefix="",
            expires_at=_iso(1),
            reason="self-escalation attempt",
        ),
    )
    assert _has_admin_bypass_grant(home) is False
    scope, access = _resolve_owner_scope_and_access(_agent(home), _config())
    assert scope == str(home.owner_home_dir)  # 仍降权,自授权无效
    assert access == "workspace-write"


def test_owner_scoped_main_wall_and_workspace(tmp_path, monkeypatch) -> None:
    """降权后 PathAccessPolicy 实际行为:自己 home/shared 放行，任意宿主工作区、
    别人 owner home 和 admin_grants 都拦。当前任务显式授权的外部目录由文件工具的
    workspace contract 单独放行，不能在 owner 基础策略里永久开口。"""
    home = _home(tmp_path, monkeypatch)
    scope, _ = _resolve_owner_scope_and_access(_agent(home), _config())
    policy = PathAccessPolicy.from_values(owner_scope_root=scope)
    # ① 自己 owner home 子树
    assert policy.check(home.owner_home_dir / "memory" / "long_term" / "x.jsonl").allowed
    # ② 任意宿主 workspace 默认不属于这个 owner。
    external = policy.check(tmp_path / "repo" / "src" / "a.py")
    assert external.allowed is False and external.code == "PATH_OWNER_SCOPE_BLOCKED"
    # ④ 别人 owner home → 拦
    other = home.root / "owners" / "providers" / "feishu" / "users" / "B" / "SOUL.md"
    decision = policy.check(other)
    assert decision.allowed is False and decision.code == "PATH_CROSS_OWNER_BLOCKED"
    # admin_grants → 拦(防自授权,即便没任务上下文/bwrap 不可用也兜底)
    blocked = policy.check(home.admin_grants_dir / "grant_x.json")
    assert blocked.allowed is False and blocked.code == "PATH_ADMIN_GRANTS_BLOCKED"
    # 顶层公共区(非 owners/、非 admin_grants/)放行
    assert policy.check(home.root / "shared" / "skills" / "x" / "SKILL.md").allowed


def test_bypass_restores_cross_owner_visibility(tmp_path, monkeypatch) -> None:
    """bypass 解除降权后(scope=""),owner 墙不再生效,管理员可看所有 owner 及 admin_grants。"""
    home = _home(tmp_path, monkeypatch)
    _write_grant(home.admin_grants_dir, _ADMIN_BYPASS_CAPABILITY, expires_at=_iso(1))
    scope, access = _resolve_owner_scope_and_access(_agent(home), _config())
    assert scope == "" and access == "full-access"
    policy = PathAccessPolicy.from_values(owner_scope_root=scope)
    other = home.root / "owners" / "providers" / "feishu" / "users" / "B" / "SOUL.md"
    assert policy.check(other).allowed  # 全权:别人 home 也放行
    assert policy.check(home.admin_grants_dir / "grant_x.json").allowed  # 全权:admin_grants 也放行
