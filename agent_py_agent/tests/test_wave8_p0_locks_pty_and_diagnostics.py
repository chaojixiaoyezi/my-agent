# LLM: P0回归使用真实文件锁、执行器与配置投影；只替换进程启动等外部边界，不削弱生产权限或回收断言。
# 模块用途: 验证路径锁寿命、缓存上限、PTY隔离、任务日志隔离及不含正文的内存诊断。
from __future__ import annotations

import gc
import json
import logging
import threading
import time
import tracemalloc
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.gateway_parts import background_resource_report as resources
from agent_py_agent.agent.gateway_parts import io as gateway_io
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.services.runtime_config_task import (
    apply_task_runtime_config_overlay,
)
from agent_py_agent.agent.tooling._filesystem_read import ReadFileTool
from agent_py_agent.agent.user_space import home_index_compact as compact
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call


# LLM: 工厂必须返回共同弱表中的包装而不是裸锁；路径别名共享同一临界区。
# 函数用途: 确认公共路径锁复用与弱引用寿命合同。
def test_path_thread_lock_is_public_and_shared(tmp_path):
    factory = getattr(json_io, "path_thread_lock", None)
    assert callable(factory), "缺少公共路径锁工厂"
    assert "path_thread_lock" in json_io.__all__
    handle = factory(tmp_path / "state.json")
    assert factory(tmp_path / "." / "state.json") is handle
    assert weakref.ref(handle)() is handle
    with handle.lock:
        with gateway_io.try_locked_file_transition(tmp_path / "state.json") as acquired:
            assert acquired is False


# LLM: 64个真实工作线程同时读改写同一文件；额外让出GIL放大无锁丢更新窗口。
# 函数用途: 核对Gateway事务锁在并发下没有丢更新。
def test_gateway_transition_64_threads_have_no_lost_updates(tmp_path):
    path = tmp_path / "counter.json"
    path.write_text('{"count":0}', encoding="utf-8")
    barrier = threading.Barrier(64)

    # LLM: 每个任务必须实际进入同一文件事务；异常通过future.result传回，不把线程退出当成功。
    # 函数用途: 等所有线程就绪后执行一次真实原子计数更新。
    def increment(_):
        barrier.wait(timeout=15)
        with gateway_io.locked_file_transition(path):
            value = json.loads(path.read_text(encoding="utf-8"))["count"]
            time.sleep(0.001)
            path.write_text(json.dumps({"count": value + 1}), encoding="utf-8")

    with ThreadPoolExecutor(max_workers=64) as executor:
        list(executor.map(increment, range(64)))
    assert json.loads(path.read_text(encoding="utf-8")) == {"count": 64}


# LLM: 活跃包装必须被强持有，最后一个引用消失后弱表应摘除全部2000个历史路径。
# 函数用途: 验证锁表不会随历史文件数量永久增长。
def test_2000_path_locks_are_collected_after_release(tmp_path):
    factory = getattr(json_io, "path_thread_lock", None)
    assert callable(factory), "缺少公共路径锁工厂"
    cache = getattr(json_io, "_PATH_LOCKS", None)
    assert isinstance(cache, weakref.WeakValueDictionary)
    handles = [factory(tmp_path / f"file-{index}.json") for index in range(2000)]
    keys = {str((tmp_path / f"file-{index}.json").resolve()) for index in range(2000)}
    refs = [weakref.ref(handle) for handle in handles]
    assert keys.issubset(cache)
    handles.clear()
    gc.collect()
    assert all(ref() is None for ref in refs)
    assert keys.isdisjoint(cache)


# LLM: 两个模块必须共用线程锁，即使OS锁层被测试替身移除也不能互相闯入。
# 函数用途: 持有common锁时，Gateway非阻塞入口必须拒绝竞争且保留原锁。
def test_gateway_nonblocking_uses_common_lock_and_keeps_handle_alive(tmp_path):
    factory = getattr(json_io, "path_thread_lock", None)
    assert callable(factory)
    path = tmp_path / "shared.json"
    with json_io.locked_json_path(path):
        gc.collect()
        with gateway_io.try_locked_file_transition(path) as acquired:
            assert not acquired
    with gateway_io.try_locked_file_transition(path) as acquired:
        assert acquired
        handle = weakref.ref(factory(path))
        gc.collect()
        assert handle() is not None
    gc.collect()
    assert handle() is None


# LLM: 每次sidecar载入均走生产入口，容量和成对淘汰要在600个不同路径后仍成立。
# 函数用途: 验证压缩冷却缓存最多512项且新记录保留、旧记录先淘汰。
def test_compact_state_cache_is_bounded_and_paired(tmp_path, monkeypatch):
    monkeypatch.setattr(compact, "_LAST_COMPACT_BYTES", {})
    monkeypatch.setattr(compact, "_LAST_COMPACT_AT", {})
    paths = [tmp_path / f"index-{index}.jsonl" for index in range(600)]
    entries = {str(path): {"bytes": index, "at": float(index)} for index, path in enumerate(paths)}
    (tmp_path / "compact_state.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")
    for path in paths:
        compact.load_compact_state(path)
    assert len(compact._LAST_COMPACT_BYTES) == 512
    assert set(compact._LAST_COMPACT_BYTES) == set(compact._LAST_COMPACT_AT)
    assert str(paths[0]) not in compact._LAST_COMPACT_BYTES
    assert compact._LAST_COMPACT_BYTES[str(paths[-1])] == 599


# LLM: 损坏的派生记录不能只写入bytes留下半对；缺失/损坏冷却仍保持无记录语义。
# 函数用途: 确认解析失败不会污染压缩内存缓存。
def test_invalid_compact_record_does_not_write_half_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(compact, "_LAST_COMPACT_BYTES", {})
    monkeypatch.setattr(compact, "_LAST_COMPACT_AT", {})
    path = tmp_path / "index.jsonl"
    (tmp_path / "compact_state.json").write_text(json.dumps({
        "entries": {str(path): {"bytes": 42, "at": "invalid"}},
    }), encoding="utf-8")
    compact.load_compact_state(path)
    assert str(path) not in compact._LAST_COMPACT_BYTES
    assert str(path) not in compact._LAST_COMPACT_AT


# LLM: 使用真实任务overlay文件和合并入口，finally恢复测试原日志级别，不将配置投影mock掉。
# 函数用途: 验证子代理debug覆盖不能修改整个Gateway的logger级别。
def test_task_debug_overlay_does_not_change_global_logger(tmp_path):
    (tmp_path / "overlay.yaml").write_text("log_level: debug\n", encoding="utf-8")
    task = SimpleNamespace(runtime_identity=SimpleNamespace(config_overlay_ref="overlay.yaml", config_scope="run"))
    base = AgentConfig()
    logger = logging.getLogger("agent_py_agent")
    before = logger.level
    logger.setLevel(logging.WARNING)
    try:
        effective = apply_task_runtime_config_overlay(base, task, workspace_root=tmp_path)
        assert effective.log_level == "debug"
        assert logger.level == logging.WARNING
        assert base.log_level != "debug"
    finally:
        logger.setLevel(before)


# LLM: owner差异从宿主上下文与operation_owner_id经过真实执行器进入检查，不从模型参数读取。
# 函数用途: 验证错owner请求在文件handler前明确拒绝。
def test_executor_rejects_trusted_owner_mismatch_before_handler(tmp_path):
    (tmp_path / "visible.txt").write_text("visible", encoding="utf-8")
    execution = execute_canonical_test_call(
        tmp_path, tools={"read_file": ReadFileTool(tmp_path, 4000)}, tool_name="read_file",
        arguments={"path": "visible.txt"}, operation_owner_id="owner-a",
        trusted_run_context={"owner_id": "owner-b"},
    )
    assert not execution.result.ok
    assert execution.result.error_code == "TOOL_PERMISSION_DENIED"
    assert not execution.result.handler_executed
    assert "owner_mismatch" in execution.result.content_blocks[0].text


# LLM: 缺失身份保持旧入口兼容，匹配身份正常执行；模型伪造的owner字段不能成为宿主来源。
# 函数用途: 验证新增检查不会阻断匹配或未声明的合法调用。
@pytest.mark.parametrize("trusted", [
    None, {"owner_id": "owner-a"}, {"owner_id": ""},
    {"run_scope": {"owner_id": "old-non-authoritative-owner"}},
])
def test_executor_matching_or_missing_owner_keeps_read_path(tmp_path, trusted):
    (tmp_path / "visible.txt").write_text("visible", encoding="utf-8")
    execution = execute_canonical_test_call(
        tmp_path, tools={"read_file": ReadFileTool(tmp_path, 4000)}, tool_name="read_file",
        arguments={"path": "visible.txt"}, operation_owner_id="owner-a", trusted_run_context=trusted,
    )
    assert execution.result.ok, execution.result.content_blocks[0].text


# LLM: 默认字段必须同时属于真实dataclass与随包YAML，诊断默认关闭而非自动开启tracemalloc。
# 函数用途: 核对两处配置默认值一致。
def test_memory_diagnostics_config_defaults_match_yaml():
    import yaml

    config = AgentConfig()
    assert getattr(config, "enable_gateway_memory_diagnostics", None) is False
    assert getattr(config, "gateway_owner_inflight_limit", None) == 0
    defaults = yaml.safe_load((Path(__file__).parents[1] / "config" / "agent_config.yaml").read_text(encoding="utf-8"))
    assert defaults["enable_gateway_memory_diagnostics"] is False
    assert defaults["gateway_owner_inflight_limit"] == 0


# LLM: 诊断只返回计数与ru_maxrss峰值口径；默认不得启动追踪或泄露池内对象。
# 函数用途: 验证轻量内存诊断和公开池计数。
def test_memory_diagnostics_returns_peak_rss_gc_and_pool_gauges(monkeypatch):
    collector = getattr(resources, "collect_runtime_memory_diagnostics", None)
    assert callable(collector), "缺少内存诊断入口"
    monkeypatch.setattr(tracemalloc, "is_tracing", lambda: False)
    pool = SimpleNamespace(hard_count=3, soft_count=2, retired_count=1, pinned_count=1)
    report = collector(owner_pool=pool)
    assert report["rss_peak_bytes"] > 0
    assert report["rss_source"] == "resource.ru_maxrss (peak)"
    assert len(report["gc_counts"]) == 3
    assert report["owner_pool"] == {"hard_count": 3, "soft_count": 2, "retired_count": 1, "pinned_count": 1}
    assert "tracemalloc_top" not in report


# LLM: 真实owner池没有公开count属性，需在原锁内仅投影四份容器数量，不创建或repr任何Agent。
# 函数用途: 验证现有OwnerScopedAgentPool也能被诊断。
def test_memory_diagnostics_reads_actual_owner_pool_counts(tmp_path):
    from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool

    collector = getattr(resources, "collect_runtime_memory_diagnostics", None)
    assert callable(collector)
    pool = OwnerScopedAgentPool(SimpleNamespace(owner_agent_pool_max_agents=64), tmp_path)
    pool._hard_agents["a"] = object()
    pool._soft_agents["b"] = object()
    pool._retired_agents[1] = object()
    pool._pins[1] = 2
    report = collector(owner_pool=pool)
    assert report["owner_pool"] == {"hard_count": 1, "soft_count": 1, "retired_count": 1, "pinned_count": 1}


# LLM: 显式快照最多top_n行，且不能留下本次启动的追踪；原本已有追踪则不能擅自关闭。
# 函数用途: 验证按需tracemalloc仅输出位置与大小，不带源码、正文或对象repr。
def test_memory_diagnostics_tracemalloc_is_bounded_and_preserves_trace_owner():
    collector = getattr(resources, "collect_runtime_memory_diagnostics", None)
    assert callable(collector)
    was_tracing = tracemalloc.is_tracing()
    report = collector(include_tracemalloc=True, top_n=3)
    assert tracemalloc.is_tracing() is was_tracing
    assert 0 < len(report["tracemalloc_top"]) <= 3
    for row in report["tracemalloc_top"]:
        assert set(row) == {"location", "size_bytes"}
        assert int(row["location"].rsplit(":", 1)[1]) >= 0
        assert row["size_bytes"] >= 0
        assert "/" not in row["location"]
    assert "diagnostic-private-content" not in json.dumps(report)


# LLM: 外部开启的追踪即使include=False也应提供有界统计，调用完仍在追踪。
# 函数用途: 确认诊断不占有或关闭已存在的全局追踪会话。
def test_memory_diagnostics_preserves_existing_tracemalloc():
    collector = getattr(resources, "collect_runtime_memory_diagnostics", None)
    assert callable(collector)
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    try:
        secret = bytearray(b"diagnostic-private-content" * 100)
        report = collector(top_n=2)
        assert tracemalloc.is_tracing()
        assert len(report["tracemalloc_top"]) <= 2
        assert bytes(secret).decode() not in json.dumps(report)
    finally:
        if not was_tracing:
            tracemalloc.stop()


# LLM: 注册表使用真实装配参数，禁止把构造ToolRegistry换成替身而漏掉原全局setter。
# 函数用途: 创建指定PTY超时的真实工具注册表。
def _registry_with_pty_timeout(root, minutes):
    from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

    return ToolRegistry(ToolRegistryParams(
        workspace_root=root, max_chars=4000, max_entries=50, max_matches=50,
        web_max_chars=4000, http_timeout=5, catalog_limit=20, retrieval_limit=10,
        vector_search_enabled=False, pty_session_idle_timeout_minutes=minutes,
    ))


# LLM: 只替换外部Popen和线程执行，保留真实start/_spawn/装配/会话登记，实际PTY FD均在fixture退出时归还。
# 函数用途: 给纯超时与线程启动单元测试提供无真实子进程的PTY运行边界。
@pytest.fixture
def _pty_probe(monkeypatch):
    import os
    from contextlib import nullcontext

    from agent_py_agent.agent.tooling import pty_sessions as pty
    from agent_py_agent.agent.tooling import registry_bootstrap as bootstrap

    threads = []

    # LLM: 不执行目标函数，只记录生产代码请求启动的线程；is_alive准确区分start前后状态。
    # 类用途: 观察巡检与排空线程的创建，不让测试遗留后台线程。
    class ProbeThread:
        # LLM: 接受真实Thread使用的构造参数，保留名称和daemon事实。
        # 函数用途: 初始化受控线程状态。
        def __init__(self, target=None, args=(), name="", daemon=None):
            self.name, self.daemon, self.alive = name, daemon, False

        # LLM: 不依赖外部时钟或调度。
        # 函数用途: 返回受控线程是否已启动。
        def is_alive(self):
            return self.alive

        # LLM: 不启动真正线程，避免把单元边界误报成后台巡检实跑。
        # 函数用途: 记录一次真实启动请求。
        def start(self):
            self.alive = True
            threads.append(self)

    registry = pty.PtySessionRegistry()
    monkeypatch.setattr(pty, "threading", SimpleNamespace(Thread=ProbeThread, Lock=threading.Lock))
    monkeypatch.setattr(pty, "pty_session_registry", registry)
    monkeypatch.setattr(bootstrap, "pty_session_registry", registry, raising=False)
    monkeypatch.setattr(pty, "source_process_boundary", lambda **_kw: nullcontext())
    monkeypatch.setattr(pty, "process_source_configurations", lambda *_args, **_kw: ())
    monkeypatch.setattr(pty, "_subprocess_text_env", lambda *_args, **_kw: {})
    monkeypatch.setattr(pty, "_sandbox_exec", lambda command, *_args, **_kw: (command, False))
    monkeypatch.setattr(pty.subprocess, "Popen", lambda *_args, **_kw: SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(pty, "PtyOwnedProcess", lambda process: process)
    try:
        yield registry, threads
    finally:
        for session in registry._sessions.values():
            for descriptor in (session.master_fd, session.tail_hold_fd):
                if descriptor >= 0:
                    os.close(descriptor)
        registry._sessions.clear()


# LLM: 两个真实ToolRegistry共享进程注册表，但构造与零超时会话不启动巡检，正超时会话才启动唯一线程。
# 函数用途: 验证30/0分钟配置完整传到各自会话，互不覆盖。
def test_two_registries_isolate_timeouts_and_start_sweeper_lazily(tmp_path, _pty_probe):
    registry, threads = _pty_probe
    first = _registry_with_pty_timeout(tmp_path, 30)
    second = _registry_with_pty_timeout(tmp_path, 0)
    assert registry._idle_thread is None
    assert registry.idle_timeout_seconds == 0.0
    zero = second.tools["terminal_session"].execute({"action": "start", "command": "python -q"})
    assert zero.ok, zero.output
    assert registry._idle_thread is None
    positive = first.tools["terminal_session"].execute({"action": "start", "command": "python -q"})
    assert positive.ok, positive.output
    zero_session = registry._sessions[json.loads(zero.output)["session_id"]]
    positive_session = registry._sessions[json.loads(positive.output)["session_id"]]
    assert zero_session.idle_timeout_seconds == 0.0
    assert positive_session.idle_timeout_seconds == 1800.0
    assert registry._idle_thread is not None
    assert [thread.name for thread in threads].count("pty-idle-sweeper") == 1
    registry.set_idle_timeout_minutes(0)
    assert positive_session.idle_timeout_seconds == 1800.0


# LLM: 显式零值仅跳过当前会话；全局默认零值不能跳过带正覆盖的其它会话。
# 函数用途: 验证混合会话按自己的有效超时选取，None仍继承默认。
@pytest.mark.parametrize("fallback", [0.0, 60.0])
def test_sweep_selects_each_session_timeout_independently(monkeypatch, fallback):
    from agent_py_agent.agent.tooling import pty_sessions as pty

    assert "idle_timeout_seconds" in pty.PtySession.__dataclass_fields__
    registry = pty.PtySessionRegistry()
    registry.idle_timeout_seconds = fallback
    for name, timeout in (("disabled", 0.0), ("override", 1.0), ("fallback", None)):
        registry._sessions[name] = pty.PtySession(
            session_id=name, command="unit-test", process=SimpleNamespace(poll=lambda: None),
            master_fd=-1, started_at=0.0, last_active_at=0.0,
            access_scope=pty._pty_access_scope(None, None), idle_timeout_seconds=timeout,
        )
    closed = []
    monkeypatch.setattr(registry, "close", closed.append)
    expected = {"override", "fallback"} if fallback else {"override"}
    assert set(registry.sweep_idle(now=1000.0)) == expected
    assert set(closed) == expected


# LLM: 给已有None超时会话启用默认值应补启动巡检，但空注册表配置不能起线程。
# 函数用途: 保留直接setter兼容性并验证其惰性启动约束。
def test_configure_timeout_without_sessions_does_not_start_thread(_pty_probe):
    registry, threads = _pty_probe
    registry.set_idle_timeout_minutes(30)
    assert registry._idle_thread is None
    assert threads == []


# LLM: 请求缺省owner_scope_root时，旧handler的owner墙必须继续有效，workspace_roots不授穿墙权限。
# 函数用途: 确认新增身份检查没有擦掉原配置目录墙fallback。
def test_missing_request_owner_fence_preserves_configured_policy(tmp_path):
    from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions

    own = tmp_path / "owner-a"
    other = tmp_path / "owner-b"
    own.mkdir()
    other.mkdir()
    (other / "private.txt").write_text("other-owner-private", encoding="utf-8")
    tool = ReadFileTool(own, 4000, [own, other], FileSystemAccessOptions(owner_scope_root=str(own)))
    execution = execute_canonical_test_call(
        own, tools={"read_file": tool}, tool_name="read_file",
        arguments={"path": str(other / "private.txt")}, workspace_roots=(own, other), owner_scope_root="",
    )
    assert not execution.result.ok
    assert "other-owner-private" not in execution.result.content_blocks[0].text


# LLM: 新解释器用当前树真实导入和构造，不用线程替身，把零sweeper要求覆盖到真实启动入口。
# 函数用途: 确认导入PTY模块并构造两个配置不同的ToolRegistry不启动巡检线程。
def test_fresh_interpreter_registry_construction_has_no_sweeper():
    import subprocess
    import sys

    program = """
import json, threading
from pathlib import Path
import agent_py_agent.agent.tooling.pty_sessions as pty
from agent_py_agent.tests.test_wave8_p0_locks_pty_and_diagnostics import _registry_with_pty_timeout
assert Path(pty.__file__).resolve().is_relative_to(Path.cwd().resolve())
_registry_with_pty_timeout(Path.cwd(), 30)
_registry_with_pty_timeout(Path.cwd(), 0)
print(json.dumps([t.name for t in threading.enumerate() if t.name == 'pty-idle-sweeper']))
"""
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=Path(__file__).parents[2], text=True,
        capture_output=True, timeout=30, check=True,
    )
    assert json.loads(result.stdout) == []
