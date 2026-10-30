# LLM: 只在临时文件与替身进程边界验证Gateway缺陷，不启动用户服务或访问供应商。
# 模块用途: 覆盖多值头、默认准入、缓存隔离、后台ID投影和安全自举。
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from websockets.datastructures import Headers

from agent_py_agent.agent.backends import rejection_diagnostics as diagnostics
from agent_py_agent.agent.backends.responses_ws_pool import ResponsesSessionPool
from agent_py_agent.agent.conversation import ConversationStore, background_context, store_threads
from agent_py_agent.agent.gateway_parts import request_worker
from agent_py_agent.cli import gateway_process


# LLM: 使用真实websockets多值容器而非普通dict；异常转断言失败以准确记录红绿结果。
# 函数用途: 双Cookie不能炸掉路由状态捕获或拒绝诊断。
@pytest.mark.parametrize("operation", ["capture", "rejection", "raw"])
def test_real_multivalue_headers(operation):
    headers = Headers([("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"),
                       ("x-codex-turn-state", "tok123"), ("x-request-id", "req-test"),
                       ("Content-Type", "application/json")])
    pool = ResponsesSessionPool()
    key = ("test", "session")
    state = pool.state(key, 30, 2)
    try:
        if operation == "capture":
            pool.capture(key, state, headers)
            assert state.turn_state_header == "tok123"
        elif operation == "rejection":
            assert diagnostics.rejection_headers(headers) == ({"x-request-id": "req-test"}, False)
        else:
            assert diagnostics._raw_header(headers, "content-type") == "application/json"
    except Exception as exc:
        pytest.fail(f"multi-value response headers crashed: {type(exc).__name__}: {exc}")
    finally:
        pool.close()


# LLM: 显式小上限不被新默认覆盖；缺失和无效配置采用同一50回退。
# 函数用途: 保证配置缺省不会再把第九个会话卡住。
def test_admission_default_fallback_is_fifty():
    for config in [SimpleNamespace(), SimpleNamespace(gateway_user_inflight_limit="invalid")]:
        assert request_worker.AdmissionLimits.from_config(config).user_inflight == 50
    assert request_worker.AdmissionLimits.from_config(SimpleNamespace(gateway_user_inflight_limit=8)).user_inflight == 8


# LLM: 真实store热读取仍给嵌套可变字段独立副本；统计标准库deepcopy调用而非声称CPU降幅。
# 函数用途: 避免每次缓存命中走九次通用递归分发，同时守住修改隔离。
def test_thread_cache_avoids_generic_deepcopy_and_isolates_nested_values(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner"})
    thread = replace(thread, metadata={"nested": [{"value": 1}]},
                     pending_host_notices=({"nested": [1]},), conversation_methods=({"nested": [2]},))
    expected = store_threads._clone_cached_thread(thread)
    copier = Mock(wraps=store_threads.copy.deepcopy)
    monkeypatch.setattr(store_threads.copy, "deepcopy", copier)
    result = store_threads._clone_cached_thread(thread)
    assert result == expected
    assert copier.call_count == 0
    result.metadata["nested"][0]["value"] = 3
    result.pending_host_notices[0]["nested"].append(9)
    result.conversation_methods[0]["nested"].append(9)
    assert thread.metadata["nested"][0]["value"] == 1
    assert thread.pending_host_notices[0]["nested"] == [1]
    assert thread.conversation_methods[0]["nested"] == [2]


# LLM: 只读谱系收集不得调用全任务深副本列表；未知管理器仍允许原接口。
# 函数用途: 后台仅收集标量ID时采用轻摘要并保持精确根过滤。
def test_context_ids_use_lightweight_run_summaries():
    manager = SimpleNamespace(load=lambda _: SimpleNamespace(root_id="root"),
        list_run_summaries=Mock(return_value=[SimpleNamespace(id="child", root_id="root"),
                                            SimpleNamespace(id="foreign", root_id="other")]),
        list_runs=Mock(side_effect=AssertionError("full copy scan must not run")))
    state = SimpleNamespace(task_id="current", agent=SimpleNamespace(subagents=manager))
    assert background_context._task_context_ids(state) == {"root", "current", "child"}
    manager.list_run_summaries.assert_called_once_with()
    manager.list_runs.assert_not_called()


# LLM: 不真正出生子进程，仅捕获Popen入口的环境和cwd；父环境必须原样保留。
# 函数用途: 从同名源码目录启动时子Gateway不再继承危险的cwd导入优先级。
def test_gateway_spawn_uses_safe_path_and_safe_cwd(tmp_path, monkeypatch):
    unsafe = tmp_path / "source"
    (unsafe / "agent_py_agent").mkdir(parents=True)
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.chdir(unsafe)
    monkeypatch.setenv("PYTHONSAFEPATH", "0")
    popen = Mock()
    monkeypatch.setattr(gateway_process.subprocess, "Popen", popen)
    gateway_process._spawn_gateway_process(SimpleNamespace(log=root / "gateway.log", root=root),
                                          ["python", "-m", "agent_py_agent"], cwd=unsafe)
    kwargs = popen.call_args.kwargs
    assert kwargs.get("env", {}).get("PYTHONSAFEPATH") == "1"
    assert Path(kwargs["cwd"]) == root
    assert gateway_process.os.environ["PYTHONSAFEPATH"] == "0"


# LLM: 公开run入口在冻结参数及延迟导入前清洗；不启动生命周期，测试异常只是停止探针。
# 函数用途: 保留实际包根和普通依赖，移除同名外来包以及空串cwd入口。
def test_gateway_run_removes_foreign_package_roots(tmp_path, monkeypatch):
    from agent_py_agent.cli import common

    foreign = tmp_path / "source"
    (foreign / "agent_py_agent").mkdir(parents=True)
    dependencies = tmp_path / "dependencies"
    dependencies.mkdir()
    installed = str(Path(gateway_process.__file__).resolve().parents[2])
    monkeypatch.chdir(foreign)
    monkeypatch.setattr(gateway_process.sys, "path", ["", str(foreign), installed, str(dependencies)])
    stop = RuntimeError("stop before lifecycle")
    monkeypatch.setattr(common, "frozen_gateway_arguments", Mock(side_effect=stop))
    with pytest.raises(RuntimeError, match="stop before lifecycle"):
        gateway_process.cmd_gateway_run(SimpleNamespace())
    assert gateway_process.sys.path == [installed, str(dependencies)]


# LLM: 两次死宿主恢复使用真实processing/inbox，重复扫描不能二次计数；高attempts不等于失败。
# 函数用途: 覆盖刚认领及启动探测阶段连续重启仍保留同一请求。
@pytest.mark.parametrize("has_identity", [False, True])
def test_two_consecutive_startup_recoveries_preserve_request(tmp_path, has_identity):
    from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.gateway_parts.recovery import (
        recover_gateway_processing_requests_report,
    )

    paths = gateway_paths_from_root(tmp_path)
    paths.processing.mkdir(parents=True)
    path = paths.processing / "req-double-restart.json"
    payload = {"id": path.stem, "kind": "ask", "goal": "继续原回合", "status": "processing",
               "attempts": 90, "turn_phase": "open", "processing_failure_count": 0}
    for restart in (1, 2):
        payload.update(status="processing", execution_attempt_id=f"attempt-{restart}")
        if has_identity:
            from agent_py_agent.agent.gateway_parts.daemon_metadata import process_host_id
            payload["lease_process_identity"] = {"host_id": process_host_id(), "pid": 999999999, "start_time": 1}
        write_json_file_atomic(path, payload)
        report = recover_gateway_processing_requests_report(paths, startup=True, max_attempts=2)
        assert report.summary["requeued"] == 1 and report.summary["failed"] == 0
        assert not report.load_errors
        assert not path.exists() and not (paths.terminal / path.name).exists()
        pending = paths.inbox / path.name
        payload = read_json_file(pending)
        assert payload["active_turn_recovery"]["unplanned_resume_count"] == restart
        assert payload["processing_failure_count"] == 0 and payload["attempts"] == 90
        assert payload["goal"] == "继续原回合"
        again = recover_gateway_processing_requests_report(paths, startup=True, max_attempts=2)
        assert again.summary["requeued"] == 0 and read_json_file(pending) == payload
        if restart == 1:
            pending.replace(path)


# LLM: 缺少target时仍须用durable前台ID插话，但不能伪造Gateway claim/执行代次。
# 函数用途: 绑定空窗不会退化为queued_inputs，空闲或未知ID不误发。
@pytest.mark.parametrize("running, request_id, accepted", [(True, "req_123", True), (False, "req_123", False), (True, "", False)])
def test_foreground_steer_binding_gap(tmp_path, monkeypatch, running, request_id, accepted):
    import threading

    from agent_py_agent.cli.chat_parts import tui_actions
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("steer-gap")
    reconciler = SimpleNamespace(enqueue=Mock())
    monkeypatch.setattr(tui_actions, "_ensure_active_input_reconciler", lambda _: reconciler)
    params = SimpleNamespace(use_gateway=True, tui_runtime=runtime, state_lock=threading.Lock(),
        is_running_ref=[running], running_request_id_ref=[request_id],
        agent=SimpleNamespace(request_active_turn_input=Mock()), current_session_id="session")
    options = SimpleNamespace(input_media=())
    result = tui_actions._tui_submit_active_turn_input(params, "补充", display_text="补充", execution_options=options)
    assert result is accepted
    if accepted:
        entry = reconciler.enqueue.call_args.args[0]
        assert entry.expected_turn_id == "req_123" and entry.text == "补充"
        assert entry.expected_execution is None, "missing claim must be revalidated by Gateway, not fabricated"
        assert not runtime.store.snapshot().queued_inputs
    else:
        reconciler.enqueue.assert_not_called()


# LLM: 本地running也包含等待准入，不能覆盖Gateway已发布的真实request/task执行目标与claim。
# 函数用途: 防止排队中的本地请求抢走已有执行者的插话投递目标。
@pytest.mark.parametrize("kind", ["task", "request"])
def test_valid_execution_target_not_overridden_by_waiting_request(monkeypatch, kind):
    import threading

    from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget
    from agent_py_agent.cli.chat_parts import tui_actions
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("steer-valid-target")
    target = GatewayExecutionTarget("thread-1", kind, "actual-turn", "actual-claim").to_dict()
    runtime.publish_execution_target(target)
    reconciler = SimpleNamespace(enqueue=Mock())
    monkeypatch.setattr(tui_actions, "_ensure_active_input_reconciler", lambda _: reconciler)
    params = SimpleNamespace(use_gateway=True, tui_runtime=runtime, state_lock=threading.Lock(),
        is_running_ref=[True], running_request_id_ref=["queued-request"],
        agent=SimpleNamespace(request_active_turn_input=Mock()), current_session_id="session")
    assert tui_actions._tui_submit_active_turn_input(params, "补充", display_text="补充",
                                                    execution_options=SimpleNamespace(input_media=()))
    entry = reconciler.enqueue.call_args.args[0]
    assert entry.expected_turn_id == "actual-turn"
    assert entry.expected_execution == target
    assert runtime.execution_target() == target
    assert not runtime.store.snapshot().queued_inputs


# LLM: 仅同一本地durable前台请求可保留旧claim投影；空闲和切换请求必须清掉，绝不保留后台task。
# 函数用途: 真实轮询提交遇瞬时空目标时不抹掉正在观察的前台绑定。
@pytest.mark.parametrize("running, request_id, retained", [(True, "req-1", True), (False, "req-1", False), (True, "req-2", False)])
def test_notice_null_target_preserves_only_observed_foreground(monkeypatch, running, request_id, retained):
    import threading

    from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget
    from agent_py_agent.cli.chat_parts import tui_threading
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("notice-gap")
    target = GatewayExecutionTarget("thread-1", "request", "req-1", "claim-1").to_dict()
    runtime.publish_execution_target(target)
    runtime._foreground_request_refs = (threading.Lock(), [running], [request_id])
    monkeypatch.setattr(tui_threading, "_publish_gateway_background_watch_binding", lambda *_: False)
    tui_threading._apply_gateway_background_snapshot(None, "session", runtime, [None], set(), [0], None,
                                                    {"ok": True, "execution_target": None}, 0)
    assert runtime.execution_target() == (target if retained else None)


# LLM: BaseException模拟进程消失而非可捕获IO失败；不发送信号或改真实队列。
# 类用途: 在认领与重排的两次磁盘操作之间中断测试。
class _QueueCrash(BaseException):
    pass


# LLM: 原claim/recovery真实入口注入两处崩溃；解除注入后两次扫描只搬运一次，恢复计数不重复增加。
# 函数用途: processing中的pending运输半态不能被静默漏收。
@pytest.mark.parametrize("window", ["claim", "requeue"])
def test_interrupted_pending_transport_is_recovered_once(tmp_path, monkeypatch, window):
    from agent_py_agent.agent.gateway_parts import daemon_metadata, queue_service, recovery
    from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path)
    queue_service.ensure_gateway_folders(paths)
    pending = paths.inbox / "req-transport.json"
    write_json_file_atomic(pending, {"id": pending.stem, "kind": "ask", "goal": "原请求", "status": "pending", "attempts": 90})
    processing = paths.processing / pending.name
    if window == "claim":
        with monkeypatch.context() as patch:
            patch.setattr(queue_service, "_gateway_claim_payload", Mock(side_effect=_QueueCrash()))
            with pytest.raises(_QueueCrash):
                queue_service.claim_request(paths, pending)
    else:
        queue_service.claim_request(paths, pending)
        payload = read_json_file(processing)
        payload["lease_process_identity"] = {"host_id": daemon_metadata.process_host_id(), "pid": 999999999, "start_time": 1}
        write_json_file_atomic(processing, payload)
        real_replace = Path.replace

        # LLM: 仅阻断精确processing→inbox原子rename，不影响临时写入或其它请求。
        # 函数用途: 模拟pending已落盘但尚未搬运时退出。
        def crash_on_requeue(path, target):
            if path == processing and Path(target) == pending:
                raise _QueueCrash()
            return real_replace(path, target)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "replace", crash_on_requeue)
            with pytest.raises(_QueueCrash):
                recovery.recover_gateway_processing_requests_report(paths, startup=True)
    saved = read_json_file(processing)
    assert saved["status"] == "pending"
    reports = [recovery.recover_gateway_processing_requests_report(paths, startup=True) for _ in range(2)]
    assert not any(report.load_errors for report in reports)
    assert pending.exists() and not processing.exists()
    assert read_json_file(pending) == saved
    assert sum(report.summary["requeued"] for report in reports) == 1


# LLM: 真实manager共用canonical指纹缓存，热摘要不得deepcopy或返回可变任务；跨manager保存必须下次可见。
# 函数用途: 验证轻摘要的复制成本、隔离、更新、删除与坏账报告。
def test_real_run_summaries_are_frozen_fresh_and_copy_free(tmp_path, monkeypatch):
    from dataclasses import FrozenInstanceError

    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.agent.subagents.services.persistence import service

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="摘要", thought="只取标量", plan=["读取"])
    first = manager.list_run_summaries_report()
    assert not first.load_errors and first.runs[0].id == task.id
    with pytest.raises(FrozenInstanceError):
        first.runs[0].status = "corrupted"
    # 模拟超过粗mtime保护期的热缓存；不修改产品的保护窗口。
    monkeypatch.setattr(service, "_now", lambda: service.time.time() + 60)
    manager.list_run_summaries()
    with monkeypatch.context() as patch:
        copier = Mock(side_effect=AssertionError("summary must not deepcopy"))
        patch.setattr(service.copy, "deepcopy", copier)
        assert manager.list_run_summaries() == first.runs
        copier.assert_not_called()
    other = SubAgentManager(tmp_path)
    task.status = "DONE"
    other.save(task)
    assert manager.list_run_summaries()[0].status == "DONE"
    assert first.runs[0].status != "DONE"
    # 只移走本测试自己的locator，模拟跨进程删除，不碰canonical正文。
    locator = tmp_path / task.id / "task.json"
    locator.replace(locator.with_suffix(".gone"))
    assert manager.list_run_summaries() == []
    broken = tmp_path / "broken" / "task.json"
    broken.parent.mkdir()
    broken.write_text("not-json", encoding="utf-8")
    report = manager.list_run_summaries_report()
    assert not report.runs and report.load_errors[0]["run_id"] == "broken"


# LLM: 原身份未知必须先等待租约到期；后续常规扫描也应完成同一pending运输，不消耗失败或续跑预算。
# 函数用途: 覆盖启动时未到期、稍后才变stale的半迁移记录。
def test_pending_transport_expiring_after_startup_is_recovered(tmp_path):
    import time

    from agent_py_agent.agent.gateway_parts import queue_service, recovery
    from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path)
    queue_service.ensure_gateway_folders(paths)
    path = paths.processing / "req-deferred.json"
    now = time.time()
    payload = {"id": path.stem, "kind": "ask", "status": "pending", "goal": "原请求",
               "lease_heartbeat_at": now, "lease_process_identity": {"pid": 999999999, "start_time": 1},
               "active_turn_recovery": {"unplanned_resume_count": 2}}
    write_json_file_atomic(path, payload)
    startup = recovery._RecoveryContext(now, 2, 60, True, None, None)
    first = recovery.recover_gateway_processing_requests_report(paths, params=startup)
    assert not first.load_errors and first.summary["requeued"] == 0 and path.exists()
    second = recovery.recover_gateway_processing_requests_report(paths, params=replace(startup, now=now + 70, startup=False))
    assert not second.load_errors and second.summary["requeued"] == 1
    assert not path.exists() and read_json_file(paths.inbox / path.name) == payload


# LLM: 半迁移不是绕过原权限/新鲜度/不可变身份的许可；拒绝时保留原文件和冲突记录，不制造终态。
# 函数用途: 保证搬运修复不覆盖新队列、不处理活宿主、不放行坏指纹与未知状态。
@pytest.mark.parametrize("case", ["conflict", "fingerprint", "live", "status", "managed"])
def test_pending_transport_keeps_original_safety_fences(tmp_path, monkeypatch, case):
    from agent_py_agent.agent.gateway_parts import daemon_metadata, queue_service, recovery
    from agent_py_agent.agent.gateway_parts.io import (
        gateway_request_fingerprint,
        read_json_file,
        write_json_file_atomic,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path)
    queue_service.ensure_gateway_folders(paths)
    path = paths.processing / "req-fenced.json"
    payload = {"id": path.stem, "kind": "ask", "status": "pending", "goal": "原请求"}
    if case == "fingerprint":
        payload["request_fingerprint"] = gateway_request_fingerprint(payload, path.stem)
        payload["request_fingerprint_schema"] = "gateway_request_fingerprint.v1"
        payload["goal"] = "被改动"
    if case == "live":
        payload["lease_process_identity"] = {"host_id": "test"}
        monkeypatch.setattr(daemon_metadata, "process_identity_is_live", lambda _: True)
    if case == "status":
        payload["status"] = "unknown"
    if case == "managed":
        payload["route"] = {"c_run_id": "old"}
    pending = paths.inbox / path.name
    conflict = {"id": path.stem, "kind": "ask", "goal": "不能覆盖"}
    if case == "conflict":
        write_json_file_atomic(pending, conflict)
    write_json_file_atomic(path, payload)
    report = recovery.recover_gateway_processing_requests_report(paths, startup=True)
    assert read_json_file(path) == payload and report.summary["requeued"] == 0
    assert not (paths.terminal / path.name).exists()
    if case == "conflict":
        assert read_json_file(pending) == conflict
    else:
        assert not pending.exists()
    if case in {"conflict", "fingerprint", "managed"}:
        assert report.load_errors
