"""决策服务故障矩阵：断网、DNS、TLS、额度、计费、服务错误、慢响应经真实传输栈的回退与恢复，以及同 owner 多会话并发。"""
from __future__ import annotations

import socket
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import model_call_summary
from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.conversation import decision_policy, decision_service
from agent_py_agent.agent.settings.model_profiles import model_profiles_path
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.tests.test_decision_service_http import configured, decide, server  # noqa: F401
from agent_py_agent.tests.test_decision_settings import patch

_DNS_HOST = "decision-fault.invalid"
# (故障, 首次结果, 冷却秒数；None 表示须改设置才重试, 冷却后或改设置后结果)
FAULTS = {
    "refused": ("error", 30, "error"),
    "dns": ("error", 30, "error"),
    "503": ("error", 30, "success"),
    "slow": ("deadline", 30, "success"),
    "429_quota": ("error", 300, "success"),
    "429_limit": ("error", 300, "success"),
    "402": ("configuration_required", None, "success"),
    "tls": ("configuration_required", None, "configuration_required"),
}


# LLM: 本机若配了 HTTP 代理（环境变量 HTTP_PROXY 等），非回环地址的请求会先连代理，注入的 DNS 故障根本走不到，
#   结果随机器而变（2026-09-25：开发机经 127.0.0.1:7890 代理拖到超时得到 deadline，CI 直连得到 error）。
#   清掉代理环境并让所有主机绕过代理，保证每台机器都经同一条直连传输栈。
# 函数用途: 让本文件的每个用例都不经本机代理。
@pytest.fixture(autouse=True)
def _direct_transport(monkeypatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")


# LLM: 只让一个保留域名解析失败，其余解析照旧；不向真实 resolver 发送该域名。返回的列表记录被拦截的解析次数，
#   用例据此证明注入的故障确实被走到，而不是被代理等其它路径绕过。
# 函数用途: 在测试进程内模拟 DNS 故障。
def _block_dns(monkeypatch) -> list[str]:
    original = socket.getaddrinfo
    hits: list[str] = []

    def resolve(host, *args, **kwargs):
        if host == _DNS_HOST:
            hits.append(host)
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    return hits


# 函数用途: 让服务指向一个刚释放、无人监听的本机端口。
def _refused(state, _monkeypatch) -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    state.url = f"http://127.0.0.1:{probe.getsockname()[1]}"
    probe.close()


# 函数用途: 让服务指向只在本进程内解析失败的保留域名，并把拦截记录挂到服务状态上供用例核对。
def _dns(state, monkeypatch) -> None:
    state.url = f"http://{_DNS_HOST}"
    state.dns_hits = _block_dns(monkeypatch)


# 函数用途: 在明文 HTTP 端口上发起 https，制造 TLS 握手失败。
def _tls(state, _monkeypatch) -> None:
    state.url = state.url.replace("http://", "https://")


# 函数用途: 让服务在放行前一直不回应，制造超时。
def _slow(state, _monkeypatch) -> None:
    state.block = True


_INJECT = {"refused": _refused, "dns": _dns, "tls": _tls, "slow": _slow}


# LLM: 只在测试进程内制造故障：关闭端口、拦截单一保留域名解析、明文端口上发 https、服务商错误体；不访问外网。
# 函数用途: 按故障名改写本地服务或解析；其余故障按 HTTP 状态码带服务商错误体返回。
def _inject(fault: str, state, monkeypatch) -> None:
    if fault in _INJECT:
        _INJECT[fault](state, monkeypatch)
        return
    state.status = int(fault[:3])
    state.body = {"error": {"code": "insufficient_quota" if fault == "429_quota" else "rate_limited"}}


# 函数用途: 修复故障：服务恢复 200 并放行被挡住的请求（冷却到期或改设置后使用）。
def _heal(state) -> None:
    state.status, state.block = 200, False
    state.release.set()


# 函数用途: 统计本请求的决策账本尝试次数（完成、失败、超时之和），用于证明冷却期间没有新尝试。
def _attempts(host, params) -> int:
    counts = model_call_summary(host, request_id=params.request_id)["status_counts"]
    return counts["finished"] + counts["failed"] + counts["timed_out"]


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_transport_faults_fall_back_then_recover_on_the_declared_boundary(tmp_path, server, monkeypatch, fault):  # noqa: F811
    first_status, cooldown, recovered = FAULTS[fault]
    offset = [0.0]
    real = time.monotonic
    monkeypatch.setattr(decision_policy, "time", SimpleNamespace(monotonic=lambda: real() + offset[0]))
    _inject(fault, server, monkeypatch)
    # 只有"慢响应"需要短期限来制造超时；其余故障立即失败，给足期限——整次决策调用（路由、记账、工作线程）都算在期限里，
    # 慢 CI 机器上 0.3 秒挤不下，会把本应立即失败的故障误判成 deadline（2026-09-25 runner 上 dns 恢复步骤实测）。
    host, params, thread, stage = configured(tmp_path, server, timeout=0.3 if fault == "slow" else 5.0)
    existing = set(threading.enumerate())
    first = decide(host, params, stage)
    assert first.status == first_status and not first.may_apply
    if fault == "dns":
        assert server.dns_hits, "注入的 DNS 解析失败必须真的被走到（决策请求零重试，解析失败即结束）"
    assert host.config.model_name == "deployment-model", "决策故障不改变主模型"
    blocked = "cooldown" if cooldown else "configuration_required"
    attempts = _attempts(host, params)
    offset[0] = (cooldown or 301) - 1
    again = decide(host, params, stage)
    assert (again.status, again.reason) == (blocked, "connection_backoff") and _attempts(host, params) == attempts
    if cooldown == 300:
        assert 0 < again.retry_after_seconds <= 1, "额度与限流冷却 300 秒，普通故障的 30 秒不能提前放行"
    _heal(server)
    # 超时后原 worker 仍占着本会话的资源键，放行后等它真实退出，再验证冷却到期的新尝试。
    for worker in set(threading.enumerate()) - existing:
        worker.join(3)
    # 恢复这一步模拟冷却到期后的下一轮需求：新决策阶段、宽裕期限。改设置会换修订：配置类故障正靠它才重新尝试，
    # 按时间冷却的故障不受修订影响，仍须等到期（offset 推过冷却）。
    patch(host, {"timeout_seconds": 6.0}, thread_id=thread.thread_id)
    stage = decision_service.begin_decision_stage(host, params, operation_id="batch-2")
    if cooldown:
        offset[0] = cooldown + 1
    last = decide(host, params, stage)
    assert last.status == recovered and _attempts(host, params) == attempts + 1
    assert last.may_apply is (recovered == "success")
    if cooldown and recovered != "success":
        # 故障仍在：冷却过期后的重试再失败，冷却按连续失败翻倍。
        backoff = decide(host, params, stage)
        assert (backoff.status, backoff.reason) == ("cooldown", "connection_backoff")
        assert 2 * cooldown - 1 < backoff.retry_after_seconds <= 2 * cooldown


# LLM: 同一 owner 的不同会话各自一条 canonical 线程；同会话的资源键相同，只能有一个决策在途。
# 函数用途: 创建 count 个独立会话的调用参数和决策阶段。
def _sessions(host, count: int) -> list[tuple[SimpleNamespace, object]]:
    sessions = []
    for index in range(count):
        thread = host.conversation_store.threads.get_or_create({
            "channel": "tui", "channel_conversation_id": f"conv-{index}", "channel_user_id": "alice",
            "canonical_user_id": "alice", "owner_id": "alice"})
        params = SimpleNamespace(request_id=f"req-{index}", run_id=f"run-{index}", task_id=f"task-{index}",
                                 task_attributes={"conversation_thread_id": thread.thread_id}, live_archive_state={})
        params.tui_runtime = TuiRuntime(f"decision-fault-{index}")
        params.effective_on_chunk = params.tui_runtime.begin_turn(params.request_id)
        sessions.append((params, decision_service.begin_decision_stage(host, params, operation_id=f"batch-{index}")))
    return sessions


def test_concurrent_sessions_of_one_owner_each_get_their_own_decision(tmp_path, server):  # noqa: F811
    server.block = True
    # 本用例验证并发与同会话准入，不验证期限：服务端要等 13 个请求都到齐才放行，慢 CI 上到齐本身就可能超过 1 秒
    # （2026-09-25 3.10 job 12 个会话全部 deadline）。期限给足，并在改完阶段预算后重新开始本会话的阶段。
    host, params, _thread, _stage = configured(tmp_path, server, timeout=8.0)
    patch(host, {"stage_timeout_seconds": 15.0})
    stage = decision_service.begin_decision_stage(host, params, operation_id="batch-main")
    sessions = _sessions(host, 12)
    assert len({session_stage.thread_id for _params, session_stage in sessions}) == 12
    with ThreadPoolExecutor(max_workers=14) as pool:
        futures = [pool.submit(decide, host, session_params, session_stage) for session_params, session_stage in sessions]
        same = [pool.submit(decide, host, params, stage) for _ in range(2)]
        deadline = time.monotonic() + 6
        while len(server.requests) < 13 and time.monotonic() < deadline:
            time.sleep(0.01)
        server.release.set()
        results = Counter(future.result(timeout=5).status for future in futures)
        same_session = sorted((future.result(timeout=5).status, future.result(timeout=5).reason) for future in same)
    # 修复前同 owner 并发读设置时排它锁互挤，12 个会话只有 1 个真正发出请求。
    assert results == Counter({"success": 12})
    assert same_session == [("error", "admission_busy"), ("success", "")], "同一会话同一时刻只允许一个决策在途"
    assert len(server.requests) == 13 and host.config.model_name == "deployment-model"


def test_catalog_write_lock_does_not_turn_decisions_into_settings_busy(tmp_path, server):  # noqa: F811
    # 验证的是锁语义不是期限，给足期限，免得慢 CI 上整次调用挤不进默认 1 秒而误判为 deadline
    host, params, _thread, stage = configured(tmp_path, server, timeout=8.0)
    with locked_json_path(model_profiles_path(host.home_paths)):
        result = decide(host, params, stage)
    assert result.status == "success" and result.may_apply and len(server.requests) == 1
