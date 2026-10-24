# LLM: c3的管道唤醒和路径投影回归；运行真实本地子进程，不代表Linux隔离实测。
# 模块用途: 锁住单selector、不丢唤醒以及每次准备的新鲜路径保护。
import os
import subprocess
import sys
import threading
import time

import pytest

from agent_py_agent.agent.tooling.process_output_capture import ProcessOutputCapture


# LLM: 只启动本测试可回收的短命令，finally总是关闭采集器并回收组长。
# 函数用途: 验证空输出进程只占一个采集线程，而不是每条流一个。
@pytest.mark.skipif(os.name != "posix", reason="POSIX selector contract")
def test_posix_capture_uses_one_thread():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    before = set(threading.enumerate())
    capture = ProcessOutputCapture(proc)
    try:
        readers = set(threading.enumerate()) - before
        assert len(readers) == 1
    finally:
        capture.finish(0)
        proc.kill()
        proc.wait(timeout=3)


# LLM: 先保存generation再发生EOF；wait_changed必须识别早于wait的变化，不能丢通知。
# 函数用途: 验证事件等待立即看到既有变化，并能显式唤醒空管道reader。
def test_generation_prevents_lost_wakeup_and_stop_closes_pipes():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    capture = ProcessOutputCapture(proc)
    try:
        assert callable(getattr(capture, "wait_changed", None))
        generation = capture.generation
        capture.record_data("stdout", b"chunk")
        assert capture.generation == generation
        capture.wake()
        start = time.monotonic()
        assert capture.wait_changed(1, generation=generation) > generation
        assert time.monotonic() - start < .5
        capture.stop()
        assert not capture.finish(0.5)
        assert proc.stdout.closed and proc.stderr.closed
        assert not capture.result()[2]["complete"]
    finally:
        capture.finish(0)
        proc.kill()
        proc.wait(timeout=3)


# LLM: spy仅记录原等待调用，不替换进程退出或回收；真实子进程仍由原入口处理。
# 函数用途: 验证shell与attempt使用capture变化等待而非盲sleep。
@pytest.mark.parametrize("entry", ["shell", "attempt"])
def test_process_waiters_use_capture_events(tmp_path, monkeypatch, entry):
    from agent_py_agent.agent.attempt.process_run import SandboxProcessOptions, run_sandbox_process
    from agent_py_agent.agent.tooling import shell

    calls = []
    original = ProcessOutputCapture.wait_changed

    # LLM: 保存传入的旧代次，转调真实条件等待，不提前返回。
    # 函数用途: 记录生产等待是否调用事件接口。
    def observed(self, timeout, **kwargs):
        calls.append(timeout)
        return original(self, timeout, **kwargs)

    monkeypatch.setattr(ProcessOutputCapture, "wait_changed", observed)
    argv = [sys.executable, "-c", "import time; time.sleep(.15); print('done')"]
    if entry == "attempt":
        result = run_sandbox_process(argv, SandboxProcessOptions(3, .01, True, tmp_path))
    else:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            result = shell._communicate_process(proc, command="event-wait", timeout=3)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=3)
    assert result.returncode == 0 and result.stdout == "done\n"
    assert calls, "生产等待必须使用可唤醒接口"


# LLM: 使用真实规范目录，spy只统计owner枚举；不同调用之间新owner和新托管目录必须可见。
# 函数用途: 验证冻结路径投影每次只枚举一次，且没有跨调用TTL缓存。
def test_host_paths_enumerate_once_and_refresh(tmp_path, monkeypatch):
    from dataclasses import FrozenInstanceError

    from agent_py_agent.agent import path_access_policy as policy

    assert callable(getattr(policy, "host_sandbox_paths", None))
    main = tmp_path / "owners/local/main"
    main.mkdir(parents=True)
    calls, original = [], policy._existing_owner_homes

    # LLM: 转调真实目录枚举，不凭预设清单模拟新增owner。
    # 函数用途: 统计单次投影的owner枚举次数。
    def homes(root):
        calls.append(root)
        return original(root)

    monkeypatch.setattr(policy, "_existing_owner_homes", homes)
    first = policy.host_sandbox_paths(tmp_path, system="Darwin")
    assert calls == [tmp_path]
    assert first.readonly == policy.host_readonly_paths(tmp_path)
    assert first.hidden == policy.host_managed_store_dirs(tmp_path)
    assert first.patterns == policy.host_readonly_patterns(tmp_path)
    assert first.directory_write_patterns == policy.host_readonly_patterns(tmp_path, directory_operation="write")
    assert first.directory_unlink_patterns == policy.host_readonly_patterns(tmp_path, directory_operation="unlink")
    with pytest.raises(FrozenInstanceError):
        first.hidden = ()
    other = tmp_path / "owners/providers/im/users/new"
    private = other / "data/private_interactions"
    private.mkdir(parents=True)
    calls.clear()
    second = policy.host_sandbox_paths(tmp_path, system="Linux")
    assert calls == [tmp_path]
    assert private in second.hidden and private not in first.hidden
    assert other / "config" in second.readonly
    assert second.patterns == second.directory_write_patterns == second.directory_unlink_patterns == ()
    calls.clear()
    scoped = policy.host_sandbox_paths(tmp_path, main, system="Darwin")
    assert calls == [] and other / "config" not in scoped.readonly


# LLM: 替换沙箱argv构建而非权限投影，观察实际shell准备阶段枚举次数。
# 函数用途: 验证Full Access一次准备只生成一份owner投影。
def test_shell_prepares_host_paths_once(tmp_path, monkeypatch):
    from agent_py_agent.agent import path_access_policy as policy
    from agent_py_agent.agent.attempt import sandbox
    from agent_py_agent.agent.tooling import shell

    main = tmp_path / "owners/local/main"
    main.mkdir(parents=True)
    calls, original = [], policy._existing_owner_homes

    # LLM: 不替换真实发现结果，只计数。
    # 函数用途: 观察shell调用的owner目录枚举。
    def homes(root):
        calls.append(root)
        return original(root)

    monkeypatch.setattr(policy, "_existing_owner_homes", homes)
    monkeypatch.setattr(sandbox.AttemptExecutionSandbox, "build_argv", lambda self, argv: list(argv))
    shell._sandbox_exec("true", main, None, main)
    assert calls == [tmp_path]


# LLM: 不运行shell；只创建本测试拥有的两条真实管道，调用方负责关闭写端。
# 函数用途: 提供可精确控制EOF的管道对象。
def _pipes():
    from types import SimpleNamespace

    out, out_writer = os.pipe()
    err, err_writer = os.pipe()
    return SimpleNamespace(stdout=os.fdopen(out, "rb"), stderr=os.fdopen(err, "rb")), (out_writer, err_writer)


# LLM: spy只记录真实selector等待期限；空闲等待必须是无超时的单次select，不是10ms空醒。
# 函数用途: 验证空管道阻塞与显式停止唤醒共用一个selector。
@pytest.mark.skipif(os.name != "posix", reason="POSIX selector contract")
def test_idle_selector_blocks_until_wake(monkeypatch):
    from agent_py_agent.agent.tooling import process_pipe_pump as pump

    proc, writers = _pipes()
    entered, timeouts = threading.Event(), []
    original = pump.selectors.DefaultSelector

    # LLM: 真实selector继续管理所有FD，仅插入无副作用计数。
    # 函数用途: 观察每次select使用的期限。
    def selector():
        instance = original()
        select = instance.select

        # LLM: 不缩短等待、不伪造事件，通知主线程已进入阻塞。
        # 函数用途: 记录reader是否固定短超时空醒。
        def observed(timeout=None):
            timeouts.append(timeout)
            entered.set()
            return select(timeout)

        instance.select = observed
        return instance

    monkeypatch.setattr(pump.selectors, "DefaultSelector", selector)
    capture = ProcessOutputCapture(proc)
    try:
        assert entered.wait(1)
        assert not capture.finish(.03)
        assert timeouts == [None]
        assert proc.stdout.closed and proc.stderr.closed
    finally:
        capture.finish(0)
        for writer in writers:
            os.close(writer)


# LLM: 只对测试stdout注入一次EAGAIN，之后真实短读；EOF不能由EAGAIN或单字节读取推断。
# 函数用途: 验证部分读取、EAGAIN和真实EOF分别处理。
@pytest.mark.skipif(os.name != "posix", reason="POSIX nonblocking pipe contract")
def test_partial_reads_and_eagain_are_not_eof(monkeypatch):
    from agent_py_agent.agent.tooling import process_pipe_pump as pump

    proc, writers = _pipes()
    fd, original, calls = proc.stdout.fileno(), os.read, []

    # LLM: 其它FD特别是wake管道不被替换，确保停机机制真实执行。
    # 函数用途: 模拟可读事件后的EAGAIN以及后续部分数据。
    def read(descriptor, count):
        if descriptor == fd:
            calls.append(count)
            if len(calls) == 1:
                raise BlockingIOError()
            return original(descriptor, 1)
        return original(descriptor, count)

    monkeypatch.setattr(pump.os, "read", read)
    os.write(writers[0], b"ab")
    for writer in writers:
        os.close(writer)
    capture = ProcessOutputCapture(proc)
    assert capture.finish(1)
    out, err, facts = capture.result()
    assert out == "ab" and err == "" and facts["complete"]
    assert facts["bytes_seen"] == {"stdout": 2, "stderr": 0} and not facts["errors"]


# LLM: 一条流EOF不停止另一条reader，更不表示进程退出；只检查capture自己的事实。
# 函数用途: 验证单流结束后stderr仍可继续输出。
def test_stdout_eof_does_not_stop_stderr():
    proc, writers = _pipes()
    capture = ProcessOutputCapture(proc)
    try:
        generation = capture.generation
        os.close(writers[0])
        deadline = time.monotonic() + 1
        while not capture.result()[2]["streams_eof"]["stdout"] and time.monotonic() < deadline:
            generation = capture.wait_changed(.1, generation=generation)
        assert capture.result()[2]["streams_eof"] == {"stdout": True, "stderr": False}
        assert not capture.done()
        os.write(writers[1], b"still-open")
    finally:
        os.close(writers[1])
        capture.finish(1)
    assert capture.result()[1] == "still-open"


# LLM: 仅替换采集器的平台分派，使用真实管道；此用例不冒充真实Windows机器验证。
# 函数用途: 锁住Windows兼容分支仍启动双reader且读到两流EOF。
def test_windows_compatibility_branch_keeps_two_readers(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling import process_output_capture as module

    proc, writers = _pipes()
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt", read=os.read, set_blocking=os.set_blocking))
    capture = ProcessOutputCapture(proc)
    try:
        assert len(capture._threads) == 2 and capture._pump is None
        os.write(writers[0], b"out")
        os.write(writers[1], b"err")
    finally:
        for writer in writers:
            os.close(writer)
    assert capture.finish(1) and capture.result()[:2] == ("out", "err")


# LLM: 仅注入第二个wake FD初始化失败；无论失败与否都关闭测试自建FD，不污染后续测试。
# 函数用途: 验证pump初始化异常不会泄漏已创建的自管道。
def test_pump_initialization_failure_closes_wake_fds(monkeypatch):
    from agent_py_agent.agent.tooling import process_pipe_pump as pump

    descriptors, calls, original_pipe = [], [], os.pipe

    # LLM: 真实创建自管道并记录本次描述符，不扫描宿主FD。
    # 函数用途: 观察失败路径中应该关闭的FD。
    def pipe():
        pair = original_pipe()
        descriptors.extend(pair)
        return pair

    # LLM: 仅第二次初始化抛原异常，不干扰其它文件操作。
    # 函数用途: 模拟已有一半初始化完成后失败。
    def blocking(fd, value):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError("set_blocking failure")

    monkeypatch.setattr(pump.os, "pipe", pipe)
    monkeypatch.setattr(pump.os, "set_blocking", blocking)
    try:
        with pytest.raises(OSError, match="set_blocking failure"):
            pump.ProcessPipePump(None, {})
        for fd in descriptors:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        for fd in descriptors:
            try:
                os.close(fd)
            except OSError:
                pass


# LLM: 不伪造真实进程已杀；这里只核对初始化失败必须调用原身份终止入口且关闭本次管道。
# 函数用途: 锁住shell在capture构造失败时的原异常与回收顺序。
def test_capture_initialization_failure_uses_original_cleanup(monkeypatch):
    from agent_py_agent.agent.tooling import shell

    proc, writers = _pipes()
    proc.pid = 12345
    calls, failure = [], OSError("capture initialization failed")

    # LLM: 在任何reader启动之前失败，保留异常对象作身份比较。
    # 函数用途: 模拟业务进程已启动后的采集构造失败。
    def capture(_):
        raise failure

    monkeypatch.setattr(shell, "ProcessOutputCapture", capture)
    monkeypatch.setattr(shell, "capture_process_birth_token", lambda pid: "launch-birth")
    monkeypatch.setattr(shell, "_kill_process_group", lambda *args: calls.append(args))
    try:
        with pytest.raises(OSError) as caught:
            shell._communicate_process(proc, command="capture-init", timeout=1)
        assert caught.value is failure
        assert calls == [(proc, "launch-birth")]
        assert proc.stdout.closed and proc.stderr.closed
    finally:
        proc.stdout.close()
        proc.stderr.close()
        for writer in writers:
            os.close(writer)


# LLM: 受控时间片内退出且无新generation，事件等待后仍需重新核对结果；同步attempt允许wait/reap。
# 函数用途: 复现两流提前EOF后末等待片正常退出被误报未完成的问题。
def test_attempt_rechecks_exit_after_eof_wait():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from agent_py_agent.agent.attempt import process_run

    proc = Mock(returncode=None)

    # LLM: 退出发生在观察片中，不产生输出事件。
    # 函数用途: 模拟该等待片内返回真实退出码。
    def exited(*args, **kwargs):
        proc.returncode = 7
        return 7

    proc.poll.side_effect = lambda: proc.returncode
    proc.wait.side_effect = exited
    capture = SimpleNamespace(generation=0, done=lambda: True, wait_changed=exited)
    state = process_run._ProcessRun(proc, .01)
    state.capture = capture
    assert process_run._wait_slice(state, .05) is True
    assert proc.returncode == 7


# LLM: 原版就应通过的空ready回归；注册完成后再拦备份writer/fsync，不误伤登记表本身的耐久追加。
# 函数用途: 确认空表、全非ready、全tool_output不会新建备份或刷盘。
@pytest.mark.parametrize("selection", ["empty", "not_ready", "tool_output"])
def test_empty_preimage_selection_never_writes_backup(tmp_path, monkeypatch, selection):
    from agent_py_agent.agent.artifacts import registry, shell_protection

    root, backup = tmp_path / "task", tmp_path / "backups"
    root.mkdir()
    if selection != "empty":
        output = root / "out.txt"
        output.write_text("same content")
        metadata = {registry.ARTIFACT_ROLE_METADATA_KEY: registry.ARTIFACT_ROLE_TOOL_OUTPUT_ARCHIVE} if selection == "tool_output" else {}
        registry.register_artifact(registry.ArtifactRegistration(
            root, output, artifact_id="a", status="ready" if selection == "tool_output" else "missing", metadata=metadata))

    # LLM: 只在无备份应发生的观察范围内注入失败，不能削弱产品的fsync顺序。
    # 函数用途: 把任何不必要的备份/刷盘转成测试失败。
    def unexpected(*args, **kwargs):
        pytest.fail("empty selection must not create or sync a backup")

    monkeypatch.setattr(shell_protection, "_copy_ready_snapshot", unexpected)
    monkeypatch.setattr(shell_protection.os, "fsync", unexpected)
    assert shell_protection.snapshot_ready_artifacts(root, backup) == []
    assert not backup.exists()
