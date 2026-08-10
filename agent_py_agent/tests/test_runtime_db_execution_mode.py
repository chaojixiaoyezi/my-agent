"""G4 补 1（3.txt）：ExecutionMode 显式枚举 + 权威库挂载三态。

修复前：权威库挂载失败（owner_home_dir 空 / DB 打不开）时 runtime_db 被
置 None，各入口「没找到 DB 就跳过权威链」——托管运行与本地投影走同一
代码路径，旁路是隐式的。现在执行模式由框架显式传入：

- MANAGED：必须完整权威链。无 home 或挂载失败 → 构造即抛（fail-closed）。
- LOCAL_UNMANAGED：显式选择纯文件层/投影，允许无权威库。
- 未显式传入 → 按有无 owner_home_dir 推断，兼容存量调用。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.runtime_db.execution_mode import (
    ExecutionMode,
    expects_managed_authority,
    resolve_execution_mode,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager


def test_resolve_explicit_mode_wins_over_home():
    # 显式模式优先于 home 推断：无 home 也能显式 MANAGED（有挂载点才挂）。
    assert (
        resolve_execution_mode("managed", has_home_dir=False)
        is ExecutionMode.MANAGED
    )
    assert (
        resolve_execution_mode(ExecutionMode.LOCAL_UNMANAGED, has_home_dir=True)
        is ExecutionMode.LOCAL_UNMANAGED
    )


def test_resolve_inferred_from_home():
    # 未显式 → 有 home 即 MANAGED（存量行为：挂权威库）。
    assert resolve_execution_mode(None, has_home_dir=True) is ExecutionMode.MANAGED
    assert (
        resolve_execution_mode(None, has_home_dir=False)
        is ExecutionMode.LOCAL_UNMANAGED
    )


def test_resolve_illegal_mode_fails_closed():
    # 拼错模式名不得静默落到推断/投影路径 → ValueError。
    with pytest.raises(ValueError, match="非法 execution_mode"):
        resolve_execution_mode("self_managed", has_home_dir=True)


def test_expects_managed_authority():
    # 显式模式优先。
    assert (
        expects_managed_authority(
            type("M", (), {"execution_mode": ExecutionMode.MANAGED})()
        )
        is True
    )
    assert (
        expects_managed_authority(
            type("M", (), {"execution_mode": ExecutionMode.LOCAL_UNMANAGED})()
        )
        is False
    )
    # 字符串手设与枚举等价（防身份比较误判 fail-open）。
    assert expects_managed_authority(type("M", (), {"execution_mode": "managed"})())
    assert (
        expects_managed_authority(
            type("M", (), {"execution_mode": "local_unmanaged"})()
        )
        is False
    )
    # 未知模式值 → 按 MANAGED（fail-closed：无法证明非托管 = 要权威）。
    assert expects_managed_authority(type("M", (), {"execution_mode": "weird"})())
    # 未显式化（老路径兜底）→ 按有无 owner_home_dir。
    assert expects_managed_authority(type("M", (), {"owner_home_dir": "/tmp/x"})())
    assert (
        expects_managed_authority(type("M", (), {"owner_home_dir": ""})())
        is False
    )


def test_manager_local_unmanaged_skips_runtime_db(tmp_path):
    # 显式非托管：无权威库（纯投影），不挂载不抛。
    manager = SubAgentManager(
        tmp_path / "ws_unmanaged",
        owner_home_dir="",
        execution_mode=ExecutionMode.LOCAL_UNMANAGED.value,
    )
    assert manager.runtime_db is None
    assert manager.execution_mode is ExecutionMode.LOCAL_UNMANAGED


def test_manager_managed_without_home_fails_fast(tmp_path):
    # MANAGED 但无挂载点 → 构造即抛（fail-closed，不延迟到调用点）。
    with pytest.raises(ValueError, match="必须有 owner_home_dir"):
        SubAgentManager(
            tmp_path / "ws_managed",
            owner_home_dir="",
            execution_mode=ExecutionMode.MANAGED.value,
        )


def test_manager_managed_with_home_attaches(tmp_path):
    # MANAGED + home：权威库挂载成功，执行模式落枚举。
    home = tmp_path / "home"
    manager = SubAgentManager(
        tmp_path / "ws_managed2",
        owner_home_dir=str(home),
        execution_mode=ExecutionMode.MANAGED.value,
    )
    assert manager.runtime_db is not None
    assert manager.execution_mode is ExecutionMode.MANAGED
    # 挂载的库可正常建权威链。
    chain = manager.runtime_db.record_run_creation(
        owner_id="local/main", run_id="run-mode", goal="g"
    )
    assert chain["attempt_id"]


def test_manager_unset_mode_inferred_from_home(tmp_path):
    # 未显式传入 → 按 home 推断（存量行为不变）：无 home 不挂、不抛。
    manager = SubAgentManager(tmp_path / "ws_inferred")
    assert manager.runtime_db is None
    assert manager.execution_mode is ExecutionMode.LOCAL_UNMANAGED
