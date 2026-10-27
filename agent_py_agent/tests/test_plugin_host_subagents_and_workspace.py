# LLM: W5只验宿主内部handler合同；真实授权/动作注册由W4/W6负责，不冒充HTTP或生产进程验收。
# 模块用途: 用真实临时文件、Git、canonical子代理及进程账守住隔离、CAS和有界回执。
from __future__ import annotations

import base64
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.path_access_policy import HOST_STATE_OWNER_DIRS
from agent_py_agent.agent.plugin_host import capabilities_v2_handlers as caps
from agent_py_agent.agent.plugin_host import subagents_v2_handlers as sub
from agent_py_agent.agent.plugin_host import workspace_v2_handlers as files
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.settings import model_profiles
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    process_session_store_root,
)
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    ensure_owner_home,
    home_paths_with_owner,
)


# LLM: 所有状态均在pytest临时目录；native manager非模型替身，整个测试不发网络请求。
# 函数用途: 建立两个互相隔离的owner和真实原子代理/会话存储。
@pytest.fixture
def case(tmp_path):
    home = ensure_owner_home(tmp_path, OwnerIdentity.local_user("alice"))
    other = ensure_owner_home(tmp_path, OwnerIdentity.local_user("bob"))
    context = SimpleNamespace(owner_home=home, identity=home.identity)
    workspace = home.home_dir / "workspace" / "runtime" / "workspaces" / "project" / "subagents"
    manager = SubAgentManager(workspace, owner_id=home.owner_id, owner_home_dir=str(home.home_dir))
    store = ConversationStore(home.data_dir / "conversations")
    agent = SimpleNamespace(subagents=manager, conversation_store=store,
                            home_paths=SimpleNamespace(owner_home_dir=home.home_dir))
    task = manager.create_run(goal="核对文件", owner=home.owner_id,
                              attributes={"conversation_thread_id": "thread-one"})
    sibling = manager.create_run(goal="兄弟任务", owner=home.owner_id,
                                 attributes={"conversation_thread_id": "thread-two"})
    return SimpleNamespace(context=context, home=home, other=other, manager=manager, agent=agent,
                           store=store, task=task, sibling=sibling, root=home.home_dir)


# LLM: 列表和详情读取真实canonical记录，不允许按线程过滤失败后退回整个owner。
# 函数用途: 检查冷读发现、字段投影、空范围和终态过滤。
def test_subagents_list_get_and_cold_discovery(case):
    payload = {"thread_id": "thread-one"}
    listed = sub.list_subagents(case.context, payload)
    assert isinstance(listed, dict)
    rows = listed["items"]
    assert [row["subagent_id"] for row in rows] == [case.task.id]
    assert {"run_id", "parent_run_id", "thread_id", "name", "role", "model_name", "reasoning_effort",
            "status", "current_action", "started_at", "elapsed_seconds", "revision"} <= rows[0].keys()
    detail = sub.get_subagent(case.context, {**payload, "subagent_id": case.task.id}, agent=case.agent, store=case.store)
    assert detail["instruction"] == "核对文件"
    assert {"steps", "messages", "output_summary"} <= detail.keys()
    assert sub.list_subagents(case.context, {"thread_id": "absent"})["items"] == []
    case.manager.mutate(case.task.id, lambda task: setattr(task, "status", "DONE"))
    assert sub.list_subagents(case.context, {**payload, "include_finished": False})["items"] == []


# LLM: 即使同一存储存在跨owner/线程记录，也必须先拒绝再投影正文。
# 函数用途: 检查详情和写动作拒绝不匹配归属及非法ID。
@pytest.mark.parametrize("action", ["get_subagent", "steer_subagent", "stop_subagent"])
@pytest.mark.parametrize("wrong", ["thread", "owner", "path"])
def test_subagent_scope_rejects_before_effect(case, action, wrong):
    payload = {"subagent_id": "../escape" if wrong == "path" else case.task.id, "message": "new"}
    if wrong == "thread":
        payload["thread_id"] = "thread-two"
    if wrong == "owner":
        case.manager.mutate(case.task.id, lambda task: setattr(task, "owner", case.other.owner_id))
    before = case.manager.load(case.task.id).state_revision
    with pytest.raises(HostAPIError):
        getattr(sub, action)(case.context, payload, agent=case.agent, store=case.store)
    assert case.manager.load(case.task.id).state_revision == before
    assert case.store.guidance.pending("agent_run", case.task.id) == []


# LLM: 接受插话只证明持久入队；不伪造送达或子代理执行完成。
# 函数用途: 验证真实GuidanceStore写入和原canonical修订CAS。
def test_steer_uses_native_guidance_and_revision_cas(case):
    revision = case.manager.load(case.task.id).state_revision
    payload = {"subagent_id": case.task.id, "text": "先检查边界", "expected_revision": revision}
    result = sub.steer_subagent(case.context, payload, agent=case.agent, store=case.store)
    assert isinstance(result, dict)
    assert result["steered"] is True and result["revision"] > revision
    pending = case.store.guidance.pending("agent_run", case.task.id)
    assert len(pending) == 1 and pending[0].message == "先检查边界"
    assert pending[0].delivery == "interrupt"
    with pytest.raises(HostAPIError) as stale:
        sub.steer_subagent(case.context, payload, agent=case.agent, store=case.store)
    assert stale.value.code == "SUBAGENT_REVISION_CONFLICT"
    assert len(case.store.guidance.pending("agent_run", case.task.id)) == 1


# LLM: 原取消服务单点冻结并在锁外清理；不取消父会话、兄弟或后代。
# 函数用途: 检查真实状态机取消目标且保留兄弟原状态。
def test_stop_only_selected_subagent(case):
    revision = case.manager.load(case.task.id).state_revision
    payload = {"subagent_id": case.task.id, "expected_revision": revision}
    result = sub.stop_subagent(case.context, payload, agent=case.agent, store=case.store)
    assert isinstance(result, dict)
    assert result["stopped"] is True and result["status"] == "cancelled"
    assert case.manager.load(case.task.id).status == "CANCELLED"
    assert case.manager.load(case.sibling.id).status == case.sibling.status
    with pytest.raises(HostAPIError):
        sub.stop_subagent(case.context, payload, agent=case.agent, store=case.store)


# LLM: 参数路径仍须逐次nofollow，合法二进制扩展名不授越界权限。
# 函数用途: 检查通用上传下载保留完整字节及中文文本行范围。
@pytest.mark.parametrize("extension", ["docx", "xlsx", "pdf", "png", "md"])
def test_upload_download_and_preview(case, extension):
    data = b"\x00\xffbinary-report\x01" if extension != "md" else "一\n二\n三\n".encode()
    payload = {"workspace_dir": str(case.root), "filename": f"report.{extension}",
               "data_base64": base64.b64encode(data).decode()}
    uploaded = files.upload_workspace_file(case.context, payload)
    assert isinstance(uploaded, dict)
    assert uploaded["path"] == f"uploads/report.{extension}"
    selected = {"workspace_dir": str(case.root), "path": uploaded["path"]}
    assert base64.b64decode(files.download_workspace_file(case.context, selected)["data_base64"]) == data
    preview = files.read_workspace_file(case.context, {**selected, "start_line": 2, "end_line": 2})
    assert preview["is_binary"] is (extension != "md")
    if extension == "md":
        assert preview["content"] == "二\n" and preview["total_lines"] == 3
    else:
        assert base64.b64decode(preview["content"]) == data


# LLM: 固定目录FD逐项lstat，不跟随链接；列表限额和查询必须真的限制输出。
# 函数用途: 验证目录列表、UTF8截断及未知总行数不被编成零。
def test_files_listing_and_bounded_read(case):
    (case.root / "a.txt").write_text("中文\n" * 100)
    (case.root / "b.txt").write_text("b")
    (case.root / ".git").mkdir()
    listed = files.list_workspace_files(case.context, {"workspace_dir": str(case.root), "query": ".txt", "limit": 1})
    assert isinstance(listed, dict)
    assert len(listed["items"]) == 1 and listed["items"][0]["size_bytes"] > 0
    result = files.read_workspace_file(case.context, {"workspace_dir": str(case.root), "path": "a.txt", "max_bytes": 5})
    assert result["truncated"] is True and result["is_binary"] is False
    assert result["total_lines"] is None


# LLM: 同时检查叶链接、父链接、绝对路径、点点和跨owner根；写拒绝后外部文件必须原样。
# 函数用途: 防止文件操作逃离项目根。
@pytest.mark.parametrize("action", ["read_workspace_file", "download_workspace_file", "upload_workspace_file"])
@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", "link/secret", "leaf"])
def test_file_escape_rejected(case, action, path):
    (case.other.home_dir / "secret").write_bytes(b"unchanged")
    (case.root / "link").symlink_to(case.other.home_dir, target_is_directory=True)
    (case.root / "leaf").symlink_to(case.other.home_dir / "secret")
    (case.root / "uploads").symlink_to(case.root, target_is_directory=True)
    payload = {"workspace_dir": str(case.root), "path": path, "data_base64": "eA=="}
    with pytest.raises(HostAPIError):
        getattr(files, action)(case.context, payload)
    assert (case.other.home_dir / "secret").read_bytes() == b"unchanged"


# LLM: 外部目录未经管理员目录门，不接受仅因文件存在就读取。
# 函数用途: 检查跨owner目录和上传尺寸/编码非法输入。
def test_foreign_root_and_bad_uploads(case):
    with pytest.raises(HostAPIError):
        files.list_workspace_files(case.context, {"workspace_dir": str(case.other.home_dir)})
    for value in ("!not-base64!", "A" * (28 * 1024 * 1024 + 8)):
        with pytest.raises(HostAPIError):
            files.upload_workspace_file(case.context, {"workspace_dir": str(case.root), "filename": "bad.bin", "data_base64": value})
    assert not (case.root / "uploads" / "bad.bin").exists()


# LLM: 环境禁止新写.git/config，且Host禁止绑定自身代码目录；只测原生只读Git执行口，不冒充目录授权E2E。
# 函数用途: 以当前授权工作树对照真实Git输出，不改配置和索引。
def test_native_git_readonly_helper_with_actual_repository():
    root = Path(__file__).resolve().parents[2]
    path = "agent_py_agent/agent/plugin_host/capabilities_v2_handlers.py"
    expected = subprocess.run(["git", "diff", "--no-ext-diff", "--no-textconv", "--", path],
                              cwd=root, check=True, capture_output=True).stdout.decode()
    descriptor = os.open(root, os.O_RDONLY)
    try:
        diff, truncated = files._git(descriptor, ["diff", "--no-ext-diff", "--no-textconv", "--", path])
    finally:
        os.close(descriptor)
    assert diff == expected and not truncated


# LLM: 仅用Git输出替身检查授权目录内的投影；不声称临时目录是真实仓库。
# 函数用途: 检查分支、重命名及带换行文件名的NUL解析。
def test_git_projection_of_authorized_workspace(case, monkeypatch):
    monkeypatch.setattr(files, "_stat", lambda *args, **kwargs: SimpleNamespace(st_mode=0o040700))
    outputs = iter([("## main...origin/main\x00 M with\nnewline.txt\x00R  new.txt\x00old.txt\x00", False), ("+changed\n", False)])
    monkeypatch.setattr(files, "_git", lambda *args: next(outputs))
    result = files.inspect_workspace_git(case.context, {"workspace_dir": str(case.root)})
    assert result == {"branch": "main", "clean": False,
                      "changed_files": [{"status": "M", "path": "with\nnewline.txt"}, {"status": "R", "path": "new.txt"}],
                      "diff": "+changed\n", "truncated": False}


# LLM: 不启动进程；使用v2原Store及原cleanup结清已确认未启动的资源，不伪造PID终止。
# 函数用途: 验证进程停止归属与准确停止确认字段。
def test_background_stop_uses_real_owner_store(case):
    root = process_session_store_root(case.root, case.root)
    store = ProcessSessionStore(root)
    record = {"schema": "managed_process_session.v2", "session_id": "bg-unit-one", "revision": 0,
              "access_scope": {"owner_id": case.home.owner_id, "owner_home": str(case.root), "conversation_id": "thread-one"},
              "execution_scope": {"owner_home": str(case.root), "thread_id": "thread-one", "root_task_id": "root", "run_id": "run", "attempt_id": "attempt"},
              "completion_target": {}, "launcher_pid": os.getpid(), "launcher_birth_token": "unit-not-signalled",
              "pid": 0, "pid_birth_token": "", "child_pid": 0, "child_pid_birth_token": "",
              "stop_requested": False, "handoff_confirmed": False, "child_launch_started": False,
              "reserved_at": 1.0, "started_at": 0.0, "finished_at": 2.0, "status": "not_started", "exit_code": None,
              "command": "unit", "cwd": str(case.root), "output_file": "", "host_state_file": "", "completion_notice_id": "",
              "termination": {"confirmed": True}}
    store.write(record)
    result = sub.stop_background_process(case.context, {"process_id": record["session_id"]})
    assert result == {"session_id": record["session_id"], "stopped": True, "status": "not_started", "confirmed": True}
    assert store.load(record["session_id"]).record["stop_requested"] is True
    foreign = SimpleNamespace(owner_home=case.other, identity=case.other.identity)
    with pytest.raises(HostAPIError):
        sub.stop_background_process(foreign, {"session_id": record["session_id"]})


# LLM: 缺方法属于实际合同缺口；身份委派必须仍调用鲜活read_context，不能用payload伪造owner。
# 函数用途: 检查application_scope完整投影和原身份门委派。
def test_package_projection_and_identity_delegate(monkeypatch):
    projection = getattr(caps, "_package_dict", None)
    assert callable(projection)
    cap = SimpleNamespace(application_scope={"app": "coding"})
    record = SimpleNamespace(capability=cap, package_id="pkg", name="Package", enabled=True)
    result = projection(record)
    assert result["application_scope"] == {"app": "coding"}
    result["application_scope"]["app"] = "changed"
    assert cap.application_scope == {"app": "coding"}
    context, authorized = object(), object()
    monkeypatch.setattr(caps, "read_context", lambda value: context if value is authorized else None)
    assert caps.user_space_identity_resolve(authorized) is context


# LLM: Git输出替身仅隔离进程层；链接必须在调用后端前失败，不让“git自己报错”冒充路径保护。
# 函数用途: 防止Git路径中的父/叶链接逃到另一个owner。
@pytest.mark.parametrize("path", ["shortcut", "shortcut/secret"])
def test_git_rejects_symlink_path_before_backend(case, monkeypatch, path):
    monkeypatch.setattr(files, "_stat", lambda *args, **kwargs: SimpleNamespace(st_mode=0o040700))
    (case.other.home_dir / "secret").write_text("outside")
    (case.root / "shortcut").symlink_to(case.other.home_dir)
    calls = []
    monkeypatch.setattr(files, "_git", lambda *args: calls.append(args) or ("## main\x00", False))
    with pytest.raises(HostAPIError):
        files.inspect_workspace_git(case.context, {"workspace_dir": str(case.root), "path": path})
    assert calls == []


# LLM: 模拟原子写已rename后fsync失败，原始错误不等于未写。
# 函数用途: 上传部分生效必须明确unknown。
def test_upload_post_commit_error_stays_unknown(case, monkeypatch):
    original = files.write_bytes_atomic_beneath
    # LLM: 只写当前pytest目录，故障注入位于真实原子写完成后。
    # 函数用途: 复现提交后的存储错误。
    def committed(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("post-commit")
    monkeypatch.setattr(files, "write_bytes_atomic_beneath", committed)
    with pytest.raises(HostAPIError) as error:
        files.upload_workspace_file(case.context, {"workspace_dir": str(case.root), "filename": "accepted", "data_base64": "eA=="})
    assert (case.root / "uploads" / "accepted").read_bytes() == b"x"
    assert error.value.outcome == "unknown"


# LLM: canonical取消和进程清理均已成功后，最后只读失败不能变成确定未执行。
# 函数用途: 复现评审发现的停止后回执读取失败。
def test_stop_post_effect_read_error_stays_unknown(case, monkeypatch):
    original = sub.cleanup_subagent_tree
    # LLM: 保留真实native清理后才替换最后一次load，不影响前面的权限核验。
    # 函数用途: 注入最后读取失败。
    def cleaned(*args):
        result = original(*args)
        monkeypatch.setattr(case.manager, "load", lambda *args: (_ for _ in ()).throw(OSError("after-stop")))
        return result
    monkeypatch.setattr(sub, "cleanup_subagent_tree", cleaned)
    with pytest.raises(HostAPIError) as error:
        sub.stop_subagent(case.context, {"subagent_id": case.task.id}, agent=case.agent, store=case.store)
    assert error.value.outcome == "unknown"


# LLM: 20MiB是原字节上限，不是编码后字符数；上传/下载必须回完整文件。
# 函数用途: 检查精确20MiB允许，20MiB加一字节拒绝。
def test_upload_download_exact_twenty_mib_limit(case):
    data = b"x" * (20 * 1024 * 1024)
    payload = {"workspace_dir": str(case.root), "filename": "large.pdf", "data_base64": base64.b64encode(data).decode()}
    assert files.upload_workspace_file(case.context, payload)["size_bytes"] == len(data)
    downloaded = files.download_workspace_file(case.context, {"workspace_dir": str(case.root), "path": "uploads/large.pdf"})
    assert base64.b64decode(downloaded["data_base64"]) == data
    payload["data_base64"] = base64.b64encode(data + b"x").decode()
    with pytest.raises(HostAPIError) as error:
        files.upload_workspace_file(case.context, payload)
    assert error.value.status == 413
    assert (case.root / "uploads" / "large.pdf").stat().st_size == len(data)


# LLM: 只读Git同样不能沿.git根链接或外部gitdir指针读另一个owner的对象库。
# 函数用途: 检查不安全Git元数据在运行前拒绝。
@pytest.mark.parametrize("kind", ["symlink", "gitfile"])
def test_git_metadata_escape_rejected(case, monkeypatch, kind):
    # 环境禁止创建.git文件/链接，这里只验证lstat结果分支，不声称真实恶意仓库端到端验证。
    mode = 0o120777 if kind == "symlink" else 0o100600
    monkeypatch.setattr(files, "_stat", lambda *args, **kwargs: SimpleNamespace(st_mode=mode))
    calls = []
    monkeypatch.setattr(files, "_git", lambda *args: calls.append(args) or ("## main\x00", False))
    with pytest.raises(HostAPIError):
        files.inspect_workspace_git(case.context, {"workspace_dir": str(case.root)})
    assert calls == []


# LLM: 进程替身只测试适配器预算和退出回执，不冒充真实慢Git测试。
# 函数用途: 超时必须终止固定子进程并返回结构化504。
def test_git_timeout_stops_child_and_returns_typed_error(monkeypatch):
    from unittest.mock import MagicMock
    process = MagicMock()
    process.__enter__.return_value = process
    process.poll.return_value = None
    calls = []
    monkeypatch.setattr(files.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)) or process)
    monkeypatch.setattr(files.select, "select", lambda *args: ([], [], []))
    with pytest.raises(HostAPIError) as error:
        files._git(1, ["status"])
    assert error.value.status == 504
    process.kill.assert_called_once()
    process.wait.assert_called_once()
    assert "--attr-source=4b825dc642cb6eb9a060e54bf8d69288fbee4904" in calls[0][0][0]
    assert calls[0][1]["env"]["GIT_ATTR_NOSYSTEM"] == "1"
    assert calls[0][1]["env"]["GIT_NO_LAZY_FETCH"] == "1"


# LLM: 使用原ThreadStore创建同owner线程，显式绑定canonical任务，不替换消息解析和路径保护。
# 函数用途: 准备需要读取消息的真实子代理详情。
@pytest.fixture
def message_thread(case):
    thread = case.store.threads.create(owner_id=case.home.owner_id, owner_home=str(case.root))
    case.manager.mutate(case.task.id, lambda task: setattr(task, "agent_thread_id", thread.thread_id))
    return thread


# LLM: 外部消息是可解析的真实JSONL；叶/父链接及直接外部路径均须在原消息后端调用前拒绝。
# 函数用途: 复现F1跨owner消息文件读取并守住前置检查顺序。
@pytest.mark.parametrize("kind", ["leaf", "parent", "outside"])
def test_r2_messages_reject_foreign_path_before_reader(case, message_thread, monkeypatch, kind):
    path = case.store.storage.message_path(message_thread.thread_id)
    foreign = case.other.home_dir / path.name
    foreign.write_text(json.dumps({"message_id": "foreign-message", "thread_id": message_thread.thread_id,
                                   "role": "assistant", "content": "foreign-private-message"}) + "\n")
    if kind == "leaf":
        path.symlink_to(foreign)
    elif kind == "parent":
        path.parent.rename(case.root / "saved-messages")
        path.parent.symlink_to(case.other.home_dir, target_is_directory=True)
    else:
        monkeypatch.setattr(case.store.storage, "message_path", lambda _: foreign)
    # 确认来源有效，避免用不可解析的假消息掩盖越界读取。
    rows, errors = case.store.messages.recent_report(message_thread.thread_id, limit=100)
    assert not errors and rows[0].content == "foreign-private-message"
    calls, original = [], case.store.messages.recent_report
    monkeypatch.setattr(case.store.messages, "recent_report", lambda *a, **kw: calls.append(a) or original(*a, **kw))
    with pytest.raises(HostAPIError) as error:
        sub.get_subagent(case.context, {"subagent_id": case.task.id}, agent=case.agent, store=case.store)
    assert error.value.code in {"SUBAGENT_NOT_FOUND", "STORE_UNAVAILABLE"}
    assert calls == []


# LLM: owner_id和owner_home分别核验，线程存储出错不可降级为无记录或继续读取消息。
# 函数用途: 复现F1线程归属错误与读取失败分支。
@pytest.mark.parametrize("kind", ["owner_id", "owner_home", "load_error"])
def test_r2_messages_reject_foreign_or_unavailable_thread(case, message_thread, monkeypatch, kind):
    if kind == "load_error":
        monkeypatch.setattr(case.store.threads, "load_report", lambda _: (None, "unavailable"))
    else:
        value = case.other.owner_id if kind == "owner_id" else str(case.other.home_dir)
        case.store.threads.update_atomic(message_thread.thread_id, lambda thread: replace(thread, **{kind: value}))
    calls = []
    monkeypatch.setattr(case.store.messages, "recent_report", lambda *a, **kw: (calls.append(a) or [], []))
    with pytest.raises(HostAPIError) as error:
        sub.get_subagent(case.context, {"subagent_id": case.task.id}, agent=case.agent, store=case.store)
    assert error.value.code == ("STORE_UNAVAILABLE" if kind == "load_error" else "SUBAGENT_NOT_FOUND")
    assert calls == []


# LLM: 缺少消息文件是合法空历史，普通文件仍沿原消息Store解析，不能把保护做成全面禁用。
# 函数用途: 验证F1正常消息与尚未产生消息的详情。
@pytest.mark.parametrize("has_message", [False, True])
def test_r2_messages_allow_owned_regular_or_missing_file(case, message_thread, has_message):
    if has_message:
        case.store.messages.append({"thread_id": message_thread.thread_id, "role": "assistant", "content": "own-message"})
    result = sub.get_subagent(case.context, {"subagent_id": case.task.id}, agent=case.agent, store=case.store)
    assert [row["content"] for row in result["messages"]] == (["own-message"] if has_message else [])


# LLM: 目录清单来自原策略，不另抄一份；相对/绝对根与读/写入口都不能把宿主控制目录当工作区。
# 函数用途: 复现F2内部工作根绕过并确认上传拒绝前不落盘。
@pytest.mark.parametrize("parts", [*HOST_STATE_OWNER_DIRS, ("runs",)])
@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("action", ["list_workspace_files", "upload_workspace_file"])
def test_r2_workspace_rejects_control_root(case, parts, absolute, action):
    root = case.root.joinpath(*parts)
    root.mkdir(parents=True, exist_ok=True)
    payload = {"workspace_dir": str(root) if absolute else "/".join(parts), "filename": "probe.txt", "data_base64": "eA=="}
    with pytest.raises(HostAPIError) as error:
        getattr(files, action)(case.context, payload)
    assert error.value.code == "WORKSPACE_PATH_INVALID"
    assert not (root / "uploads" / "probe.txt").exists()


# LLM: home根本身合法，但相对目标仍需独立拒绝控制目录；Git仅替换进程/元数据投影，不伪装仓库E2E。
# 函数用途: 复现F2从home根读、下载、列举、Git观察内部路径的绕过。
@pytest.mark.parametrize("parts", [("runs",), ("data",), ("workspace", "runtime"), ("memory", "curator")])
@pytest.mark.parametrize("action", ["read_workspace_file", "download_workspace_file", "list_workspace_files", "inspect_workspace_git"])
def test_r2_workspace_rejects_control_descendant(case, monkeypatch, parts, action):
    target = case.root.joinpath(*parts)
    target.mkdir(parents=True, exist_ok=True)
    (target / "internal.txt").write_text("private-control-content")
    monkeypatch.setattr(files, "_stat", lambda *a, **kw: SimpleNamespace(st_mode=0o040700))
    calls = []
    monkeypatch.setattr(files, "_git", lambda *a: calls.append(a) or ("## main\x00", False))
    payload = {"workspace_dir": str(case.root), "relative_path": "/".join(parts), "path": "/".join((*parts, "internal.txt"))}
    with pytest.raises(HostAPIError) as error:
        getattr(files, action)(case.context, payload)
    assert error.value.code == "WORKSPACE_PATH_INVALID"
    assert calls == []


# LLM: 列出祖先不能展示受保护直接子项，但同名前缀的普通项目不能被误杀。
# 函数用途: 检查F2列表过滤及正常项目路径不回归。
@pytest.mark.parametrize("parent, hidden, visible", [((), "runs", "runs-project"), (("workspace",), "runtime", "runtime-project")])
def test_r2_workspace_listing_hides_control_children(case, parent, hidden, visible):
    root = case.root.joinpath(*parent)
    (root / hidden).mkdir(parents=True, exist_ok=True)
    (root / visible).mkdir(parents=True, exist_ok=True)
    (root / visible / "allowed.txt").write_text("ordinary-project")
    result = files.list_workspace_files(case.context, {"workspace_dir": str(root)})
    names = {row["name"] for row in result["items"]}
    assert hidden not in names and visible in names
    preview = files.read_workspace_file(case.context, {"workspace_dir": str(root), "path": f"{visible}/allowed.txt"})
    assert preview["content"] == "ordinary-project"


# LLM: 真实时间线超过256KiB；截断点落在UTF8/长行中，返回值须恰为原文件最后100整行。
# 函数用途: 复现F3大时间线失败并验证尾读内容，不只验证没有抛错。
def test_r2_large_timeline_returns_last_hundred_complete_lines(case):
    lines = [f"{index:04d}:" + "中文进度" * 90 for index in range(600)]
    path = Path(case.manager.load(case.task.id).agent_run_timeline_jsonl)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert path.stat().st_size > 262144
    result = sub.get_subagent(case.context, {"subagent_id": case.task.id}, agent=case.agent, store=case.store)
    assert result["steps"] == lines[-100:]


# LLM: 使用原模型管理入口保存两个owner同ID但不同名称的有效档案；只有临时测试凭据，不发网络请求。
# 函数用途: 准备R3原生档案，并故意给agent不同owner的路径以验证只信认证context。
@pytest.fixture
def native_model_profile(case):
    profile_id = str(uuid4())
    for owner, name in ((case.home, "Owner-Model-R3"), (case.other, "Other-Model-R3")):
        paths = home_paths_with_owner(home_paths(case.home.root), owner)
        host = SimpleNamespace(home_paths=paths, config=AgentConfig(model_backend="echo", model_name="deployment-model"))
        model_profiles.execute_model_profile_operation(host, "add", {"profile_id": profile_id, "profile": {
            "model_backend": "anthropic_compatible", "model_name": name, "api_base": "https://example.test/anthropic",
            "api_key": "synthetic-test-credential", "model_context_window_tokens": 96000}})
    case.agent.home_paths = paths
    case.agent.config = host.config
    return profile_id


# LLM: 冷/热列表与详情均读取认证owner原档案，单次列表至多读一次目录，不公开服务商凭据或回写配置。
# 函数用途: 复现R3原生profile_id没有model_name时的展示缺失。
@pytest.mark.parametrize("cold", [False, True])
@pytest.mark.parametrize("action", ["list_subagents", "get_subagent"])
def test_r3_native_profile_resolves_owner_model_name(case, native_model_profile, monkeypatch, cold, action):
    ref = {"schema": "host_model_profile.v1", "profile_id": native_model_profile}
    task = case.manager.create_run(goal="model projection", owner=case.home.owner_id,
                                   attributes={"conversation_thread_id": "thread-one", "host_model_profile.v1": ref})
    assert "model_name" not in case.manager.load(task.id).attributes["host_model_profile.v1"]
    paths = home_paths_with_owner(home_paths(case.home.root), case.home)
    catalog_path = model_profiles.model_profiles_path(paths)
    before = catalog_path.read_bytes()
    calls, original = [], model_profiles.read_model_profiles
    monkeypatch.setattr(model_profiles, "read_model_profiles", lambda path: calls.append(path) or original(path))
    result = getattr(sub, action)(case.context, {"subagent_id": task.id, "thread_id": "thread-one"},
                                  agent=None if cold else case.agent)
    row = next(item for item in result["items"] if item["subagent_id"] == task.id) if action == "list_subagents" else result
    assert row["model_name"] == "Owner-Model-R3"
    assert calls == [catalog_path]
    assert "synthetic-test-credential" not in json.dumps(result)
    assert catalog_path.read_bytes() == before


# LLM: 显式名称优先；只有default可回退agent配置，未知或非字符串ID不能误报为部署模型。
# 函数用途: 检查R3名称优先级、default与无配置时的未知语义。
@pytest.mark.parametrize("ref, configured, expected", [
    ({"model_name": "recorded-name", "profile_id": "default"}, True, "recorded-name"),
    ({"profile_id": "default"}, True, "deployment-model"),
    ({"profile_id": "default"}, False, None),
    ({"profile_id": "removed-profile"}, True, None),
    ({"profile_id": []}, True, None),
    ({}, True, None),
])
def test_r3_model_name_fallback_semantics(case, ref, configured, expected):
    case.agent.config = SimpleNamespace(model_name="deployment-model") if configured else None
    case.manager.mutate(case.task.id, lambda task: task.attributes.update({"host_model_profile.v1": ref}))
    rows = sub.list_subagents(case.context, {"thread_id": "thread-one"}, agent=case.agent)["items"]
    detail = sub.get_subagent(case.context, {"subagent_id": case.task.id}, agent=case.agent)
    assert rows[0]["model_name"] == detail["model_name"] == expected


# LLM: 模型目录损坏是存储不可用，不可伪装为空目录/未知模型，也不把原错误中的配置内容回给调用者。
# 函数用途: 保留R3原生目录校验失败的结构化错误。
@pytest.mark.parametrize("action", ["list_subagents", "get_subagent"])
def test_r3_corrupt_model_catalog_is_unavailable(case, native_model_profile, action):
    path = model_profiles.model_profiles_path(home_paths_with_owner(home_paths(case.home.root), case.home))
    path.write_text("{broken")
    with pytest.raises(HostAPIError) as error:
        getattr(sub, action)(case.context, {"subagent_id": case.task.id, "thread_id": "thread-one"}, agent=case.agent)
    assert error.value.code == "STORE_UNAVAILABLE" and error.value.status == 503


# LLM: 在真实nofollow FD打开后立刻替换叶/父路径；只注入确定性时序，不替换真实消息内容或解析器。
# 函数用途: 复现R4消息预检后重新按路径打开导致的跨owner泄露。
@pytest.mark.parametrize("swap", ["leaf", "parent"])
def test_r4_messages_keep_verified_descriptor_after_path_swap(case, message_thread, monkeypatch, swap):
    case.store.messages.append({"thread_id": message_thread.thread_id, "role": "assistant", "content": "own-safe-text"})
    path = case.store.storage.message_path(message_thread.thread_id)
    foreign = case.other.home_dir / path.name
    foreign.write_text(json.dumps({"message_id": "foreign", "thread_id": message_thread.thread_id,
                                   "role": "assistant", "content": "foreign-private-text"}) + "\n")
    original, opened, calls = sub.open_readonly_file_beneath, [], []
    original_recent = case.store.messages.recent_report
    monkeypatch.setattr(case.store.messages, "recent_report", lambda *a, **kw: calls.append(a) or original_recent(*a, **kw))
    # LLM: 保留真正已校验的FD，再把同一原路径换成另一个owner的文件。
    # 函数用途: 在旧预检和路径重开之间建立准确的竞态观察点。
    def swapped(home, parts):
        fd = original(home, parts)
        if Path(home).joinpath(*parts) == path:
            opened.append(fd)
            if swap == "leaf":
                path.rename(path.with_name("preserved.jsonl"))
                path.symlink_to(foreign)
            else:
                path.parent.rename(case.root / "preserved-messages")
                path.parent.symlink_to(case.other.home_dir, target_is_directory=True)
        return fd
    monkeypatch.setattr(sub, "open_readonly_file_beneath", swapped)
    result = sub.get_subagent(case.context, {"subagent_id": case.task.id}, agent=case.agent, store=case.store)
    assert [row["content"] for row in result["messages"]] == ["own-safe-text"]
    assert len(opened) == 1 and calls == []
    with pytest.raises(OSError):
        os.fstat(opened[0])


# LLM: 大display记录占满窗口前缀；真实消息含合成秘密，坏JSON/坏类型不占最后50条真实消息名额。
# 函数用途: 验证R4有界FD尾读、原类型解析、display过滤及递归脱敏。
@pytest.mark.parametrize("corrupt", [False, True])
def test_r4_messages_tail_limit_and_redaction(case, message_thread, monkeypatch, corrupt):
    from unittest.mock import MagicMock

    path = case.store.storage.message_path(message_thread.thread_id)
    prefix = json.dumps({"role": "display", "content": "x" * (600 * 1024)})
    rows = [{"message_id": f"row-{index}", "thread_id": message_thread.thread_id, "role": "assistant",
             "content": f"message-{index}; api_key=synthetic-inline-value",
             "metadata": {"api_key": "synthetic-field-value"}} for index in range(60)]
    suffix = ['{"role":"display"}', json.dumps({"thread_id": "different-thread", "role": "assistant", "content": "wrong-thread"})]
    if corrupt:
        suffix += ["{broken", "[]", '{"created_at":"not-a-number"}']
    path.write_text("\n".join([prefix, *(json.dumps(row) for row in rows), *suffix]) + "\n")
    original, calls = case.store.messages.recent_report, []
    monkeypatch.setattr(case.store.messages, "recent_report", lambda *a, **kw: calls.append(a) or original(*a, **kw))
    open_file, fdopen, descriptors, reads = sub.open_readonly_file_beneath, os.fdopen, set(), []
    # LLM: 不伪造FD，只标记真正消息FD以观察其单次读取预算。
    # 函数用途: 在原nofollow打开后记录准确描述符。
    def tracked(home, parts):
        fd = open_file(home, parts)
        if Path(home).joinpath(*parts) == path:
            descriptors.add(fd)
        return fd
    # LLM: 包装真实流且沿原__exit__关闭；仅消息的rb流记录read参数，不改变数据或seek。
    # 函数用途: 确认固定字节预算而非无界read。
    def observed(fd, *args, **kwargs):
        stream = fdopen(fd, *args, **kwargs)
        if fd not in descriptors or args != ("rb",):
            return stream
        proxy = MagicMock(wraps=stream)
        proxy.__enter__.return_value = proxy
        proxy.__exit__.side_effect = stream.__exit__
        proxy.read.side_effect = lambda count: reads.append(count) or stream.read(count)
        return proxy
    monkeypatch.setattr(sub, "open_readonly_file_beneath", tracked)
    monkeypatch.setattr(os, "fdopen", observed)
    result = sub.get_subagent(case.context, {"subagent_id": case.task.id}, agent=case.agent, store=case.store)
    assert [row["message_id"] for row in result["messages"]] == [f"row-{index}" for index in range(10, 60)]
    assert "synthetic-" not in json.dumps(result)
    assert all(row["metadata"]["api_key"] == "<redacted>" for row in result["messages"])
    assert calls == [] and reads == [524288]


# LLM: 元数据路径必须在检查目录存在性或运行后端之前拒绝；只创建空.git目录，不写Git配置或hooks。
# 函数用途: 复现R4工作根含.git及大小写变体绕过。
@pytest.mark.parametrize("raw", ["project/.git", "project/.GIT/hooks", "project/.GiT/objects", "project/.GiT/../project"])
@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("action", ["list_workspace_files", "read_workspace_file", "download_workspace_file", "upload_workspace_file", "inspect_workspace_git"])
def test_r4_workspace_git_root_is_forbidden(case, monkeypatch, raw, absolute, action):
    (case.root / "project" / ".git").mkdir(parents=True)
    calls, original = [], files.validate_workspace_directory
    monkeypatch.setattr(files, "validate_workspace_directory", lambda *a, **kw: calls.append(a) or original(*a, **kw))
    payload = {"workspace_dir": str(case.root / raw) if absolute else raw, "path": "pack", "data_base64": "eA=="}
    with pytest.raises(HostAPIError) as error:
        getattr(files, action)(case.context, payload)
    assert error.value.code == "PATH_OUTSIDE_WORKSPACE" and error.value.status == 403
    assert calls == []


# LLM: workspace使用capabilities的同一相对路径拆分器，两条入口均须不区分大小写且保留稳定403。
# 函数用途: 防止R4只修工作根却漏掉共享相对路径校验。
@pytest.mark.parametrize("parts", [caps._parts, files._parts])
@pytest.mark.parametrize("value", [".git", ".GIT/hooks", "project/.GiT/objects"])
def test_r4_relative_git_parts_are_forbidden(parts, value):
    with pytest.raises(HostAPIError) as error:
        parts(value)
    assert error.value.code == "PATH_OUTSIDE_WORKSPACE" and error.value.status == 403


# LLM: 管理员外部项目同样不授Git元数据浏览；相似前缀的普通目录仍可读。
# 函数用途: 验证R4外部根边界和普通工作区不误杀。
def test_r4_external_git_path_and_lookalike_directory(case):
    with pytest.raises(HostAPIError) as error:
        files._forbid_internal(case.root, case.other.home_dir / "project" / ".GIT" / "objects")
    assert error.value.code == "PATH_OUTSIDE_WORKSPACE" and error.value.status == 403
    folder = case.root / "project.git"
    folder.mkdir()
    (folder / "ok").write_text("ordinary")
    assert files.read_workspace_file(case.context, {"workspace_dir": str(folder), "path": "ok"})["content"] == "ordinary"


# LLM: 原回执仍为JSON字符串列表；秒/毫秒/带时区与无时区ISO统一后稳定排序，不改原seq或正文。
# 函数用途: 复现R4时间线按追加顺序而非实际时间返回的问题。
@pytest.mark.parametrize("field, values", [
    ("ts", [30, 10, 20]), ("ts_ms", [30000, 10000, 20000]),
    ("timestamp", ["1970-01-01T00:00:30Z", "1970-01-01T01:00:10+01:00", "1970-01-01T00:00:20Z"]),
    ("created_at", ["1970-01-01T00:00:30", "1970-01-01T00:00:10", "1970-01-01T00:00:20"]),
])
def test_r4_steps_are_chronological(case, field, values):
    path = Path(case.manager.load(case.task.id).agent_run_timeline_jsonl)
    rows = [{field: value, "label": label, "seq": index} for index, (value, label) in enumerate(zip(values, ("thirty", "ten", "twenty")))]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    result = sub.get_subagent(case.context, {"subagent_id": case.task.id}, agent=case.agent)
    parsed = [json.loads(line) for line in result["steps"]]
    assert [row["label"] for row in parsed] == ["ten", "twenty", "thirty"]
    assert [row["seq"] for row in parsed] == [1, 2, 0]


# LLM: 同时出现不同时间字段也必须按同一单位比较；非有限数/布尔时间无效，未知项稳定放末尾。
# 函数用途: 验证R4时间键回退、稳定同值次序以及原始非JSON行兼容。
def test_r4_steps_mixed_timestamps_and_stable_ties(case):
    path = Path(case.manager.load(case.task.id).agent_run_timeline_jsonl)
    rows = [{"ts_ms": 30000, "label": "thirty"}, {"ts": "NaN", "timestamp": "1970-01-01T00:00:10Z", "label": "ten-a"},
            {"created_at": "1970-01-01T00:00:20", "label": "twenty"}, {"ts": 10, "label": "ten-b"},
            {"ts": True, "label": "unknown"}]
    path.write_text("\n".join([*(json.dumps(row) for row in rows), "legacy-plain-line"]) + "\n")
    result = sub.get_subagent(case.context, {"subagent_id": case.task.id}, agent=case.agent)
    assert [json.loads(line)["label"] for line in result["steps"][:-1]] == ["ten-a", "ten-b", "twenty", "thirty", "unknown"]
    assert result["steps"][-1] == "legacy-plain-line"


# LLM: 原生消息写入器实际保留这些Unicode字符，它们是正文而不是JSONL的LF记录分隔符。
# 函数用途: 复现独立复核发现的splitlines误拆合法正文，并覆盖CRLF时间线。
@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
@pytest.mark.parametrize("view", ["messages", "steps"])
def test_r4_jsonl_unicode_separators_are_content(case, message_thread, separator, view):
    content = "甲" + separator + "乙"
    if view == "messages":
        case.store.messages.append({"thread_id": message_thread.thread_id, "role": "assistant", "content": content})
        path = case.store.storage.message_path(message_thread.thread_id)
    else:
        path = Path(case.manager.load(case.task.id).agent_run_timeline_jsonl)
        raw = json.dumps({"ts": 10, "label": content}, ensure_ascii=False)
        path.write_bytes((raw + "\r\n").encode())
    assert separator.encode() in path.read_bytes()
    result = sub.get_subagent(case.context, {"subagent_id": case.task.id}, agent=case.agent, store=case.store)
    if view == "messages":
        assert [row["content"] for row in result["messages"]] == [content]
    else:
        assert result["steps"] == [raw]


# LLM: 大小写不敏感文件系统上DATA/RUNS/runſ(U+017F)等受保护宿主目录别名及Git观察子目录均必须拦截或过滤。
# 函数用途: 覆盖工作区根与相对路径的Unicode casefold/inode受保护目录拦截及inspect_workspace_git过滤。
@pytest.mark.parametrize("alias", ["DATA", "DaTa", "RUNS", "run\u017f", "workspace/RUNTIME", "WORKSPACE/runtime"])
def test_r5_case_insensitive_protected_dirs_forbidden(case, monkeypatch, alias):
    (case.root / "runs").mkdir(exist_ok=True)
    encoded = base64.b64encode(b"probe").decode()
    for root_val, rel_val in ((str(case.root / alias), "probe.txt"), (str(case.root), f"{alias}/probe.txt")):
        with pytest.raises(HostAPIError) as err_list:
            files.list_workspace_files(case.context, {"workspace_dir": root_val, "relative_path": "" if root_val != str(case.root) else alias})
        assert err_list.value.code == "WORKSPACE_PATH_INVALID"
        with pytest.raises(HostAPIError) as err_read:
            files.read_workspace_file(case.context, {"workspace_dir": root_val, "path": rel_val})
        assert err_read.value.code == "WORKSPACE_PATH_INVALID"
        with pytest.raises(HostAPIError) as err_down:
            files.download_workspace_file(case.context, {"workspace_dir": root_val, "path": rel_val})
        assert err_down.value.code == "WORKSPACE_PATH_INVALID"
        if root_val != str(case.root):
            with pytest.raises(HostAPIError) as err_up:
                files.upload_workspace_file(case.context, {"workspace_dir": root_val, "path": "probe.txt", "data_base64": encoded})
            assert err_up.value.code == "WORKSPACE_PATH_INVALID"
    monkeypatch.setattr(files, "_stat", lambda *args, **kwargs: SimpleNamespace(st_mode=0o040700))
    outputs = iter([
        ("## main\x00 M data/secret.txt\x00 M src/app.py\x00", False),
        (
            'diff --git a/old.txt "b/run\\305\\277/probe.txt"\n+secret1\n'
            'diff --git "1/run\\305\\277/fixture.txt" "2/run\\305\\277/fixture.txt"\n+secret2\n'
            'diff --git "x/run\\305\\277/before.txt" "y/run\\305\\277/after.txt"\n--- "x/run\\305\\277/before.txt"\n+++ "y/run\\305\\277/after.txt"\n@@ -1 +1 @@\n-before\n+secret3\n'
            'diff --git a/a/data/before.txt b/b/after.txt\nsimilarity index 100%\nrename from a/data/before.txt\nrename to b/after.txt\n'
            'diff --git a/notes a/data/old.txt b/notes a/data/new.txt\n--- a/notes a/data/old.txt\n+++ b/notes a/data/new.txt\n@@ -1 +1 @@\n-old\n+kept_space_dir\n'
            'diff --git a/src/app.py b/src/app.py\n+literal diff --git a/data/not-a-path.txt b/data/not-a-path.txt\n+ok\n',
            False,
        ),
    ])
    recorded = []
    monkeypatch.setattr(files, "_git", lambda _fd, args: recorded.append(args) or next(outputs))
    git_res = files.inspect_workspace_git(case.context, {"workspace_dir": str(case.root), "path": ":(exclude,icase)agent_py_agent"})
    assert recorded[0][recorded[0].index("--") + 1] == ":(literal):(exclude,icase)agent_py_agent"
    assert git_res["changed_files"] == [{"status": "M", "path": "src/app.py"}]
    assert "secret" not in git_res["diff"] and "rename from a/data/before.txt" in git_res["diff"] and "kept_space_dir" in git_res["diff"]
    assert "+literal diff --git a/data/not-a-path.txt" in git_res["diff"] and git_res["clean"] is False
