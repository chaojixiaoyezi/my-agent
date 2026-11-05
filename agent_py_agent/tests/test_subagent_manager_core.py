from __future__ import annotations

import json
from pathlib import Path

import pytest


# LLM: 独立解释器屏蔽fcntl并重载；Windows模拟同时覆盖dir_fd缺失与3.12路径/FD的ctime异义，保留真实SQLite和闸门。
# 函数用途: 验证本机和Windows无目录FD时均能保存、读写、更新数据源，关闭只读连接的写权限。
@pytest.mark.parametrize("simulate_windows", [False, True], ids=["native", "windows-no-dirfd"])
def test_datasource_store_import_and_io_without_fcntl(tmp_path: Path, simulate_windows):
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    probe = r'''
import importlib
import os
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from pytest import MonkeyPatch, raises

root, home = map(Path, sys.argv[1:3])
sys.path.insert(0, str(root))
from agent_py_agent.agent import assets
from agent_py_agent.agent.assets import datasource_store
from agent_py_agent.agent.common import nofollow_fs
from agent_py_agent.agent.gateway_parts import standalone_inventory
from agent_py_agent.agent.gateway_parts.standalone_migration_errors import StandaloneMigrationRejected

assert Path(datasource_store.__file__).resolve().is_relative_to(root)
with MonkeyPatch.context() as patch:
    patch.setitem(sys.modules, "fcntl", None)
    importlib.reload(datasource_store)
    assert datasource_store.fcntl is None
    if sys.argv[3] == "windows":
        patch.setattr(datasource_store, "os", SimpleNamespace(**{
            **vars(os), "name": "nt", "O_NOFOLLOW": 0, "O_DIRECTORY": 0,
        }))
        for module in (assets, datasource_store, nofollow_fs):
            patch.setattr(module, "_supports_dir_fd", lambda: False)
        patch.setattr(datasource_store, "open_directory_beneath",
            Mock(side_effect=AssertionError("Windows must not open directory FDs")))
        if os.name != "nt":
            # LLM: 仅在非Windows模拟：空文件创建/变更时间重合，写入后FD变更时间与路径创建时间分离；不改变身份。
            # 函数用途: 在macOS/Linux复现Windows3.12写库后才出现的ctime来源差异。
            def windows_fstat(descriptor):
                info = os.fstat(descriptor)
                if info.st_size == 0:
                    return info
                fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                fields["st_ctime_ns"] += 1_000_000_000
                return SimpleNamespace(**fields)
            patch.setattr(datasource_store.os, "fstat", windows_fstat)
    store = datasource_store.DatasourceStore(home)
    saved = store.save_datasource({"datasource_id": "portable", "kind": "sqlite", "title": "portable"})
    assert saved["revision"] == 1
    spec = store.get_datasource_spec("portable")
    control = store.writer_control(spec)
    with datasource_store.shared_writer_gate(control):
        connection = datasource_store.open_sqlite(spec)
        try:
            connection.execute("CREATE TABLE records (value TEXT)")
            connection.execute("INSERT INTO records VALUES ('retained')")
            connection.commit()
        finally:
            connection.close()
    store.save_datasource({"datasource_id": "portable", "kind": "sqlite", "title": "updated",
        "database_path": spec.database_path}, expected_revision=1)
    updated = store.get_datasource_spec("portable")
    assert updated.revision == 2 and updated.title == "updated"
    connection = sqlite3.connect(updated.database_path)
    try:
        assert connection.execute("SELECT value FROM records").fetchall() == [("retained",)]
    finally:
        connection.close()
    for readonly_spec in (updated, replace(updated, read_only=True)):
        connection = datasource_store.open_sqlite(readonly_spec, read_only=readonly_spec is updated)
        try:
            assert connection.execute("SELECT value FROM records").fetchall() == [("retained",)]
            with raises(sqlite3.OperationalError):
                connection.execute("INSERT INTO records VALUES ('not-allowed')")
        finally:
            connection.close()
    assert store.list_datasources()[0]["revision"] == 2
    with raises(StandaloneMigrationRejected):
        standalone_inventory._settled_approval_lock(home, ("unverified.lock",), {})
'''
    result = subprocess.run([sys.executable, "-B", "-c", probe, str(root), str(tmp_path / "home"),
        "windows" if simulate_windows else "native"],
        cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


# LLM: 仅替换数据源模块os视图，保留本机Path及真实文件IO；严格入口毒化防止模拟在POSIX上意外借用目录FD。
# 函数用途: 为Windows安全反例准备无O_NOFOLLOW、无目录打开能力的SQLite入口。
@pytest.fixture
def windows_datasource_store(monkeypatch):
    import os
    from types import SimpleNamespace
    from unittest.mock import Mock

    from agent_py_agent.agent.assets import datasource_store

    monkeypatch.setattr(datasource_store, "os", SimpleNamespace(**{
        **vars(os), "name": "nt", "O_NOFOLLOW": 0, "O_DIRECTORY": 0,
    }))
    for name in ("open_readonly_file_beneath", "open_directory_beneath"):
        monkeypatch.setattr(datasource_store, name,
            Mock(side_effect=AssertionError("Windows must not call POSIX strict open")))
    return datasource_store


# LLM: 不要求Windows创建符号链接的管理员权限；仅向真实lstat结果注入被测组件的链接/reparse/type/nlink元数据。
# 函数用途: 确认Windows拒绝叶子、父目录及祖先的重定向和不安全叶子，且拒绝发生在文件/SQLite打开之前。
@pytest.mark.parametrize("component,unsafe", [
    ("leaf", "symlink"), ("parent", "symlink"), ("ancestor", "symlink"),
    ("leaf", "reparse"), ("parent", "reparse"), ("ancestor", "reparse"),
    ("leaf", "directory"), ("leaf", "hardlink"),
])
def test_windows_sqlite_rejects_unsafe_path_components(
        tmp_path, monkeypatch, windows_datasource_store, component, unsafe):
    import stat
    from types import SimpleNamespace
    from unittest.mock import Mock

    store = windows_datasource_store
    path = tmp_path.resolve() / "ancestor" / "parent" / "data.sqlite"
    path.parent.mkdir(parents=True)
    path.touch()
    bad = {"leaf": path, "parent": path.parent, "ancestor": path.parent.parent}[component]
    real_lstat = Path.lstat

    # LLM: 仅改变本用例指定组件的元数据；其余路径仍从真实文件系统读取。
    # 函数用途: 模拟无需提权即可覆盖的Windows链接、重解析点与非法叶子。
    def unsafe_lstat(candidate, *args, **kwargs):
        info = real_lstat(candidate, *args, **kwargs)
        if candidate != bad:
            return info
        fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        if unsafe == "symlink":
            fields["st_mode"] = stat.S_IFLNK | 0o777
        elif unsafe == "reparse":
            fields["st_file_attributes"] = stat.FILE_ATTRIBUTE_REPARSE_POINT
        elif unsafe == "directory":
            fields["st_mode"] = stat.S_IFDIR | 0o700
        else:
            fields["st_nlink"] = 2
        return SimpleNamespace(**fields)

    monkeypatch.setattr(Path, "lstat", unsafe_lstat)
    open_file = Mock(side_effect=AssertionError("unsafe path must not be opened"))
    connect = Mock(side_effect=AssertionError("unsafe path must not reach SQLite"))
    monkeypatch.setattr(store.os, "open", open_file)
    monkeypatch.setattr(store, "sqlite3", SimpleNamespace(connect=connect))
    spec = store.DatasourceConnectionSpec("unsafe", "sqlite", "unsafe", database_path=str(path))
    with pytest.raises(ValueError, match="DATASOURCE_UNSAFE_DATABASE_PATH"):
        store.open_sqlite(spec)
    open_file.assert_not_called()
    connect.assert_not_called()


# LLM: 真实FD和SQLite，仅在指定边界伪造身份、硬链或同源ctime漂移；不把合法跨源ctime差异当作绕过检查的理由。
# 函数用途: 验证Windows身份/链接数/叶与父链时间漂移仍会被拒绝，且关闭真实FD和连接。
@pytest.mark.parametrize("drift", [
    "open_inode", "open_device", "open_hardlink", "connect_inode", "connect_parent",
    "connect_fd_ctime", "connect_path_ctime",
])
def test_windows_sqlite_rejects_path_drift_and_closes_resources(
        tmp_path, monkeypatch, windows_datasource_store, drift):
    import os
    import sqlite3
    from types import SimpleNamespace

    store = windows_datasource_store
    path = tmp_path.resolve() / "data.sqlite"
    path.touch()
    descriptors, connections = [], []
    real_open, real_fstat, real_stat, real_lstat = os.open, os.fstat, os.stat, Path.lstat
    real_connect = sqlite3.connect

    # LLM: 不替换打开实现或伪造成功，记录需要在失败出口真实关闭的文件描述符。
    # 函数用途: 记录校验FD。
    def track_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    # LLM: 保留真实元数据，只变更当前反例指定的一个字段。
    # 函数用途: 构造可证伪的inode或父目录ctime漂移。
    def changed(info, field):
        values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        values[field] += 1
        return SimpleNamespace(**values)

    # LLM: 调用真实SQLite后才记录连接，后续漂移不得使该连接以成功状态返回。
    # 函数用途: 保存供拒绝后核对关闭状态的真实连接。
    def track_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(store.os, "open", track_open)
    monkeypatch.setattr(store, "sqlite3", SimpleNamespace(connect=track_connect))
    if drift.startswith("open_"):
        field = {"open_inode": "st_ino", "open_device": "st_dev", "open_hardlink": "st_nlink"}[drift]
        monkeypatch.setattr(store.os, "fstat", lambda fd: changed(real_fstat(fd), field))
    elif drift == "connect_inode":
        monkeypatch.setattr(store.os, "stat",
            lambda *args, **kwargs: changed(real_stat(*args, **kwargs), "st_ino")
            if connections else real_stat(*args, **kwargs))
    elif drift == "connect_fd_ctime":
        monkeypatch.setattr(store.os, "fstat",
            lambda fd: changed(real_fstat(fd), "st_ctime_ns") if connections else real_fstat(fd))
    else:
        component = path if drift == "connect_path_ctime" else path.parent
        monkeypatch.setattr(Path, "lstat",
            lambda candidate, *args, **kwargs: changed(real_lstat(candidate, *args, **kwargs), "st_ctime_ns")
            if candidate == component and connections else real_lstat(candidate, *args, **kwargs))
    spec = store.DatasourceConnectionSpec("drift", "sqlite", "drift", database_path=str(path))
    with pytest.raises(PermissionError, match="DATASOURCE_SQLITE_PATH_REPLACED"):
        store.open_sqlite(spec)
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        real_fstat(descriptors[0])
    assert len(connections) == (0 if drift.startswith("open_") else 1)
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


# LLM: CPython3.12 Windows win32_xstat用birthtime覆盖ctime，os.fstat保留ChangeTime；仅模拟稳定时间差，不改dev/ino/类型/硬链数。
# 函数用途: 写过数据的SQLite在两个时间来源不同的情况下仍可只读或可写打开，并保持只读限制。
@pytest.mark.parametrize("read_only", [False, True])
def test_windows_sqlite_accepts_distinct_path_and_fd_ctime_after_write(
        tmp_path, monkeypatch, windows_datasource_store, read_only):
    import sqlite3
    from types import SimpleNamespace

    store = windows_datasource_store
    path = tmp_path.resolve() / "written.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.execute("INSERT INTO records VALUES ('retained')")
    real_fstat = store.os.fstat

    # LLM: 同一FD前后返回同一稳定ChangeTime，但故意不同于路径时间；身份及其它元数据来自真实fstat。
    # 函数用途: 防止只在空库或POSIX相同ctime条件下验证Windows兼容性。
    def changed_time_fstat(descriptor):
        info = real_fstat(descriptor)
        fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        fields["st_ctime_ns"] += 1_000_000_000
        return SimpleNamespace(**fields)

    monkeypatch.setattr(store.os, "fstat", changed_time_fstat)
    spec = store.DatasourceConnectionSpec("written", "sqlite", "written", database_path=str(path))
    connection = store.open_sqlite(spec, read_only=read_only)
    try:
        assert connection.execute("SELECT value FROM records").fetchall() == [("retained",)]
        if read_only:
            with pytest.raises(sqlite3.OperationalError):
                connection.execute("INSERT INTO records VALUES ('forbidden')")
        else:
            connection.execute("INSERT INTO records VALUES ('written')")
            connection.commit()
    finally:
        connection.close()
    with sqlite3.connect(path) as check:
        expected = [("retained",)] if read_only else [("retained",), ("written",)]
        assert check.execute("SELECT value FROM records ORDER BY rowid").fetchall() == expected


# LLM: 即使在Windows运行也仅代理被测模块的平台名，不改全局os.name；强制缺少dir_fd应保持POSIX失败关闭。
# 函数用途: 防止以后将Windows局部兼容分支扩大成POSIX静默降级。
def test_sqlite_posix_keeps_strict_open_without_dirfd(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    from unittest.mock import Mock

    from agent_py_agent.agent.assets import datasource_store
    from agent_py_agent.agent.common import nofollow_fs

    path = tmp_path.resolve() / "data.sqlite"
    path.touch()
    monkeypatch.setattr(datasource_store, "os", SimpleNamespace(**{**vars(os), "name": "posix"}))
    monkeypatch.setattr(nofollow_fs, "_supports_dir_fd", lambda: False)
    connect = Mock(side_effect=AssertionError("POSIX must fail before SQLite"))
    monkeypatch.setattr(datasource_store, "sqlite3", SimpleNamespace(connect=connect))
    spec = datasource_store.DatasourceConnectionSpec("strict", "sqlite", "strict", database_path=str(path))
    with pytest.raises(nofollow_fs.NoFollowPathError, match="strict no-follow opening is unavailable"):
        datasource_store.open_sqlite(spec)
    connect.assert_not_called()


def test_manager_builds_standard_work_order_paths(tmp_path: Path):
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    manager = SubAgentManager(workspace=tmp_path)
    paths = manager._build_work_order_paths("run_123", extra_write_roots=["/tmp/extra"])

    assert paths["task_dir"] == str(tmp_path / "run_123")
    assert paths["data_dir"] == str(tmp_path / "run_123" / "data")
    assert paths["logs_dir"] == str(tmp_path / "run_123" / "logs")
    assert str(tmp_path / "run_123") in paths["allowed_write_roots"]
    assert "/tmp/extra" in paths["allowed_write_roots"]
    assert "forbidden_write_roots" in paths


def test_manager_creates_work_order_files(tmp_path: Path):
    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.agent.subagents.models import SubAgentTask

    manager = SubAgentManager(workspace=tmp_path)
    task = SubAgentTask(
        id="test_json_files",
        goal="测试任务",
        thought="思考",
        plan=["步骤1"],
        agent_name="test",
        created_at=1234567890.0,
        updated_at=1234567890.0,
        **manager._build_work_order_paths("test_json_files"),
    )

    manager._ensure_work_order_files(task)

    output = json.loads(Path(task.output_json).read_text(encoding="utf-8"))
    status = json.loads(Path(task.status_report_json).read_text(encoding="utf-8"))
    deps = json.loads(Path(task.dependencies_json).read_text(encoding="utf-8"))
    assert Path(task.data_dir).exists()
    assert Path(task.output_dir).exists()
    assert Path(task.logs_dir).exists()
    assert output["run_id"] == "test_json_files"
    assert status["run_id"] == "test_json_files"
    assert deps["dependencies"] == []


def test_manager_does_not_parse_write_roots_from_natural_language():
    import agent_py_agent.agent.subagents.manager as manager

    assert not hasattr(manager, "_extract_write_dirs")


def test_inherited_workspace_root_removes_only_equal_default_deny(tmp_path: Path):
    from agent_py_agent.agent.subagents.models import SubAgentTask
    from agent_py_agent.agent.subagents.services.base import _reconciled_forbidden_write_roots

    task = SubAgentTask(
        id="run_workspace_inheritance",
        goal="在继承工作区中写产品文件",
        thought="按父级权限执行",
        plan=["写入工作区"],
        owner="local/main",
        allowed_write_roots=[str(tmp_path)],
        forbidden_write_roots=[
            str(tmp_path),
            str(tmp_path / ".ssh"),
            str(tmp_path / "Downloads"),
        ],
        attributes={
            "workspace_root": str(tmp_path),
            "workspace_roots": [str(tmp_path)],
        },
    )

    assert _reconciled_forbidden_write_roots(task) == [
        str(tmp_path / ".ssh"),
        str(tmp_path / "Downloads"),
    ]


def test_non_workspace_equal_allow_does_not_remove_default_deny(tmp_path: Path):
    from agent_py_agent.agent.subagents.models import SubAgentTask
    from agent_py_agent.agent.subagents.services.base import _reconciled_forbidden_write_roots

    external = tmp_path / "external"
    task = SubAgentTask(
        id="run_external_scope",
        goal="尝试写外部路径",
        thought="守住工作区上界",
        plan=["核对路径"],
        allowed_write_roots=[str(external)],
        forbidden_write_roots=[str(external)],
        attributes={"workspace_root": str(tmp_path)},
    )

    assert _reconciled_forbidden_write_roots(task) == [str(external)]


def test_remote_owner_keeps_equal_host_home_deny(tmp_path: Path):
    from agent_py_agent.agent.subagents.models import SubAgentTask
    from agent_py_agent.agent.subagents.services.base import _reconciled_forbidden_write_roots

    task = SubAgentTask(
        id="run_remote_owner",
        goal="处理远程 owner 任务",
        thought="保留宿主围栏",
        plan=["核对权限"],
        owner="users/remote-a",
        allowed_write_roots=[str(tmp_path)],
        forbidden_write_roots=[str(tmp_path)],
        attributes={"workspace_root": str(tmp_path)},
    )

    assert _reconciled_forbidden_write_roots(task) == [str(tmp_path)]


def test_create_run_applies_inherited_workspace_reconciliation(tmp_path: Path, monkeypatch) -> None:
    from agent_py_agent.agent.subagents import manager_work_orders
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    monkeypatch.setattr(
        manager_work_orders,
        "_default_forbidden_write_roots",
        lambda: [str(tmp_path), str(tmp_path / ".ssh")],
    )
    manager = SubAgentManager(workspace=tmp_path, workspace_root=tmp_path)

    task = manager.create_run(
        goal="在父级工作区写一个文件",
        thought="继承父级边界",
        plan=["写入", "回报"],
        owner="local/main",
        extra_write_roots=[str(tmp_path)],
    )

    assert str(tmp_path) in task.allowed_write_roots
    assert str(tmp_path) not in task.forbidden_write_roots
    assert str(tmp_path / ".ssh") in task.forbidden_write_roots


# LLM: 检查本测试临时树的目录和实际字节，不能以 mocked save 的调用次数替代无落盘证据。
# 函数用途: 记录准备或失败前后的文件树，发现隐藏的任务、线程、空目录及账本写入。
def _prepared_tree_snapshot(root: Path) -> dict[str, bytes | None]:
    return {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None for path in root.rglob("*")}


@pytest.mark.parametrize("workspace_override", [False, True])
def test_prepared_root_child_grandchild_commit_the_same_object_and_identity(tmp_path, monkeypatch, workspace_override):
    from dataclasses import asdict

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.subagents.services import base
    from agent_py_agent.agent.subagents.services.runner_context_service import (
        project_execution_context,
    )

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    manager = agent.subagents
    parent = None
    root_id = ""
    attributes = {"run_workspace": {"task_root": str(tmp_path / "task-space")}} if workspace_override else {}
    extra_root = str(tmp_path / "authorized-output")
    for depth in range(3):
        params = base.CreateRunParams(goal="核对材料", thought="按授权范围检查", plan=["检查", "汇报"],
            parent_id=parent.id if parent else "", root_id=root_id, depth=depth,
            allowed_tools=["read_file"], parent_access_mode="restricted", attributes=attributes,
            extra_write_roots=[extra_root])
        before = _prepared_tree_snapshot(tmp_path)
        prepared = manager.base_service.prepare_run(params=params)
        raw_task = asdict(prepared.task)
        request = manager.runner_context.prepare_execution_context(prepared.task)
        projected = project_execution_context(request)
        assert _prepared_tree_snapshot(tmp_path) == before
        assert asdict(prepared.task) == raw_task
        assert request.unresolved_fields == ()
        assert not Path(request.task.agent_run_workspace_dir).exists()
        assert request.task.daily_ledger_file == request.task.daily_ledger_last_event_id == ""
        assert extra_root in request.task.allowed_write_roots
        assert prepared.task.task_dir not in request.task.allowed_write_roots
        with pytest.raises(FileNotFoundError):
            manager.load(prepared.run_id)
        assert agent.conversation_store.threads.load(prepared.task.agent_thread_id) is None
        original_identity = (prepared.run_id, prepared.task.agent_thread_id, prepared.task.subagent_session_id)
        with monkeypatch.context() as guard:
            guard.setattr(base, "_framework_new_id", lambda *_: pytest.fail("提交不能再生成 run_id"))
            created = manager.base_service.create_run(params=params, prepared=prepared)
        assert created is prepared.task
        assert (created.id, created.agent_thread_id, created.subagent_session_id) == original_identity
        assert manager.load(created.id).id == prepared.run_id
        thread = agent.conversation_store.threads.load(created.agent_thread_id)
        assert thread is not None
        assert created.effective_permissions["shell_access_mode"] == "restricted"
        live = manager.runner_context.build_execution_context(created.id)
        assert projected.task_dir == live.task_dir
        assert projected.execution_context_json == live.execution_context_json
        assert projected.execution_context_file == live.execution_context_file
        assert projected.context_bundle_json == live.context_bundle_json
        assert projected.context_bundle_file == live.context_bundle_file
        assert projected.context_bundle["workspace_refs"] == live.context_bundle["workspace_refs"]
        assert projected.write_boundary == live.write_boundary
        assert request.task.task_artifact_manifest_jsonl == created.task_artifact_manifest_jsonl
        assert request.task.agent_run_artifact_manifest_jsonl == created.agent_run_artifact_manifest_jsonl
        assert created.daily_ledger_file and created.daily_ledger_last_event_id
        if parent:
            assert created.parent_subagent_session_id == parent.subagent_session_id
            assert created.root_subagent_session_id == parent.root_subagent_session_id
            assert created.id in manager.load(parent.id).child_ids
        root_id = root_id or created.id
        assert created.root_id == root_id
        parent = created
    assert len(manager.list_runs()) == 3


@pytest.mark.parametrize("changed", ["input", "identity", "task_permission", "owner_policy", "parent_revision"])
def test_prepared_run_rejects_drift_before_any_materialization(tmp_path, changed):
    from dataclasses import replace

    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="父任务", parent_access_mode="restricted")
    params = CreateRunParams(goal="子任务", thought="检查", plan=["核对"], parent_id=parent.id,
        root_id=parent.id, depth=1, allowed_tools=["read_file"])
    prepared = manager.base_service.prepare_run(params=params)
    if changed == "input":
        params = replace(params, allowed_tools=["write_file"])
    elif changed == "identity":
        prepared.task.id = "another-valid-run"
    elif changed == "task_permission":
        prepared.task.effective_permissions["shell_access_mode"] = "full-access"
    elif changed == "owner_policy":
        manager.owner_policy_snapshot = {"tools": {"disabled_tools": ["read_file"]}}
    else:
        manager.mutate(parent.id, lambda task: task.blockers.append("父任务已更新"))
    before = _prepared_tree_snapshot(tmp_path)
    with pytest.raises(ValueError, match="已变化"):
        manager.base_service.create_run(params=params, prepared=prepared)
    assert _prepared_tree_snapshot(tmp_path) == before
    assert len(manager.list_runs()) == 1


def test_prepared_run_isolated_from_input_mutation_and_cannot_publish_twice(tmp_path):
    from copy import deepcopy

    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    manager = SubAgentManager(tmp_path)
    params = CreateRunParams(goal="子任务", thought="检查", plan=["核对"],
        allowed_tools=["read_file"], context_packs=[{"summary": "输入材料"}])
    prepared = manager.base_service.prepare_run(params=params)
    params.context_packs[0]["summary"] = "后来修改"
    assert prepared.params.context_packs[0]["summary"] == "输入材料"
    assert prepared.task.context_packs[0]["summary"] == "输入材料"
    unchanged = deepcopy(prepared)
    created = manager.base_service.create_run(params=prepared.params, prepared=prepared)
    before = _prepared_tree_snapshot(tmp_path)
    with pytest.raises(ValueError, match="已经发布"):
        manager.base_service.create_run(params=unchanged.params, prepared=unchanged)
    assert _prepared_tree_snapshot(tmp_path) == before
    assert [task.id for task in manager.list_runs()] == [created.id]


def test_refreeze_preserves_task_identity_and_never_changes_published_task(tmp_path):
    from dataclasses import asdict, replace

    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="父任务")
    params = CreateRunParams(goal="子任务", thought="检查", plan=["核对"], parent_id=parent.id,
        root_id=parent.id, attributes={"host_model_profile.v1": {"profile_id": "default"}})
    prepared = manager.base_service.prepare_run(params=params)
    manager.create_run(goal="同批先保存的孩子", parent_id=parent.id, root_id=parent.id)
    selected = replace(params, attributes={"host_model_profile.v1": {"profile_id": "selected"}})
    before = _prepared_tree_snapshot(tmp_path)
    updated = manager.base_service.refreeze_run(params=selected, prepared=prepared)
    assert _prepared_tree_snapshot(tmp_path) == before
    assert updated.task is prepared.task
    assert (updated.run_id, updated.created_at) == (prepared.run_id, prepared.created_at)
    assert updated.parent_revision > prepared.parent_revision
    created = manager.create_run(params=selected, prepared=updated)
    original = asdict(created)
    before = _prepared_tree_snapshot(tmp_path)
    with pytest.raises(ValueError, match="已经发布"):
        manager.base_service.refreeze_run(params=params, prepared=updated)
    assert asdict(created) == original
    assert _prepared_tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", ["runs", "collaboration"])
@pytest.mark.parametrize("has_fchmod", [True, False])
def test_private_indexes_work_without_directory_fds(tmp_path, monkeypatch, kind, has_fchmod):
    """使用真实SQLite；模拟Windows权限位并确认种子FD在连接前已经关闭。"""
    import os
    import sqlite3
    import stat
    from types import SimpleNamespace

    from agent_py_agent.agent.collaboration import history_index
    from agent_py_agent.agent.collaboration.store import CollaborationStore
    from agent_py_agent.agent.common import nofollow_fs
    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.agent.subagents.services.persistence import run_index

    module = run_index if kind == "runs" else history_index
    monkeypatch.setattr(nofollow_fs, "_supports_dir_fd", lambda: False)
    if not has_fchmod:
        monkeypatch.setattr(module, "os", SimpleNamespace(**{key: value for key, value in vars(os).items()
                                                           if key != "fchmod"}))
        real_lstat = Path.lstat

        def windows_mode(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if ".sqlite3" not in path.name:
                return info
            values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
            values["st_mode"] = stat.S_IFMT(info.st_mode) | 0o666
            return SimpleNamespace(**values)

        monkeypatch.setattr(Path, "lstat", windows_mode)
    original_seed, original_connect = module.open_private_lock_beneath, sqlite3.connect
    descriptors = []

    def seed(*args, **kwargs):
        descriptor = original_seed(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    def connect(*args, **kwargs):
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
        descriptors.clear()
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(module, "open_private_lock_beneath", seed)
    monkeypatch.setattr(module, "sqlite3", SimpleNamespace(connect=connect))
    if kind == "runs":
        manager = SubAgentManager(tmp_path)
        index = manager.persistence.run_index
    else:
        store = CollaborationStore(tmp_path)
        index = store.history_index
    with index.connect() as db:
        db.execute("CREATE TABLE portable_probe (value TEXT)")
        db.execute("INSERT INTO portable_probe VALUES ('committed')")
    with pytest.raises(RuntimeError, match="rollback probe"):
        with index.connect() as db:
            db.execute("INSERT INTO portable_probe VALUES ('rolled back')")
            raise RuntimeError("rollback probe")
    with index.connect() as db:
        assert db.execute("SELECT value FROM portable_probe").fetchall() == [("committed",)]
        if kind == "collaboration":
            assert db.execute("PRAGMA foreign_keys").fetchone() == (1,)


def test_run_index_backfill_without_directory_fds(tmp_path, monkeypatch):
    from dataclasses import asdict

    from agent_py_agent.agent.common import nofollow_fs
    from agent_py_agent.agent.subagents.models import SubAgentTask
    from agent_py_agent.agent.subagents.services.persistence.run_index import SubagentRunIndex

    run = tmp_path / "legacy-run"
    run.mkdir()
    task = SubAgentTask(id=run.name, goal="历史任务", thought="核验迁移", plan=["读取", "索引"])
    (run / "task.json").write_text(json.dumps(asdict(task)), encoding="utf-8")
    index = SubagentRunIndex(tmp_path)
    monkeypatch.setattr(nofollow_fs, "_supports_dir_fd", lambda: False)

    def load(run_id):
        return SubAgentTask(**json.loads((tmp_path / run_id / "task.json").read_text(encoding="utf-8")))

    result = index.backfill(load)
    assert result["loaded"] == 1 and result["complete"] and result["errors"] == []
    assert index.known_ids([run.name]) == {run.name}
