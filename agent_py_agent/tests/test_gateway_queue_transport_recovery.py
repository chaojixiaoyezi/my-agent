# LLM: 隔离目录使用真实C租约、P3及RuntimeDB；P2仅沿既有合成停止进程表，不冒充物理端到端验收。
# 模块用途: 验证正MP认领/重排运输半态的跨代恢复幂等及来源拒绝围栏。
import copy
import os
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_coordinator as mc
from agent_py_agent.agent.gateway_parts import multiprocess_takeover as p2
from agent_py_agent.agent.gateway_parts import multiprocess_takeover_runtime as rt
from agent_py_agent.agent.gateway_parts import queue_service, recovery
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
    _read_reservation,
    reservation_file,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.support.multiprocess_processing_cases import (
    current_gate,
    register,
    setup_case,
)
from agent_py_agent.tests.test_gateway_processing_recovery_source import recovery_case


# LLM: BaseException只中断测试栈，不用OS信号或吞产品异常。
# 类用途: 模拟两次磁盘操作之间进程消失。
class QueueCrash(BaseException):
    pass


# LLM: 精确拦截一份processing回inbox的rename，保留真实JSON及安全门。
# 函数用途: 留住pending已写但尚未完成搬运的半态。
def crash_move(monkeypatch, processing, pending):
    original = Path.replace

    # LLM: 其它请求及原子暂存writer不受影响，异常向测试顶层传播。
    # 函数用途: 在唯一运输rename处模拟退出。
    def interrupted(path, target):
        if path == processing and Path(target) == pending:
            raise QueueCrash()
        return original(path, target)

    monkeypatch.setattr(Path, "replace", interrupted)


# LLM: 初次认领无Runtime绑定是合法事实；真实库观察零新run，同owner完整P1域由另一请求提供，不造本请求执行claim。
# 函数用途: 产生原claim rename后、执行围栏写入前的正MP后像。
@contextmanager
def claim_window(tmp_path, monkeypatch):
    case = setup_case(tmp_path)
    paths, ledger, _scope, original, pending, ownership, agent = case
    queue_service.ensure_gateway_folders(paths)
    agent.home_paths = home_paths(tmp_path / "home")
    owner = resolve_owner_home(agent.home_paths.root, ownership[0])
    owner.home_dir.mkdir(parents=True)
    repo = RuntimeRepository(runtime_db_path(owner.home_dir))
    repo.record_run_creation(owner_id=owner.owner_id, run_id="unrelated-run", role="main")
    register(ledger, dict(original, request_id="another-request"))
    row = read_json_file(pending)
    row["status"] = "pending"
    write_json_file_atomic(pending, row)
    with monkeypatch.context() as patch:
        patch.setattr(queue_service, "_gateway_claim_payload", crash_claim)
        with pytest.raises(QueueCrash):
            queue_service.claim_request(paths, pending)
    monkeypatch.setattr(p2, "observe_processes", stopped_processes)
    monkeypatch.setattr(rt, "start_takeover_rechecks", lambda _controller: None)
    context = recovery._RecoveryContext(time.time() + 100, 3, 10, True, agent, None)
    yield SimpleNamespace(case=case, context=context, repo=repo,
                          path=paths.processing / pending.name, pending=pending)


# LLM: 只在原claim写围栏前退出，不返回伪造claim。
# 函数用途: 制造认领窗口的原控制流中断。
def crash_claim(*_args):
    raise QueueCrash()


# LLM: 合成停止输入不代真实主机进程观察；原P2 verifier及证据写读回仍实际运行。
# 函数用途: 给组件回归提供可控的旧域已停止进程表。
def stopped_processes():
    return {"platform": "darwin", "host_id": "host-one", "rows": {}, "failure": ""}


# LLM: 两窗口均由产品writer产生；requeue用真实Runtime绑定及P2/P3门，不手造pending。
# 函数用途: 提供两个可跨新C恢复的运输后像。
@contextmanager
def transport_window(tmp_path, monkeypatch, window):
    if window == "claim":
        with claim_window(tmp_path, monkeypatch) as c:
            yield c
        return
    with recovery_case(tmp_path, monkeypatch) as original:
        pending = original.gate.paths.inbox / original.claim.path.name
        with monkeypatch.context() as patch:
            crash_move(patch, original.claim.path, pending)
            with pytest.raises(QueueCrash):
                recovery.recover_gateway_processing_requests_report(
                    original.gate.paths, params=replace(original.context, startup=True),
                )
        c = SimpleNamespace(case=original.case, context=replace(original.context, startup=True),
                            repo=original.repo, path=original.claim.path, pending=pending)
    yield c


# LLM: 每次新真实租约和新gate，不继承旧controller许可。
# 函数用途: 模拟全新C代次的恢复扫描。
@contextmanager
def next_coordinator(c, generation):
    paths = c.case[0]
    with mc.coordinator_guard(paths.root, 1):
        controller = rt.prepare_coordinator_takeover(paths, 1, generation)
        gate = current_gate(c.case, run=generation)
        rt.bind_admission_takeover(gate, controller)
        c.gate = gate
        yield replace(c.context, admission_gate=gate)


# LLM: 原writer产生运输后像，跨两次真实C代次恢复不新增Runtime执行、不释放P3。
# 函数用途: 验证只搬运一次并保持请求、责任和数据库不变。
@pytest.mark.parametrize("window", ["claim", "requeue"])
def test_mp_transport_two_restarts_move_once_without_new_run(tmp_path, monkeypatch, window):
    with transport_window(tmp_path, monkeypatch, window) as c:
        saved = read_json_file(c.path)
        assert saved["status"] == "pending" and "execution_attempt_id" not in saved
        p3 = _read_reservation(reservation_file(c.case[0], c.path.stem))
        before_db = c.repo.db_path.read_bytes()
        reports = []
        for generation in ("third-c", "fourth-c"):
            with next_coordinator(c, generation) as context:
                reports.append(recovery.recover_gateway_processing_requests_report(c.case[0], params=context))
                assert reports[-1].load_errors == [], reports[-1].load_errors
                assert c.gate.release_settled()["released"] == 0
                assert c.gate.occupied_snapshot()["total"] == 1
        assert [report.summary["requeued"] for report in reports] == [1, 0]
        assert c.pending.exists() and not c.path.exists()
        expected = copy.deepcopy(saved)
        expected["route"]["c_run_id"] = "third-c"
        assert read_json_file(c.pending) == expected
        assert c.repo.db_path.read_bytes() == before_db
        assert _read_reservation(reservation_file(c.case[0], c.path.stem))["reservation_id"] == p3["reservation_id"]
        assert sorted(path.name for path in c.case[0].inbox.glob("*.json")) == [c.path.name]
        assert not list(c.case[0].terminal.glob("*.json"))


# LLM: route已重盖但rename未完成时仍需原始来源证明，不允许新C自证旧责任。
# 函数用途: 验证再次崩溃后的恢复沿用原运输来源且只修改路由。
@pytest.mark.parametrize("window", ["claim", "requeue"])
def test_mp_transport_restamp_then_crash_retries_with_original_source(tmp_path, monkeypatch, window):
    with transport_window(tmp_path, monkeypatch, window) as c:
        saved = read_json_file(c.path)
        with next_coordinator(c, "third-c") as context:
            with monkeypatch.context() as patch:
                crash_move(patch, c.path, c.pending)
                with pytest.raises(QueueCrash):
                    recovery.recover_gateway_processing_requests_report(c.case[0], params=context)
        assert read_json_file(c.path)["route"]["c_run_id"] == "third-c"
        with next_coordinator(c, "fourth-c") as context:
            report = recovery.recover_gateway_processing_requests_report(c.case[0], params=context)
            assert report.load_errors == [] and report.summary["requeued"] == 1
        expected = copy.deepcopy(saved)
        expected["route"]["c_run_id"] = "fourth-c"
        assert read_json_file(c.pending) == expected and not c.path.exists()


# LLM: 逐一破坏真实恢复门输入，拒绝时不得搬运、终结请求或释放占位。
# 函数用途: 覆盖来源、P1/P2、租约、owner和重复目标的拒绝围栏。
@pytest.mark.parametrize("window", ["claim", "requeue"])
@pytest.mark.parametrize("fault", ["missing_source", "missing_p1", "old_fence", "inbox", "terminal", "unknown_p2", "closing", "owner"])
def test_mp_transport_refusals_preserve_responsibility(tmp_path, monkeypatch, window, fault):
    with transport_window(tmp_path, monkeypatch, window) as c:
        paths = c.case[0]
        row = read_json_file(c.path)
        if fault == "missing_source":
            row.pop("queue_transport_source", None)
            write_json_file_atomic(c.path, row)
        elif fault == "missing_p1":
            c.case[1].root.joinpath(*c.case[1]._parts("exec-one")).unlink()
        elif fault == "old_fence":
            row["lease_epoch"] = row.get("lease_epoch", 0) + 1
            write_json_file_atomic(c.path, row)
        elif fault == "owner":
            row["route"]["owner_sha"] = "0" * 64
            write_json_file_atomic(c.path, row)
        elif fault in {"inbox", "terminal"}:
            write_json_file_atomic((paths.inbox if fault == "inbox" else paths.terminal) / c.path.name, row)
        elif fault == "unknown_p2":
            monkeypatch.setattr(p2, "observe_processes", lambda: {
                "platform": "darwin", "host_id": "host-one", "rows": {}, "failure": "unreadable",
            })
        with next_coordinator(c, "third-c") as context:
            if fault == "closing":
                c.gate._runtime_takeover._lease._closing = True
            before = c.path.read_bytes()
            report = recovery.recover_gateway_processing_requests_report(paths, params=context)
            assert report.load_errors and report.summary["requeued"] == report.summary["failed"] == 0
            assert c.path.exists() and c.path.read_bytes() == before
            assert _read_reservation(reservation_file(paths, c.path.stem))["state"] == "occupied"


# LLM: 保留真实硬链接原语，别名导致的多链接来源必须拒绝，不用mock替代安全门。
# 函数用途: 验证多链接请求不能被运输恢复洗白。
def test_mp_transport_hardlink_source_is_refused(tmp_path, monkeypatch):
    with claim_window(tmp_path, monkeypatch) as c:
        os.link(c.path, tmp_path / "request-alias.json")
        with next_coordinator(c, "third-c") as context:
            before = c.path.read_bytes()
            report = recovery.recover_gateway_processing_requests_report(c.case[0], params=context)
            assert report.load_errors and report.summary["requeued"] == 0
            assert c.path.read_bytes() == before and not c.pending.exists()
