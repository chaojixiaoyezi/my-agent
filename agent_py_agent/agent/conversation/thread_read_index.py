"""线程列表的可重建定位投影；原 JSON 和继任发表条件始终是唯一事实源。"""
from __future__ import annotations

from pathlib import Path

from .session_list_index import SessionListIndex, _stamp


def _workspace(value):
    return str(Path(value).expanduser().resolve(strict=False)) if value else ""


class ThreadReadIndex(SessionListIndex):
    """复用列表投影的私有数据库、目录续点和写后发布，不另存线程正文。"""

    def __init__(self, threads):
        self.threads = threads
        super().__init__(threads.storage.threads_dir, kind="thread", reader=self._read)

    def _oversize(self, path):
        try:
            size = path.lstat().st_size
        except FileNotFoundError:
            return None
        if size > 1024 * 1024:
            return {"error_code": "THREAD_INDEX_RECORD_TOO_LARGE", "thread_id": path.stem,
                    "context": "conversation.thread.list", "side_effects": "not_started",
                    "message": "线程元数据超过列表单条1MiB读取预算；请用准确thread_id检查记录，列表不会读取超预算正文。"}
        return None

    def _read(self, path):
        from ..common.opaque_id import validate_opaque_id
        from .store_threads import _clone_cached_thread

        try:
            validate_opaque_id(path.stem, kind="thread_id")
        except ValueError:
            return {}, None
        error = self._oversize(path)
        if error:
            return {}, error
        thread, error = self.threads._load_path_report(path, clone=False)
        if error or thread is None:
            return {}, error
        payload = _clone_cached_thread(thread).to_dict()
        payload["workspace_root"] = _workspace(thread.workspace_root)
        return payload, None

    def _filter_field(self):
        return "workspace_root"

    def report(self, *, limit, workspace_dir=None):
        """一次最多回填128目录项和读取limit个候选；未追平显式返回partial。"""
        from .store_threads import _clone_cached_thread

        key = _workspace(workspace_dir) if workspace_dir is not None else None
        with self._connection() as conn:
            complete = self._refresh(conn)
            where, args = ("", ()) if key is None else (" WHERE filter_key=?", (key,))
            candidates = conn.execute("SELECT path,stamp FROM entries" + where
                + " ORDER BY sort_at DESC,path DESC LIMIT ?", (*args, limit)).fetchall()
            result, errors = [], []
            for row in candidates:
                path = self.root / row["path"]
                if _stamp(path) != row["stamp"]:
                    self._record(conn, path, int(self._meta(conn, "generation", "0")))
                    complete = False
                error = self._oversize(path)
                thread = None
                if error is None:
                    thread, error = self.threads._load_path_report(path, clone=False)
                if error:
                    errors.append(error)
                elif thread is not None and (key is None or _workspace(thread.workspace_root) == key):
                    result.append(_clone_cached_thread(thread))
            if not complete:
                errors.append({"error_code": "THREAD_INDEX_PARTIAL", "context": "conversation.thread.list",
                    "side_effects": "projection_only", "retryable": True,
                    "message": "线程读索引尚未追平；使用线程索引后台维护继续回填，完整导出显式调用limit=0。"})
            result.sort(key=lambda item: (item.updated_at, item.thread_id))
            return result, errors

    def maintain(self, *, budget=None):
        """后台每轮只推进现有目录页；调用者负责调度抖动和轮次预算。"""
        if budget is not None and budget.exhausted():
            return {"coverage": "partial", "side_effects": "not_started"}
        with self._connection(blocking=budget is None) as conn:
            complete = self._refresh(conn, budget=budget)
        return {"coverage": "complete" if complete else "partial", "side_effects": "projection_only"}


def publish_thread(threads, path):
    """写后重读canonical；投影失败不把已成功业务写入伪装成未执行。"""
    import logging
    import sqlite3

    try:
        ThreadReadIndex(threads).publish(path)
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        logging.getLogger(__name__).warning("THREAD_INDEX_PUBLISH_FAILED canonical_committed=true type=%s reason=%s",
                                           type(exc).__name__, str(exc))


class _LaneBudget:
    """把原不可变通道配额映射为本次累计计量，截止时间保持调用者原值。"""

    def __init__(self, original):
        self.original = original
        self.max_entries = original.max_items if original.max_items is not None else 128
        self.entries = self.size = 0

    def charge(self, *, entries=0, size=0):
        self.entries += entries
        self.size += size

    def exhausted(self):
        return self.original.exhausted(self.entries, self.size)


def maintain_thread_index_lane(request):
    """每轮推进一个canonical会话根；工作区发现也使用同一共享预算和续点。"""
    import sqlite3

    from ..user_space.directory_paging import directory_page
    from .store_layout import ConversationStorage
    from .store_threads import ThreadStore

    state = dict(request.cursor)
    budget = _LaneBudget(request.budget)
    if budget.exhausted():
        return {"cursor": state, "complete": False, "items": 0, "bytes": 0}
    owner = Path(request.home.owner_home_dir)
    root = state.get("root")
    if root is None and not state.get("direct_done"):
        root = "conversations"
        state["direct_done"] = True
    configured = getattr(getattr(request, "config", None), "conversation_workspace", "")
    if root is None and configured and not state.get("configured_done"):
        try:
            root = str(Path(configured).expanduser().absolute().relative_to(owner.absolute()))
        except ValueError:
            return {"cursor": state, "complete": False, "items": budget.entries, "bytes": budget.size,
                    "errors": [{"code": "THREAD_INDEX_CONFIG_SCOPE_MISMATCH",
                                "message": "conversation_workspace不在当前owner目录内；请核对该owner的路径配置。",
                                "side_effects": "not_started"}]}
        state["configured_done"] = True
    if root is None:
        workspace = Path(request.home.owner_workspace_dir).relative_to(owner)
        parts = (*workspace.parts, "runtime", "workspaces")
        names, cursor, done = directory_page(owner, parts, state.get("scopes", {}), 1,
                                             require_dir_fd=False)
        state["scopes"] = cursor
        budget.charge(entries=len(names))
        if names:
            root = str(Path(*parts, names[0], "conversations"))
        elif done:
            return {"cursor": {}, "complete": True, "items": budget.entries, "bytes": budget.size}
    if root is not None and not budget.exhausted():
        # 相对路径来源只有本模块的规范发现器；恢复游标不授予越界或链接目录读取权。
        import os

        from ..common.nofollow_fs import open_directory_beneath
        descriptor = None
        try:
            descriptor = open_directory_beneath(owner, Path(root).parts, require_dir_fd=False)
        except (FileNotFoundError, NotADirectoryError):
            state.pop("root", None)
        else:
            os.close(descriptor)
            threads = ThreadStore(ConversationStorage(owner / root, initialize=False))
            state["root"] = root
            try:
                outcome = request.run_write(lambda: ThreadReadIndex(threads).maintain(budget=budget))
            except (OSError, ValueError, sqlite3.Error) as exc:
                return {"cursor": state, "complete": False, "items": budget.entries, "bytes": budget.size,
                        "errors": [{"code": "THREAD_INDEX_MAINTENANCE_DEFERRED", "error_type": type(exc).__name__,
                                    "message": str(exc), "side_effects": "projection_only",
                                    "next_action": "保留游标，下一维护片重试；不修改canonical线程。"}]}
            if outcome["coverage"] == "complete":
                state.pop("root", None)
            else:
                state["root"] = root
    elif root is not None:
        state["root"] = root
    return {"cursor": state, "complete": False, "items": budget.entries, "bytes": budget.size}


def scheduled_thread_terminal_proof(home, thread, cutoff, *, max_bytes=1048576, deadline=None):
    """准确原调度历史行只证明终态；引用、活跃claim/wake及正式历史仍归原留存门。"""
    from types import SimpleNamespace

    from ..scheduler.history_index import SchedulerHistoryIndex
    from ..scheduler.repository import _SchedulerRecordParsing

    run_id = thread.metadata.get("scheduler_run_id")
    unknown = {"status": "unknown", "reason": "SCHEDULER_THREAD_IDENTITY_INVALID", "side_effects": "not_started"}
    if (thread.metadata.get("thread_kind") != "scheduled" or not isinstance(run_id, str)
            or thread.thread_id != "thread-" + run_id or thread.owner_id != home.owner_id
            or thread.owner_home != str(home.owner_home_dir) or thread.channel_bindings or thread.active_task_ids):
        return unknown
    owner = {"provider": home.owner_provider, "kind": home.owner_kind, "id": home.owner_id}
    context = SimpleNamespace(owner=owner)
    def parse(payload, *, terminal=False):
        return _SchedulerRecordParsing._parse_run(context, payload, terminal=terminal)
    history = Path(home.owner_scheduler_history_jsonl)
    try:
        history.relative_to(Path(home.owner_home_dir))
    except ValueError:
        return unknown
    index = SchedulerHistoryIndex(history, owner, parse, read_only=True)
    proof = index.terminal_run_proof(run_id, thread_id=thread.thread_id, cutoff=cutoff, max_bytes=max_bytes, deadline=deadline)
    if proof.get("status") == "settled" and proof.get("execution_mode") != "isolated":
        return {**unknown, "reason": "SCHEDULER_THREAD_EXECUTION_NOT_ISOLATED"}
    return proof
