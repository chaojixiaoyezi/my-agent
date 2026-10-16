"""来源revision与PTY资源边界；平台启动被替代，不能当真实OS/后代清理证据。"""
import json
import sys
from dataclasses import replace

import pytest

from agent_py_agent.agent.capability.agents_source_contract import AgentsContractError
from agent_py_agent.agent.capability.agents_source_repository import AgentsSourceRepository
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.tooling import pty_sessions as pty
from agent_py_agent.agent.tooling.process_registry import ProcessTerminationReceipt
from agent_py_agent.tests.test_agents_source_protection import protected as protected
from agent_py_agent.tests.test_agents_source_pty_root import full_terminal as full_terminal
from agent_py_agent.tests.test_agents_source_repository import choose, grant


@pytest.fixture
def running(protected, full_terminal, monkeypatch):
    tool, project = full_terminal
    monkeypatch.setattr(pty, "_sandbox_exec", lambda *a, **kw:
                        ([sys.executable, "-c", "import time; time.sleep(60)"], False))
    result = tool.execute({"action": "start", "command": "python -c 'import time; time.sleep(60)'"})
    assert result.ok, result.output
    sid = json.loads(result.output)["session_id"]
    session = pty.pty_session_registry.get(sid)
    repo = AgentsSourceRepository(owner="local/main", owner_home=protected.home,
                                 path_policy=PathAccessPolicy.from_values(mode="full", agent_home_root=protected.root))
    next_source = project / "next-rules.txt"
    next_source.write_text("# next source\n- 按用户授权处理。\n")
    try:
        yield tool, session, repo, next_source
    finally:
        # 夹具明确只启动一个不派生后代的Python；直接回收仅清夹具，不冒充生产树终止。
        if session.process.poll() is None:
            session.process.kill()
        session.process.wait(timeout=3)
        pty.pty_session_registry.close(sid)


@pytest.mark.parametrize("damaged", ["revision", "bad-config"])
def test_old_terminal_rejects_input_after_structured_source_change(protected, running, damaged):
    tool, session, _, _ = running
    if damaged == "revision":
        protected.control.write_text(json.dumps(dict(protected.record, revision=2)))
    else:
        protected.control.write_text("{broken")
    before = protected.source.read_bytes()
    result = tool.execute({"action": "write", "session_id": session.session_id, "data": "hello"})
    assert not result.ok, "来源revision/配置变后旧句柄不应提交任何stdin字节"
    assert protected.source.read_bytes() == before


def test_unconfirmed_resource_retirement_blocks_configuration_commit(protected, running, monkeypatch):
    _, session, repo, next_source = running
    monkeypatch.setattr(pty, "terminate_process_tree", lambda *a, **kw:
                        ProcessTerminationReceipt("test-unconfirmed", False, None, 1, (session.process.pid,)))
    with pytest.raises(AgentsContractError, match="AGENTS_SOURCE_RESOURCE_UNCONFIRMED"):
        choose(repo, next_source)
    assert protected.control.read_bytes() == protected.config_before
    assert protected.source.read_bytes() == protected.before
    assert session.process.poll() is None


def test_valid_source_change_retires_registered_pty_before_commit(protected, running, monkeypatch):
    _, session, repo, next_source = running
    before = next_source.read_bytes()

    # LLM: 包装器和原生Popen都是注册时那一个；仅调整入口形状，不放宽出生身份或配置提交顺序。
    # 函数用途: 验证受控单根清退与配置原子提交顺序。
    original_cleanup = pty.terminate_process_tree

    def controlled_single_root_cleanup(pid, proc, **kwargs):
        if proc is not session.process:
            return original_cleanup(pid, proc, expected_birth_token=proc.birth_token)
        assert proc is session.process and pid == proc.raw.pid
        assert kwargs["expected_birth_token"] == session.process.birth_token
        assert protected.control.read_bytes() == protected.config_before
        proc.kill()
        code = proc.wait(timeout=3)
        # 此替代依赖只证明受控单根和配置顺序；真实原进程树回执仍需3a观察点复核。
        return ProcessTerminationReceipt("test-single-root-only", True, code, 1)

    monkeypatch.setattr(pty, "terminate_process_tree", controlled_single_root_cleanup)
    changed = choose(repo, next_source)
    assert session.process.poll() is not None, "配置提交前必须确认旧资源已退役"
    assert session.termination is not None and session.termination.confirmed
    assert changed.revision == 2
    assert next_source.read_bytes() == before


def test_invalid_authorization_does_not_retire_a_live_resource(protected, running):
    _, session, repo, next_source = running
    value = {"kind": "local_path", "locator": str(next_source)}
    auth, context = grant(repo, "set_source", value)
    with pytest.raises(AgentsContractError):
        repo.set_source("local_path", str(next_source), authorization=auth, context=replace(context, action="write"))
    assert session.process.poll() is None
    assert protected.control.read_bytes() == protected.config_before


def test_original_tree_cleanup_retires_controlled_python_root(protected, running):
    _, session, repo, next_source = running
    changed = choose(repo, next_source)
    assert changed.revision == 2
    assert session.process.poll() is not None
    assert session.termination is not None and session.termination.confirmed
    assert session.termination.method != "already_exited"
    assert protected.source.read_bytes() == protected.before
