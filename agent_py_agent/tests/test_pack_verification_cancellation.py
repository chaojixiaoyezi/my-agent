"""块 6a：本 run 的取消停止核验、回收组内后代、不返工且如实写账。

真实进程用例只启动并清理自己创建的隔离进程组；不使用 ps/Gateway/真实 owner。
platform 用例保留真实 Seatbelt/bwrap，嵌套不可用时需 3a 在沙箱外复跑。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from agent_py_agent.agent.attempt.sandbox import (
    AttemptExecutionSandbox,
    AttemptSandboxSpec,
    SandboxUnavailableError,
)
from agent_py_agent.agent.capability import pack_verification_service as service
from agent_py_agent.agent.capability.pack_verification_hooks import (
    attach_post_write_verification,
    capture_baseline_before_tool,
    closeout_rework_block,
)
from agent_py_agent.agent.capability.pack_verification_ledger import PackVerificationLedger
from agent_py_agent.agent.capability.pack_verification_report import (
    pack_verification_facts,
    pack_verification_notice_text,
    run_pack_verification_facts,
)
from agent_py_agent.agent.capability.pack_verifier_runner import (
    PackVerifierRequest,
    run_pack_verifier,
)
from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.tests.test_pack_verification_service import (
    _tool_result,
    _write,
    build_env,
    install_fake_runner,
)
from agent_py_agent.tests.test_pack_verifier_runner import PASS, _installed, _target


@pytest.fixture
def env(tmp_path, monkeypatch):
    value = build_env(tmp_path, monkeypatch)
    value.params.cancellation_token = CancellationToken()
    capture_baseline_before_tool(value.agent, value.params, "write_file")
    return value


@pytest.mark.parametrize("trigger", ["post_write", "closeout"])
def test_already_cancelled_starts_no_new_verifier_and_records_every_target(env, monkeypatch, trigger):
    calls = install_fake_runner(monkeypatch)
    paths = [_write(env.workspace / f"out/{index}.json", {"schema": "delivery.v1"}) for index in range(3)]
    env.params.cancellation_token.cancel("user_stop")
    if trigger == "post_write":
        for path in paths:
            result = attach_post_write_verification(env.agent, env.params, _tool_result(path))
            assert result.ok, "取消核验不能翻转已经成功的写入"
    else:
        assert closeout_rework_block(env.agent, env.params) == ""
    assert calls == [], "已取消不再进入运行器"
    rows = [row for row in env.ledger.records() if row["kind"] == "result"]
    assert [row["fact"]["status"] for row in rows] == ["cancelled"] * 3
    assert {row["fact"]["target"] for row in rows} == {f"out/{index}.json" for index in range(3)}
    assert all(row["fact"]["reason_code"] == "verifier_cancelled" for row in rows)
    assert [row["payload"]["status"] for row in env.repo.events] == ["cancelled"] * 3


def test_cancel_during_closeout_does_not_rework_prior_failure_and_skips_rest(env, monkeypatch):
    calls = install_fake_runner(monkeypatch)
    original = service.run_pack_verifier

    def cancelling(request):
        result = original(request)
        env.params.cancellation_token.cancel("user_stop")
        return result

    monkeypatch.setattr(service, "run_pack_verifier", cancelling)
    for index in range(3):
        _write(env.workspace / f"out/{index}.json", {"schema": "delivery.v1", "bad": True})
    assert closeout_rework_block(env.agent, env.params) == ""
    assert len(calls) == 1
    assert env.ledger.count("rework") == 0
    facts = run_pack_verification_facts(env.agent, env.params)
    assert [item["status"] for item in facts["results"]] == ["failed", "cancelled", "cancelled"]
    assert facts["cancelled"] is True
    assert "被取消" in pack_verification_notice_text(facts)


def test_cancelled_closeout_overrides_cached_failure_and_does_not_rework(env, monkeypatch):
    calls = install_fake_runner(monkeypatch)
    target = _write(env.workspace / "out/d.json", {"schema": "delivery.v1", "bad": True})
    attach_post_write_verification(env.agent, env.params, _tool_result(target))
    env.params.cancellation_token.cancel()
    assert closeout_rework_block(env.agent, env.params) == ""
    assert len(calls) == 1 and env.ledger.count("rework") == 0
    facts = run_pack_verification_facts(env.agent, env.params)
    assert facts["results"][0]["status"] == "cancelled"
    assert "被取消" in pack_verification_notice_text(facts)


def test_cancelled_result_is_not_reused_after_explicit_new_token(env, monkeypatch):
    calls = install_fake_runner(monkeypatch)
    target = _write(env.workspace / "out/d.json", {"schema": "delivery.v1"})
    env.params.cancellation_token.cancel()
    attach_post_write_verification(env.agent, env.params, _tool_result(target))
    env.params.cancellation_token = CancellationToken()
    attach_post_write_verification(env.agent, env.params, _tool_result(target))
    assert len(calls) == 1
    assert run_pack_verification_facts(env.agent, env.params)["results"][0]["status"] == "passed"


def test_no_tool_decision_cancelled_before_or_during_verification_never_reworks(env, monkeypatch):
    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        ToolLoopRepairCounters,
        _no_tool_calls_decision,
        _NoToolCallsRequest,
    )
    from agent_py_agent.agent.backends import ModelResponse

    calls = install_fake_runner(monkeypatch)
    original = service.run_pack_verifier
    monkeypatch.setattr(service, "run_pack_verifier", lambda request: _cancel_after(original(request), env))
    _write(env.workspace / "out/d.json", {"schema": "delivery.v1", "bad": True})
    response = ModelResponse("模型声称完成", "fake", truncated=True)
    request = _NoToolCallsRequest(env.agent, env.params, response, ToolLoopRepairCounters(), False)
    decision = _no_tool_calls_decision(request)
    assert decision.action == "break" and decision.response.runtime_status == "cancelled"
    assert env.params.tool_context == [] and env.ledger.count("rework") == 0 and len(calls) == 1
    # 已取消的子代理还缺其它声明交付物，也不能先被子代理交付门送去返工。
    env.params.context_scope = "task_local"
    env.params.task_attributes = {"output_files": [str(env.workspace / "missing.txt")]}
    again = _no_tool_calls_decision(request)
    assert again.action == "break" and again.response.runtime_status == "cancelled"
    assert env.params.live_archive_state == {} and env.params.tool_context == []


def _cancel_after(result, env):
    env.params.cancellation_token.cancel()
    return result


def test_runner_maps_running_cancellation_to_cancelled_fact(tmp_path, monkeypatch):
    owner, entry = _installed(tmp_path, PASS)
    workspace, target = _target(tmp_path)
    monkeypatch.setattr(AttemptExecutionSandbox, "require_ready", lambda self: None)
    monkeypatch.setattr(AttemptExecutionSandbox, "run", lambda *a, **k: _raise_cancelled())
    result = run_pack_verifier(PackVerifierRequest(owner, entry, "check", target, workspace))
    assert result.status == "cancelled" and result.reason_code == "verifier_cancelled"
    assert result.valid is None and result.error_counts == {} and result.returncode is None
    assert len(result.member_sha256) == 64 and len(result.target_sha256) == 64


def _raise_cancelled():
    raise ToolCancelled("user_stop")


def test_runner_already_cancelled_never_probes_or_runs(tmp_path, monkeypatch):
    owner, entry = _installed(tmp_path, PASS)
    workspace, target = _target(tmp_path)
    monkeypatch.setattr(AttemptExecutionSandbox, "require_ready", lambda self: pytest.fail("不应探测"))
    token = CancellationToken()
    token.cancel()
    with bind_cancellation_token(token):
        result = run_pack_verifier(PackVerifierRequest(owner, entry, "check", target, workspace))
    assert result.status == "cancelled" and result.reason_code == "verifier_cancelled"


def _plain_sandbox(tmp_path, monkeypatch):
    sandbox = AttemptExecutionSandbox(AttemptSandboxSpec(tmp_path, tmp_path, tmp_path, tmp_path))
    monkeypatch.setattr(sandbox, "require_ready", lambda: None)
    monkeypatch.setattr(sandbox, "build_argv", lambda argv: argv)
    return sandbox


def test_sandbox_already_cancelled_does_not_spawn(tmp_path, monkeypatch):
    sandbox = _plain_sandbox(tmp_path, monkeypatch)
    token = CancellationToken()
    token.cancel()
    calls = []
    popen = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: calls.append(a) or popen(*a, **kw))
    with bind_cancellation_token(token), pytest.raises(ToolCancelled):
        sandbox.run([sys.executable, "-c", "pass"], timeout=1)
    assert calls == []


CHILD_SCRIPT = """import pathlib, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
path = pathlib.Path(sys.argv[1])
for index in range(800):
    path.write_text(str(index))
    time.sleep(0.01)
"""
PARENT_SCRIPT = """import json, os, pathlib, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while not pathlib.Path(sys.argv[2]).exists():
    time.sleep(0.01)
pathlib.Path(sys.argv[3]).write_text(json.dumps({'parent': os.getpid(), 'child': child.pid, 'group': os.getpgrp()}))
time.sleep(8)
"""


def _cancel_on_ready(token, ready):
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if ready.exists():
        token.cancel("user_stop")


@pytest.mark.parametrize("mode", ["plain", "platform"])
def test_running_cancellation_reclaims_entire_process_group(tmp_path, monkeypatch, mode):
    # 与产品里跑检查程序的规格一致（pack_verifier_runner._sandbox_spec：整根只读、只写临时目录）：用宿主自己的解释器跑命令，
    # 解释器不在系统目录（GitHub 的 /opt/hostedtoolcache、家目录里的虚拟环境）时只放行系统目录的规格会 execvp 失败（CI 修复，2026-10-07）。
    sandbox = AttemptExecutionSandbox(AttemptSandboxSpec(tmp_path, tmp_path, tmp_path, tmp_path, read_only_root=True))
    if mode == "plain":
        sandbox = _plain_sandbox(tmp_path, monkeypatch)
    else:
        try:
            sandbox.require_ready()
        except SandboxUnavailableError:
            pytest.skip("需 3a 在沙箱外复跑真实 Seatbelt/bwrap 进程组取消")
    _exercise_process_group(sandbox, tmp_path, monkeypatch)


def _exercise_process_group(sandbox, root, monkeypatch):
    ready, heartbeat = root / "ready.json", root / "heartbeat.txt"
    token = CancellationToken()
    canceller = threading.Thread(target=_cancel_on_ready, args=(token, ready))
    signals, killpg, popen, spawned = [], os.killpg, subprocess.Popen, []
    monkeypatch.setattr(os, "killpg", lambda group, sig: signals.append((group, sig)) or killpg(group, sig))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: spawned.append(popen(*a, **kw)) or spawned[-1])
    started = time.monotonic()
    canceller.start()
    try:
        _expect_running_cancel(sandbox, token, heartbeat, ready)
        canceller.join(3)
        ids, host_group = json.loads(ready.read_text()), _command_process(spawned).pid
        assert ids["child"] != ids["parent"]
        if ids["parent"] == host_group:
            assert ids["group"] == host_group, "没有 PID 命名空间时，命令自己就是本次进程组的组长"
        assert time.monotonic() - started < 2, "不应等到检查程序 timeout 才退出"
        previous = heartbeat.read_text()
        time.sleep(0.15)
        assert heartbeat.read_text() == previous, "组内子进程必须停止，不能只杀组长"
        sent = [(group, sig) for group, sig in signals if sig != 0]  # 信号 0 是宽限期里探测组是否已退出
        if ids["parent"] == host_group:
            # 父子都忽略 TERM，组活过宽限期，必须再补 KILL
            assert sent == [(host_group, signal.SIGTERM), (host_group, signal.SIGKILL)]
        else:
            # bwrap：TERM 打掉外层 bwrap 后 PID 命名空间整个被内核回收，组在宽限期内就没了，不需要 KILL（上面心跳已停）
            assert sent[0] == (host_group, signal.SIGTERM) and set(sent) <= {(host_group, signal.SIGTERM), (host_group, signal.SIGKILL)}
        assert token._callbacks == {}, "取消回调不能遗留到下一条命令"
    finally:
        canceller.join(5)
        if any(PARENT_SCRIPT in process.args for process in spawned):
            _cleanup_test_group(killpg, _command_process(spawned).pid)


# Linux bwrap 有 PID 命名空间：命令自己写下的 pid/组号是命名空间里的编号（实测 parent=2、group=1），宿主侧的组号是
# Popen 的 pid。比较信号和清理都只能用宿主侧组号，拿命名空间编号去 killpg 会打到宿主上不相干的进程组。
def _command_process(spawned):
    return next(process for process in spawned if PARENT_SCRIPT in process.args)


def _cleanup_test_group(killpg, group):
    try:
        killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _expect_running_cancel(sandbox, token, heartbeat, ready):
    try:
        with bind_cancellation_token(token):
            result = sandbox.run([sys.executable, "-c", PARENT_SCRIPT, CHILD_SCRIPT, str(heartbeat), str(ready)],
                                 timeout=2, grace_seconds=0.05)
    except ToolCancelled:
        return
    pytest.fail(f"未进入取消终态：ready={ready.exists()}, heartbeat={heartbeat.exists()}, "
                f"returncode={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}；"
                "真实 Seatbelt/bwrap 用例需 3a 在沙箱外复跑")


def test_cancelled_fact_and_notice_survive_ledger_reload(env, monkeypatch):
    install_fake_runner(monkeypatch)
    _write(env.workspace / "out/d.json", {"schema": "delivery.v1"})
    env.params.cancellation_token.cancel()
    closeout_rework_block(env.agent, env.params)
    rows = [json.loads(line) for line in env.ledger.path.read_text().splitlines()]
    assert [row["fact"]["status"] for row in rows if row["kind"] == "result"] == ["cancelled"]
    facts = run_pack_verification_facts(env.agent, env.params)
    assert facts["cancelled"] is True and facts["rework_count"] == 0
    assert "被取消" in pack_verification_notice_text(facts)


@pytest.mark.parametrize("capture_output", [True, False])
def test_sandbox_normal_completion_unregisters_callback(tmp_path, monkeypatch, capture_output):
    sandbox = _plain_sandbox(tmp_path, monkeypatch)
    token = CancellationToken()
    with bind_cancellation_token(token):
        result = sandbox.run([sys.executable, "-c", "import sys; print(repr(sys.stdin.readline()))"],
                             timeout=2, capture_output=capture_output)
    assert result.returncode == 0
    assert result.stdout == ("''\n" if capture_output else None)
    assert token._callbacks == {}
    monkeypatch.setattr(os, "killpg", lambda *args: pytest.fail("旧回调不能再控制已结束的命令"))
    token.cancel()


def test_sandbox_external_check_only_cancellation_is_polled(tmp_path, monkeypatch):
    sandbox = _plain_sandbox(tmp_path, monkeypatch)
    ready = tmp_path / "external-ready.txt"
    token = CancellationToken(_external_check=ready.exists)
    with bind_cancellation_token(token), pytest.raises(ToolCancelled):
        sandbox.run([sys.executable, "-c", "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(4)",
                     str(ready)], timeout=2, grace_seconds=0.01)
    assert ready.exists() and token._callbacks == {}


def test_sandbox_timeout_preserves_143_and_reclaims_group(tmp_path, monkeypatch):
    sandbox = _plain_sandbox(tmp_path, monkeypatch)
    token = CancellationToken()
    with bind_cancellation_token(token):
        result = sandbox.run([sys.executable, "-c", "import time; time.sleep(4)"], timeout=0.1, grace_seconds=0.01)
    assert result.returncode == 143 and "TERM->grace" in result.stderr
    assert token._callbacks == {} and not token.cancelled


# ae 整合补的三处（3a 定）：取消时输入原件、交付物两段只记事实不返工；只剩“被取消”时事实和提示也要出来。
def test_cancelled_closeout_records_inputs_and_deliverables_without_rework(tmp_path, monkeypatch):
    install_fake_runner(monkeypatch)
    env = build_env(tmp_path, monkeypatch)
    env.params.cancellation_token = CancellationToken()
    source = _write(env.workspace / "in/source.json", {"schema": "source.v1"})
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    source.write_text(json.dumps({"schema": "source.v1", "edited": True}))
    _write(env.workspace / "notes.txt", "draft")
    env.params.cancellation_token.cancel("user_stop")
    assert closeout_rework_block(env.agent, env.params) == ""
    assert env.ledger.count("input_rework") == 0, "被取消的回合不为输入原件返工"
    assert env.ledger.count("deliverable_rework") == 0, "被取消的回合不为缺交付物返工"
    facts = run_pack_verification_facts(env.agent, env.params)
    assert facts["cancelled"] is True and [item["path"] for item in facts["inputs_modified"]] == ["in/source.json"]
    assert facts["deliverables_missing"][0]["code"] == "DELIVERABLE_MISSING", "两段事实照样入账"


def test_cancelled_closeout_alone_is_still_reported(tmp_path):
    ledger = PackVerificationLedger(tmp_path / "run-1.jsonl")
    ledger.append({"kind": "input_check", "items": []})
    ledger.append({"kind": "deliverable_check", "items": []})
    ledger.append({"kind": "closeout", "keys": [], "target_count": 0, "truncated": False,
                   "uncertain_targets": [], "current_truncated": False, "cancelled": True})
    facts = pack_verification_facts(ledger)
    assert facts is not None and facts["cancelled"] is True, "没有结果、原件和缺交付物，只剩被取消时也要报"
    assert facts["results"] == [] and pack_verification_notice_text(facts).startswith("本回合核验被取消")


def test_cancelled_only_facts_still_queue_a_notice(monkeypatch):
    import types

    from agent_py_agent.agent.gateway_parts import request_pack_verification_notice as notice_module

    monkeypatch.setattr(notice_module, "queue_host_notice", lambda store, thread_id, notice, replace_same_code: True)
    facts = {"results": [], "inputs_modified": [], "deliverables_missing": [], "cancelled": True}
    context = types.SimpleNamespace(agent=types.SimpleNamespace(conversation_store=object()))
    conversation = types.SimpleNamespace(thread_id="thread-1")
    [notice] = notice_module.queue_pack_verification_notice(context, conversation, types.SimpleNamespace(pack_verifications=facts))
    assert "被取消" in notice.text


# 9b 复核：宽限期内轮询进程组，收到 TERM 就退出的命令不让 cancel() 等满宽限期。
def test_cancel_returns_as_soon_as_the_group_exits_on_term(tmp_path, monkeypatch):
    sandbox = _plain_sandbox(tmp_path, monkeypatch)
    ready, token, elapsed = tmp_path / "ready.txt", CancellationToken(), []

    def cancel_when_ready():
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        started = time.monotonic()
        token.cancel("user_stop")
        elapsed.append(time.monotonic() - started)

    canceller = threading.Thread(target=cancel_when_ready)
    canceller.start()
    try:
        with bind_cancellation_token(token), pytest.raises(ToolCancelled):
            sandbox.run([sys.executable, "-c", "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(4)",
                         str(ready)], timeout=3, grace_seconds=2.0)
    finally:
        canceller.join(5)
    assert ready.exists() and elapsed[0] < 0.3, f"cancel() 用了 {elapsed[0]:.2f} 秒，被宽限期拖住了"


# 组信号被拒（PermissionError）沿用改造前的合同：不抛给调用方，补杀组长，超时仍返回 143。
def test_denied_group_signal_keeps_the_timeout_contract(tmp_path, monkeypatch):
    sandbox = _plain_sandbox(tmp_path, monkeypatch)
    heartbeat = tmp_path / "heartbeat.txt"
    script = "import pathlib,sys,time\nfor i in range(400):\n    pathlib.Path(sys.argv[1]).write_text(str(i)); time.sleep(0.01)"
    monkeypatch.setattr(os, "killpg", lambda group, sig: (_ for _ in ()).throw(PermissionError(1, "denied")))
    with bind_cancellation_token(CancellationToken()):
        result = sandbox.run([sys.executable, "-c", script, str(heartbeat)], timeout=0.3, grace_seconds=0.01)
    assert result.returncode == 143 and "TERM->grace" in result.stderr
    previous = heartbeat.read_text()
    time.sleep(0.15)
    assert heartbeat.read_text() == previous, "组信号被拒时要补杀组长，不能让命令继续跑"


# 9b 复核 6a 沙箱侧补的三条（3a 定）：迟到回调无操作、宽限期里 reap 组长、已取消不做就绪探测。
def test_late_cancellation_callback_after_close_is_a_noop(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from agent_py_agent.agent.attempt import process_run

    sandbox = _plain_sandbox(tmp_path, monkeypatch)
    captured = []

    @contextmanager
    def capture(callback):
        captured.append(callback)
        yield

    monkeypatch.setattr(process_run, "register_cancellation_callback", capture)
    result = sandbox.run([sys.executable, "-c", "pass"], timeout=2)
    assert result.returncode == 0 and len(captured) == 1
    signals = []
    monkeypatch.setattr(os, "killpg", lambda group, sig: signals.append((group, sig)))
    captured[0]()  # 命令已经结束、句柄已关闭后，取消线程才拿着复制出来的旧回调调到这里
    assert signals == [], "关闭之后的迟到回调不能再对任何进程组发信号（信号 0 也不行）"


def test_terminate_group_reaps_a_leader_that_exits_on_term_without_a_waiter(monkeypatch):
    from agent_py_agent.agent.attempt.process_run import _terminate_group

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    signals, killpg = [], os.killpg

    # 按 Linux 的语义探测：组长退出但没被 reap（僵尸）时，信号 0 照样成功，组还算“在”；reap 之后才是进程组不存在。
    # macOS 上僵尸组长的信号 0 返回 EPERM，会把“没 reap”掩盖成“已退出”，所以这里显式模拟，两个平台都能测。
    def linux_like_killpg(group, sig):
        signals.append(sig)
        if sig != 0:
            return killpg(group, sig)
        if proc.returncode is None:
            return None
        raise ProcessLookupError(group)

    monkeypatch.setattr(os, "killpg", linux_like_killpg)
    try:
        time.sleep(0.2)  # 让解释器起来，TERM 走默认处理直接退出
        started = time.monotonic()
        _terminate_group(proc, 2.0)
        elapsed = time.monotonic() - started
        # 没有并发的等待线程：组长只能由宽限期轮询里的 poll() 回收，回收了组才算退出，不用等满宽限、也不发 KILL
        assert proc.returncode == -signal.SIGTERM, "宽限期里要 reap 已退出的组长"
        assert elapsed < 1.0 and signal.SIGKILL not in signals
    finally:
        if proc.returncode is None:
            proc.kill()
        proc.wait(timeout=5)


def test_sandbox_already_cancelled_skips_readiness_probe(tmp_path, monkeypatch):
    sandbox = _plain_sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(sandbox, "require_ready", lambda: pytest.fail("已取消时不该再做沙箱就绪探测"))
    token = CancellationToken()
    token.cancel()
    with bind_cancellation_token(token), pytest.raises(ToolCancelled):
        sandbox.run([sys.executable, "-c", "pass"], timeout=1)
