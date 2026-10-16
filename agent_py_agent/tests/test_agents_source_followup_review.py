"""followup独立审查反例；真实PTY IO，平台启动替代，不是OS防写或完整进程树验收。"""
import json
import sys
import threading
import time

import pytest

from agent_py_agent.agent.capability.agents_source_contract import AgentsContractError
from agent_py_agent.agent.capability.agents_source_repository import AgentsSourceRepository
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.tooling import pty_sessions as pty
from agent_py_agent.agent.tooling.process_registry import ProcessTerminationReceipt
from agent_py_agent.tests.test_agents_source_protection import protected as protected
from agent_py_agent.tests.test_agents_source_pty_revision import running as running
from agent_py_agent.tests.test_agents_source_pty_root import full_terminal as full_terminal
from agent_py_agent.tests.test_agents_source_repository import choose


def test_pty_fd_close_waits_for_already_admitted_write(running, monkeypatch):
    _, session, _, _ = running
    entered, release, closing, closed = (threading.Event() for _ in range(4))
    original_select = pty.select.select
    writes, errors = [], []

    def paused_select(read, write, extra, timeout):
        if session.master_fd in write:
            entered.set()
            assert release.wait(3), "主线程必须释放写屏障"
        return original_select(read, write, extra, timeout)

    def writer():
        try:
            writes.append(pty.pty_session_registry.write(session.session_id, b"already-admitted"))
        except Exception as exc:
            errors.append(exc)

    def closer():
        closing.set()
        pty._close_session_fd(session)
        closed.set()

    monkeypatch.setattr(pty.select, "select", paused_select)
    write_thread = threading.Thread(target=writer)
    close_thread = threading.Thread(target=closer)
    write_thread.start()
    try:
        assert entered.wait(2)
        close_thread.start()
        assert closing.wait(2)
        assert not closed.wait(.2), "实际写入完成前不能关闭可复用的master_fd整数"
        assert not session.closed
    finally:
        release.set()
        write_thread.join(3)
        if close_thread.ident is not None:
            close_thread.join(3)
    assert not write_thread.is_alive() and not close_thread.is_alive()
    assert closed.is_set() and session.closed
    assert writes == [session] and not errors


def test_full_access_old_pty_rejects_input_when_owner_set_grows(protected, running):
    tool, session, _, _ = running
    (protected.root / "owners/providers/feishu/users/new").mkdir(parents=True)
    result = tool.execute({"action": "write", "session_id": session.session_id, "data": "hello"})
    assert not result.ok, "Full Access不能只比较启动时已有owner的revision"
    assert protected.source.read_bytes() == protected.before


def test_new_owner_source_commit_cannot_ignore_preexisting_full_access_peer(protected, running):
    _, session, _, _ = running
    home = protected.root / "owners/providers/feishu/users/new"
    home.mkdir(parents=True)
    source = home / "business-rules.txt"
    source.write_text("# new owner\n- 保留原件。\n")
    before = source.read_bytes()
    repo = AgentsSourceRepository(owner="providers/feishu/users/new", owner_home=home,
                                  path_policy=PathAccessPolicy.from_values(mode="full", agent_home_root=protected.root))
    with pytest.raises(AgentsContractError, match="cross_owner_resource"):
        choose(repo, source)
    assert not (home / "persona/agents_source.json").exists()
    assert source.read_bytes() == before
    assert session.process.poll() is None, "新owner不能擅停另一owner的资源来冒充安全切源"


def test_product_write_drains_bidirectional_backpressure(full_terminal, monkeypatch):
    tool, _ = full_terminal
    program = ("import os,tty,time; tty.setraw(0); os.write(1,b'__READY__\\n'); remaining=64000\n"
               "while remaining:\n"
               " data=os.read(0,min(4096,remaining)); remaining-=len(data)\n"
               " os.write(1,b'Y'*64000)\n"
               "os.write(1,b'__ALL_INPUT_RECEIVED__\\n'); time.sleep(30)")
    monkeypatch.setattr(pty, "_sandbox_exec", lambda *a, **kw: ([sys.executable, "-c", program], False))
    started = tool.execute({"action": "start", "command": "controlled-raw-python"})
    assert started.ok, started.output
    sid = json.loads(started.output)["session_id"]
    session = pty.pty_session_registry.get(sid)
    try:
        deadline = time.monotonic() + 3
        while b"__READY__" not in session.read(0, 32000)[0] and time.monotonic() < deadline:
            time.sleep(.01)
        assert b"__READY__" in session.read(0, 32000)[0]
        result = tool.execute({"action": "write", "session_id": sid, "data": "x" * 64000})
        assert result.ok, "原产品write在双向背压下必须完成全部输入: " + result.output
        deadline = time.monotonic() + 3
        while b"__ALL_INPUT_RECEIVED__" not in session.output and time.monotonic() < deadline:
            time.sleep(.01)
        assert b"__ALL_INPUT_RECEIVED__" in session.output
    finally:
        # 平台启动替代、夹具单根回收；不冒充原生产后代清理或OS保护证据。
        if session.process.poll() is None:
            session.process.kill()
        session.process.wait(timeout=3)
        pty.pty_session_registry.close(sid)


def _new_provider_source(protected):
    home = protected.root / "owners/providers/feishu/users/new"
    home.mkdir(parents=True)
    source = home / "business-rules.txt"
    source.write_text("# new owner\n- 保留原件。\n")
    repo = AgentsSourceRepository(owner="providers/feishu/users/new", owner_home=home,
                                  path_policy=PathAccessPolicy.from_values(mode="full", agent_home_root=protected.root))
    return repo, source


def test_confirmed_retired_history_does_not_block_new_owner(protected, running, monkeypatch):
    _, session, _, _ = running

    # LLM: 清退器接收本次包装器及原生Popen的冻结出生身份，受控单根仍核对对象和配置先后。
    # 函数用途: 验证新PTY包装没有换错进程或提前提交来源。
    original_cleanup = pty.terminate_process_tree

    def known_single_root(pid, proc, *, expected_birth_token):
        if proc is not session.process:
            return original_cleanup(pid, proc, expected_birth_token=proc.birth_token)
        assert pid == proc.raw.pid and proc is session.process
        assert expected_birth_token == session.process.birth_token
        proc.kill()
        code = proc.wait(timeout=3)
        return ProcessTerminationReceipt("test-single-root-only", True, code, 1)

    monkeypatch.setattr(pty, "terminate_process_tree", known_single_root)
    pty.pty_session_registry.close(session.session_id)
    assert session.closed and session.termination.confirmed
    assert session.termination.method != "already_exited"
    repo, source = _new_provider_source(protected)
    before = source.read_bytes()
    changed = choose(repo, source)
    assert changed.revision == 1
    assert source.read_bytes() == before and protected.source.read_bytes() == protected.before


# LLM: 此负例刻意恢复旧Popen已回收身份；新包装器的WNOWAIT正例另测，不能删旧资源未确认保护。
# 函数用途: 原生父进程先被回收时，历史仍须阻止跨owner切源。
def test_natural_exit_history_still_blocks_new_owner(protected, running):
    _, session, _, _ = running
    with session.close_lock:
        raw = session.process.raw
        raw.kill()
        raw.wait(timeout=3)
        session.process = raw
    pty.pty_session_registry.close(session.session_id)
    assert session.closed and session.termination.method == "already_exited"
    repo, source = _new_provider_source(protected)
    with pytest.raises(AgentsContractError, match="cross_owner_resource"):
        choose(repo, source)
    assert not repo.config_path.exists()


def test_resize_size_read_waits_before_fd_close(running, monkeypatch):
    tool, session, _, _ = running
    entered, release, closing, closed = (threading.Event() for _ in range(4))
    original = pty.os.get_terminal_size
    results, errors = [], []

    def paused_size(fd):
        assert fd == session.master_fd
        entered.set()
        assert release.wait(3)
        return original(fd)

    def resizing():
        try:
            results.append(tool.execute({"action": "resize", "session_id": session.session_id,
                                         "columns": 124, "rows": 42}))
        except Exception as exc:
            errors.append(exc)

    def closer():
        closing.set()
        pty._close_session_fd(session)
        closed.set()

    monkeypatch.setattr(pty.os, "get_terminal_size", paused_size)
    resize_thread = threading.Thread(target=resizing)
    close_thread = threading.Thread(target=closer)
    resize_thread.start()
    try:
        assert entered.wait(2)
        close_thread.start()
        assert closing.wait(2)
        assert not closed.wait(.2), "尺寸真实读回完成前不能关闭/复用FD"
    finally:
        release.set()
        resize_thread.join(3)
        if close_thread.ident is not None:
            close_thread.join(3)
    assert not errors and len(results) == 1 and results[0].ok
    actual = json.loads(results[0].output)
    assert actual["columns"] == 124 and actual["rows"] == 42
    assert not resize_thread.is_alive() and not close_thread.is_alive() and closed.is_set()
