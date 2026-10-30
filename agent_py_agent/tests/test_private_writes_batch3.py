# LLM: 本文件钉住第三批宿主私有写（pw3）：改动过的写入点新文件必须 0600、新目录 0700、
#   原子写不留半截、旧数据照常可读。任何一个写入点退回普通写时，这些用例必须变红。
#   optimistic_lock 的版本锁记录在子代理工作区（用户可见产出区），按 ds10/ds1 裁定不收私、
#   恢复原样，不在本批覆盖（17j sclk 段已登记"非私有状态目录，未改"）。
# 模块用途: 验证 pw3 各写入点的权限与原子性契约，不测业务语义。

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


# LLM: 私有写回归只读取真实 stat 权限位，不模拟产品权限结果；修改须保持下游 0600/0700 断言含义。
# 函数用途: 取得测试文件或目录的权限位，供权限断言复用。
def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# LLM: 私有写夹具会修改进程全局 umask，必须 finally 恢复；测试中仅证明产品显式建目录模式，不能泄漏到其它用例。
# 函数用途: 在一段测试期间临时设置 umask，退出后还原。
@contextmanager
def _umask(value: int):
    """临时设置进程 umask；本文件用它证明 0700 来自显式 mode，而不是继承调用方的 umask。"""
    old = os.umask(value)
    try:
        yield
    finally:
        os.umask(old)


# LLM: 私有写回归要求真实文件存在且精确 0600，不能放宽为可读或仅存在；此 helper 不改权限。
# 函数用途: 核对产品写出的文件只允许当前系统用户读写。
def _assert_private_file(path: Path) -> None:
    assert path.exists(), f"missing file: {path}"
    assert _mode(path) == 0o600, f"{path} mode={oct(_mode(path))}, expected 0o600"


# LLM: 私有写回归要求真实目录且精确 0700，不能预先 chmod 伪造产品结果；修改须联查所有目录创建断言。
# 函数用途: 核对产品创建的目录仅当前系统用户可访问。
def _assert_private_dir(path: Path) -> None:
    assert path.is_dir(), f"missing dir: {path}"
    assert _mode(path) == 0o700, f"{path} mode={oct(_mode(path))}, expected 0o700"


# LLM: adapter 私有写测试从 tmp_path 构造路径合同，不建目录、不接真实 owner 或通道；产品入口负责实际私有创建。
# 函数用途: 为适配器用例组装隔离的收发队列路径。
def _adapter_paths(root: Path):
    from agent_py_agent.agent.gateway_parts.paths import AdapterPaths

    return AdapterPaths(
        root=root,
        inbox=root / "inbox",
        processing=root / "processing",
        done=root / "done",
        failed=root / "failed",
        outbox=root / "outbox",
    )


# LLM: Gateway 流式私有写回归覆盖首次写入、宽权限收紧与已有行保留；只写 tmp_path，不扩大到投递业务验收。
# 类用途: 核对 chunk 文件及目录的权限和读取兼容性。
class TestChunkStream:
    # LLM: chunk 回归调用真实 write_chunk_event，要求新目录 0700、文件 0600 且 JSON 正文可读，不只检查路径存在。
    # 函数用途: 验证首次流式事件写入同时守住隐私与内容格式。
    def test_write_chunk_event_private_and_readable(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.gateway_parts.stream_writer import write_chunk_event

        chunk = tmp_path / "chunks" / "req.jsonl"
        write_chunk_event(chunk, {"kind": "model_delta", "text": "hi"})
        _assert_private_dir(chunk.parent)
        _assert_private_file(chunk)
        row = json.loads(chunk.read_text(encoding="utf-8").splitlines()[0])
        assert row["kind"] == "model_delta"
        assert row["text"] == "hi"

    # LLM: chunk 回归先造 0644 旧文件，再沿真实追加口收紧并保留旧行；权限修复不得以截断历史换取通过。
    # 函数用途: 验证追加事件会修复宽权限，同时不丢原来的内容。
    def test_write_chunk_event_tightens_existing_and_keeps_old_rows(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.gateway_parts.stream_writer import write_chunk_event

        chunk = tmp_path / "chunks" / "req.jsonl"
        chunk.parent.mkdir(parents=True, exist_ok=True)
        chunk.write_text('{"old": 1}\n', encoding="utf-8")
        os.chmod(chunk, 0o644)
        write_chunk_event(chunk, {"kind": "runtime_progress", "text": "x"})
        _assert_private_file(chunk)
        lines = chunk.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"old": 1}

    # LLM: chunk 回归由 open_chunk_stream 自行创建缺失父目录，核真实返回路径与启动时间；不能由夹具预建目录掩盖权限问题。
    # 函数用途: 验证打开流时新目录即为私有，且启动结果可用。
    def test_open_chunk_stream_creates_private_dir(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.gateway_parts.stream_writer import open_chunk_stream

        chunk = tmp_path / "chunks" / "req.jsonl"
        path, started = open_chunk_stream(chunk)
        assert path == chunk
        assert started > 0
        _assert_private_dir(chunk.parent)


# LLM: adapter 延迟回执回归只写隔离目录，覆盖追加、重写及队列建目录；必须保留请求身份与原数据可读性。
# 类用途: 核对延迟消息索引与适配器目录不会暴露给其它系统用户。
class TestAdapterLate:
    # LLM: adapter 回归直接写 late_pending，要求 0600 及真实 request_id/timeout 往返，不能用空文件替代记录。
    # 函数用途: 验证记录一个待检查延迟请求时保存私有且完整的索引。
    def test_record_late_pending_private(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.gateway_parts.adapter_late import _record_late_pending

        paths = _adapter_paths(tmp_path)
        _record_late_pending(paths, "req_1", 30.0)
        late = tmp_path / "late_pending.jsonl"
        _assert_private_file(late)
        entry = json.loads(late.read_text(encoding="utf-8").splitlines()[0])
        assert entry["request_id"] == "req_1"
        assert entry["original_timeout"] == 30.0

    # LLM: adapter 回归以宽权限旧索引触发真实未检查记录重写，核权限收紧且请求号保留；不能改变重写业务筛选语义。
    # 函数用途: 验证重写延迟索引后仍可读取原请求，并变为私有文件。
    def test_rewrite_unchecked_private_and_readable(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.gateway_parts.adapter_late import _rewrite_unchecked_late_entries

        late = tmp_path / "late_pending.jsonl"
        late.write_text('{"request_id": "old", "checked": false}\n', encoding="utf-8")
        os.chmod(late, 0o644)
        _rewrite_unchecked_late_entries(late, [{"request_id": "old", "checked": False}], None)
        _assert_private_file(late)
        assert json.loads(late.read_text(encoding="utf-8").splitlines()[0])["request_id"] == "old"

    # LLM: adapter 回归由产品 _ensure_adapter_dirs 建全部五个队列目录，逐个核 0700，不预建或事后修权限。
    # 函数用途: 验证适配器首次准备收发目录时全部按私有模式创建。
    def test_adapter_dirs_private(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.gateway_parts.adapter import _ensure_adapter_dirs

        paths = _adapter_paths(tmp_path)
        _ensure_adapter_dirs(paths)
        for directory in (paths.inbox, paths.processing, paths.done, paths.failed, paths.outbox):
            _assert_private_dir(directory)


# LLM: task_progress 私有写回归须保持进度合并/回读与替换失败原文不变，所有状态写入仅限 tmp_path。
# 类用途: 核对进度账本的权限、原子性和失败清理。
class TestTaskProgress:
    # LLM: task_progress 回归使用真实写读入口，核 item 往返及两级新目录权限；不能只测文件生成。
    # 函数用途: 验证进度清单写成私有文件后仍能完整读回。
    def test_write_task_progress_private_and_readable(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.task_progress import (
            progress_path,
            read_task_progress,
            write_task_progress,
        )

        merged = write_task_progress(
            tmp_path, "run-1", {"items": [{"id": "p1", "title": "t", "status": "pending"}]}
        )
        assert merged["items"][0]["id"] == "p1"
        path = progress_path(tmp_path, "run-1")
        _assert_private_file(path)
        _assert_private_dir(path.parent)
        _assert_private_dir(path.parent.parent)
        again = read_task_progress(tmp_path, "run-1")
        assert again["items"][0]["id"] == "p1"

    # LLM: task_progress 回归在真实替换叶注入 OSError，必须原样抛错、保旧正文且无暂存残留；不允许吞错或放宽断言。
    # 函数用途: 验证进度更新失败不会写坏已有清单，临时文件也会清理。
    def test_atomic_replace_failure_keeps_previous_content(self, tmp_path: Path, monkeypatch) -> None:
        from agent_py_agent.agent import task_progress
        from agent_py_agent.agent.common import json_io

        task_progress.write_task_progress(
            tmp_path, "run-1", {"items": [{"id": "p1", "title": "t", "status": "pending"}]}
        )
        path = task_progress.progress_path(tmp_path, "run-1")
        before = path.read_text(encoding="utf-8")

        # LLM: task_progress 故障替身仅在真实 replace 调用点抛错，不执行写入；签名须随 json_io 原替换原语同步。
        # 函数用途: 模拟原子替换失败，触发产品的保旧文件与清理路径。
        def _fail_replace(tmp, target, *, keep_mode: bool = True) -> None:
            raise OSError("replace failed")

        monkeypatch.setattr(json_io, "_replace_with_retry", _fail_replace)
        with pytest.raises(OSError):
            task_progress.write_task_progress(
                tmp_path, "run-1", {"items": [{"id": "p1", "title": "t", "status": "done"}]}
            )
        assert path.read_text(encoding="utf-8") == before
        leftovers = [item.name for item in path.parent.iterdir() if item.name.startswith(".")]
        assert leftovers == []


# LLM: session 私有写回归区分新建目录与存量目录，既保 0600/0700 又禁止擅自改变用户已有目录模式。
# 类用途: 验证会话新建、读取和会话根权限处理。
class TestSessionManager:
    # LLM: session 回归调用真实 SessionManager 创建并加载会话，要求私有文件/目录及相同 session_id，不接真实用户会话。
    # 函数用途: 验证新会话落盘私有且能正常重新加载。
    def test_create_session_private_and_readable(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.session.manager import SessionManager

        manager = SessionManager(
            SimpleNamespace(session_workspace=str(tmp_path / "sessions"), user_id="u1")
        )
        session = manager.create_session()
        session_path = tmp_path / "sessions" / session.session_id / "session.json"
        _assert_private_file(session_path)
        _assert_private_dir(session_path.parent)
        loaded = manager.load_session(session.session_id)
        assert loaded is not None and loaded.session_id == session.session_id

    # LLM: session 回归临时设置宽松 umask 后由产品创建根，仍须 0700；全局 umask 必须经 helper 恢复。
    # 函数用途: 证明新会话根的私有权限来自显式模式，而非测试环境碰巧收紧。
    def test_session_root_created_private_under_umask_022(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.session.manager import SessionManager

        # umask 022 下仍必须是 0700：证明权限来自显式 mode，而不是跟着调用方的 umask 走。
        root = tmp_path / "sessions"
        with _umask(0o022):
            SessionManager(SimpleNamespace(session_workspace=str(root), user_id="u1"))
        _assert_private_dir(root)

    # LLM: session 回归先造 0755 存量根再初始化，必须逐位保持，不能为统一权限擅改用户已有目录。
    # 函数用途: 验证初始化会话管理器不会修改既有会话根权限。
    def test_session_root_existing_permissions_unchanged(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.session.manager import SessionManager

        # 已存在的目录一位都不动：只动自己新建的东西。
        root = tmp_path / "sessions"
        root.mkdir()
        os.chmod(root, 0o755)
        SessionManager(SimpleNamespace(session_workspace=str(root), user_id="u1"))
        assert _mode(root) == 0o755, f"existing dir mode changed to {oct(_mode(root))}"


# LLM: 跨通道会话回归只保存本地通道映射，不外发消息；新建私有、存量根不变两项合同必须同时保留。
# 类用途: 核对 channels 文件往返和跨通道会话目录权限。
class TestCrossChannel:
    # LLM: 跨通道回归由真实保存口写 channels，核 0600/0700 和原映射往返，不以模拟 JSON 代产品写入。
    # 函数用途: 验证通道状态映射以私有文件保存且内容不变。
    def test_save_channels_private_and_readable(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        store = CrossChannelSession(SimpleNamespace(session_workspace=str(tmp_path / "sessions")))
        store._save_channels("sess1", {"telegram": {"active": True}})
        path = tmp_path / "sessions" / "sess1" / "channels.json"
        _assert_private_file(path)
        _assert_private_dir(path.parent)
        assert json.loads(path.read_text(encoding="utf-8")) == {"telegram": {"active": True}}

    # LLM: 跨通道回归在 umask 022 下让产品自行建根，仍须 0700；退出测试恢复进程 umask。
    # 函数用途: 验证跨通道会话的新根目录不依赖外部默认权限。
    def test_channel_root_created_private_under_umask_022(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        root = tmp_path / "sessions"
        with _umask(0o022):
            CrossChannelSession(SimpleNamespace(session_workspace=str(root)))
        _assert_private_dir(root)

    # LLM: 跨通道回归固定已有 0755 根，初始化不能 chmod；与新建根用例成对保护“不改存量目录”的边界。
    # 函数用途: 验证跨通道初始化尊重现有目录权限。
    def test_channel_root_existing_permissions_unchanged(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        root = tmp_path / "sessions"
        root.mkdir()
        os.chmod(root, 0o755)
        CrossChannelSession(SimpleNamespace(session_workspace=str(root)))
        assert _mode(root) == 0o755, f"existing dir mode changed to {oct(_mode(root))}"


# LLM: 会话任务摘要回归验证投影更新同时收紧文件，不把摘要正文当运行权威；所有写入限临时任务目录。
# 类用途: 核对摘要状态同步的私有写入和文本兼容。
class TestStoreTasksSummary:
    # LLM: 会话摘要回归以 0644 旧文本触发真实同步，核状态/步骤两处更新和 0600；不能只检查最终存在。
    # 函数用途: 验证任务摘要更新为运行态时仍完整且私有。
    def test_summary_status_sync_private(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.conversation.store_tasks import (
            _sync_task_workspace_summary_status,
        )

        task_root = tmp_path / "task"
        task_root.mkdir()
        summary = task_root / "current_summary.md"
        summary.write_text("# T\n- status: PENDING\n- current_step: PENDING\n", encoding="utf-8")
        os.chmod(summary, 0o644)
        _sync_task_workspace_summary_status(task_root, summary, {"status": "running"})
        _assert_private_file(summary)
        text = summary.read_text(encoding="utf-8")
        assert "- status: RUNNING" in text
        assert "- current_step: RUNNING" in text


# LLM: Gateway 消息修复记录含正文，回归须由产品创建缺失目录并核内容往返；不连接真实消息通道。
# 类用途: 验证待补交会话消息以私有记录排队保存。
class TestMessageRepairs:
    # LLM: Gateway 回归使用临时 storage 与最小 agent，核 message_repairs 新目录 0700、文件 0600 及补交正文不丢。
    # 函数用途: 验证消息修复队列不会把待补交的正文写成公开文件。
    def test_queue_gateway_conversation_repair_private(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.gateway_parts.request_history import (
            _queue_gateway_conversation_repair,
        )

        # message_repairs 含消息正文；落盘必须私有：目录缺失按 0700 建、文件出生 0600。
        # 刻意不预建 message_repairs 目录，验证缺失目录也由私有写建出。
        root = tmp_path / "storage"
        root.mkdir()
        agent = SimpleNamespace(
            conversation_store=SimpleNamespace(storage=SimpleNamespace(root=str(root))),
            _conversation_indexed_threads=set(),
        )
        conversation = SimpleNamespace(thread_id="thread-1")
        _queue_gateway_conversation_repair(
            agent, {}, conversation, request_id="gw-1", role="assistant", content="补交正文",
        )
        path = root / "message_repairs" / "gw-1-assistant.json"
        _assert_private_dir(path.parent)
        _assert_private_file(path)
        assert json.loads(path.read_text(encoding="utf-8"))["content"] == "补交正文"


# LLM: home 备份回归只在 tmp_path 建隔离 home 并生成清单，不备份真实用户数据或执行恢复。
# 类用途: 核对备份清单的私有权限与原因字段。
class TestHomeBackup:
    # LLM: home 备份回归经真实 manifest 写口，要求清单 0600 且 reason 往返，不将清单存在视作完整备份验收。
    # 函数用途: 验证新备份清单只供当前用户读取，且记录创建原因。
    def test_manifest_private(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.user_space.home_backup import create_home_backup_manifest
        from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

        home = ensure_my_agent_home(tmp_path)
        manifest = create_home_backup_manifest(home, reason="pw3 test")
        _assert_private_file(manifest.manifest_path)
        payload = json.loads(manifest.manifest_path.read_text(encoding="utf-8"))
        assert payload["reason"] == "pw3 test"


# LLM: LocalStore 私有写回归在临时数据库运行真实 upsert，重点核正文旁路文件权限，不接外部存储或网络。
# 类用途: 核对本地记录的正文文件按私有模式写入并可读。
class TestLocalStorageRecords:
    # LLM: LocalStore 回归使用真实记录输入，要求旁路正文文件精确 0600 且 UTF-8 内容原样；数据库成功不能替代文件断言。
    # 函数用途: 验证保存一条记录时正文不会被写成其他系统用户可读的文件。
    def test_upsert_content_file_private_and_readable(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.local_storage import LocalStore
        from agent_py_agent.agent.local_storage.records import LocalRecordInput

        store = LocalStore(tmp_path / "data" / "local.db", enable_fts=False)
        store.upsert_record(
            LocalRecordInput(source_type="note", source_id="n1", title="t", content="正文内容", record_id="n1")
        )
        content_path = store.files_dir / "n1.txt"
        _assert_private_file(content_path)
        assert content_path.read_text(encoding="utf-8") == "正文内容"
