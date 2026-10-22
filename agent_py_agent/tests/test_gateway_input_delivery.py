"""回执批扫只恢复未收敛项，每批仅在退出时提交一次扫描位置。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.runtime_errors import DataCorruptionError


# LLM: 只在临时目录写真实v2回执，摘要沿正式函数生成；不伪造owner或授予执行身份。
# 函数用途: 准备有稳定文件顺序、能通过正式读取校验的回执页。
def _receipts(tmp_path, states):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    for index, state in enumerate(states):
        request_id = f"receipt-{index:03d}"
        prepared = {"id": request_id, "kind": "ask", "goal": "test input",
                    "metadata": {"client_input_digest": f"test-digest-{request_id}"}}
        receipt = delivery.GatewayInputReceipt(
            request_id=request_id, client_input_digest=delivery._prepared_request_digest(prepared),
            client_message_id=request_id, state=state, prepared_request=prepared,
            prepared_payload_digest=delivery._prepared_payload_digest(prepared), guidance_dedupe_key=request_id,
        )
        write_json_file_atomic(delivery.gateway_input_receipt_path(paths, request_id), receipt.to_dict())
        assert delivery.read_gateway_input_receipt(paths, request_id) == receipt
    return paths


# LLM: spy仍调用原子游标writer，不替换真实读取或状态机；同时记录提交时序和真实文件写次数。
# 函数用途: 观察游标提交发生在全部已扫描回执之后，且每批只有一次。
def _observe_cursor(monkeypatch):
    events, writes = [], []
    read, commit, write = delivery.read_gateway_input_receipt, delivery._commit_reconcile_cursor, delivery.write_json_file_atomic

    # LLM: 读取行为及异常原样保留，只记录访问顺序。
    # 函数用途: 记录本轮实际读取的回执编号。
    def read_receipt(paths, request_id, **kwargs):
        events.append(("read", request_id))
        return read(paths, request_id, **kwargs)

    # LLM: 仍走正式游标提交，测试不截获或省略落盘。
    # 函数用途: 记录提交位置及其相对读取的时序。
    def commit_cursor(paths, path, **kwargs):
        events.append(("cursor", path.stem))
        return commit(paths, path, **kwargs)

    # LLM: 仅统计原游标文件写入，其它原子写保持原样。
    # 函数用途: 核对不是只合并了调用计数而仍重复写文件。
    def write_cursor(path, payload, **kwargs):
        if path.name == "reconcile_cursor.json":
            writes.append(payload["last_name"])
        return write(path, payload, **kwargs)

    monkeypatch.setattr(delivery, "read_gateway_input_receipt", read_receipt)
    monkeypatch.setattr(delivery, "_commit_reconcile_cursor", commit_cursor)
    monkeypatch.setattr(delivery, "write_json_file_atomic", write_cursor)
    return events, writes


# LLM: 真实后台目标及guidance结算，仅旁听bound和settle；已归档且无错的终态未知必须连恢复锁入口都不进入。
# 函数用途: 验证已结清终态短路，而带持久错误的同一终态仍进入正式恢复链。
@pytest.mark.parametrize("has_error", [False, True])
def test_cpu6_settled_terminal_unknown_skips_bound_and_settlement(tmp_path, monkeypatch, has_error):
    from agent_py_agent.tests.test_steer_delivery_recovery import _background_input

    agent, paths, request_id = _background_input(tmp_path, "cpu6-task")
    agent.conversation_store.tasks.update_status({"task_id": "cpu6-task", "status": "completed"})
    if not has_error:
        paths.terminal.mkdir(parents=True, exist_ok=True)
        (paths.terminal / f"{request_id}.json").write_text("{}", encoding="utf-8")
    receipt = delivery.read_gateway_input_receipt(paths, request_id)
    error = {"error_type": "OSError", "category": "io", "context": "test"} if has_error else {}
    path = delivery.gateway_input_receipt_path(paths, request_id)
    write_json_file_atomic(path, replace(receipt, state="terminal_unknown", reconcile_error=error).to_dict())
    before = path.read_bytes()
    bound_calls, settlements = [], []
    original_bound = delivery._reconcile_bound_input
    recovery_type = type(agent.conversation_store.guidance.recovery)
    original_settle = recovery_type.settle_turn_locked

    # LLM: 不改变绑定校验或恢复结果，只观察是否进入目标锁链。
    # 函数用途: 记录本条回执进入bound恢复的次数。
    def bound(*args, **kwargs):
        bound_calls.append(args[2].request_id)
        return original_bound(*args, **kwargs)

    # LLM: 仍执行真实guidance结算，不把mock返回值当恢复成功。
    # 函数用途: 记录是否重新结算已结束目标。
    def settle(self, *args, **kwargs):
        settlements.append(args[0])
        return original_settle(self, *args, **kwargs)

    monkeypatch.setattr(delivery, "_reconcile_bound_input", bound)
    monkeypatch.setattr(recovery_type, "settle_turn_locked", settle)
    summary = delivery.reconcile_gateway_input_receipts(paths, agent)
    assert summary["checked"] == 1 and summary["errors"] == 0
    if has_error:
        assert bound_calls == [request_id] and settlements == ["cpu6-task"]
        assert not delivery.read_gateway_input_receipt(paths, request_id).reconcile_error
    else:
        assert summary["terminal_unknown"] == 1 and summary["terminal_unknown_errors"] == 0
        assert bound_calls == [] and settlements == [] and path.read_bytes() == before


# LLM: 短暂读异常后在任务仍活跃时清错，不得在任务随后终态时误跳过pending guidance恢复。
# 函数用途: 回归R6-2跨轮短暂错误->清错->目标终态恢复为queued。
def test_unknown_after_transient_read_must_not_disable_terminal_recovery(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_steer_delivery_recovery import _background_input

    agent, paths, rid = _background_input(tmp_path, "srun_probe")
    store = agent.conversation_store
    with monkeypatch.context() as patch:
        def broken(*args, **kwargs):
            raise OSError("temporary guidance read failure")

        patch.setattr(delivery, "gateway_input_guidance_binding", broken)
        assert delivery.reconcile_gateway_input_request(paths, request_id=rid, conversation_store=store) == "terminal_unknown"
    assert delivery.read_gateway_input_receipt(paths, rid).reconcile_error
    delivery.reconcile_gateway_input_receipts(paths, agent)
    receipt = delivery.read_gateway_input_receipt(paths, rid)
    assert receipt.state == "terminal_unknown" and not receipt.reconcile_error
    assert store.guidance.receipt("steer-d").status == "pending"
    store.tasks.update_status({"task_id": "srun_probe", "status": "completed"})
    delivery.reconcile_gateway_input_receipts(paths, agent)
    receipt = delivery.read_gateway_input_receipt(paths, rid)
    assert receipt.state == "queued", (receipt.state, store.guidance.receipt("steer-d").status)


# LLM: 回执读取、目录轮转与游标写均真实执行，spy只计数；64条贴近生产单批上限。
# 函数用途: 验证多条consumed回执只在批尾提交一次并写入最后一个名字。
@pytest.mark.parametrize("count", [1, 3, 64])
def test_cpu6_cursor_commits_once_at_batch_end(tmp_path, monkeypatch, count):
    paths = _receipts(tmp_path, ["consumed"] * count)
    events, writes = _observe_cursor(monkeypatch)
    summary = delivery.reconcile_gateway_input_receipts(paths, object())
    last_id = f"receipt-{count - 1:03d}"
    assert summary["checked"] == summary["consumed"] == count and summary["errors"] == 0
    assert events == [("read", f"receipt-{i:03d}") for i in range(count)] + [("cursor", last_id)]
    assert writes == [last_id + ".json"]
    cursor = json.loads((paths.root / "input_receipts/state/reconcile_cursor.json").read_text())
    assert cursor["last_name"] == last_id + ".json"


# LLM: 只在第二条读取处注入缺失或普通坏账；其它行及游标仍真实落盘，普通错误不得饿死后续。
# 函数用途: 混合缺失和损坏回执时，扫描继续到页尾且只提交一次。
@pytest.mark.parametrize("failure", ["missing", "corrupt", "ordinary"])
def test_cpu6_cursor_batch_continues_past_missing_or_bad_receipt(tmp_path, monkeypatch, failure):
    paths = _receipts(tmp_path, ["consumed"] * 3)
    original = delivery.read_gateway_input_receipt

    # LLM: 不改变首尾真实回执，仅模拟当前行读取结果。
    # 函数用途: 给一批混合数据提供单条缺失或读取错误。
    def read(paths, request_id, **kwargs):
        if request_id == "receipt-001":
            if failure == "missing":
                return None
            if failure == "corrupt":
                raise DataCorruptionError("test broken receipt")
            raise OSError("test read failed")
        return original(paths, request_id, **kwargs)

    monkeypatch.setattr(delivery, "read_gateway_input_receipt", read)
    events, writes = _observe_cursor(monkeypatch)
    summary = delivery.reconcile_gateway_input_receipts(paths, object())
    assert summary["checked"] == summary["consumed"] == 2
    assert summary["errors"] == (0 if failure == "missing" else 1)
    assert [event for event in events if event[0] == "cursor"] == [("cursor", "receipt-002")]
    assert writes == ["receipt-002.json"]


# LLM: 原typed未结异常身份必须保留；扫描在当前项中断，批尾仅提交已完成的上一项，确保下一轮重试当前未结项。
# 函数用途: 验证读取/恢复抛运输未知时，批尾一次提交上一完成路径且下轮从当前未结行重试。
@pytest.mark.parametrize("stage", ["read", "reconcile"])
def test_cpu6_uncertain_exit_commits_previous_completed_path_once(tmp_path, monkeypatch, stage):
    paths = _receipts(tmp_path, ["consumed", "active_pending", "consumed"])
    error = RequestExecutionOutcomeUncertainError(
        {"id": "receipt-001", "execution_attempt_id": "test-attempt", "lease_epoch": 1, "lease_owner": "test-attempt"}, "test")
    original = delivery.read_gateway_input_receipt
    before = delivery.gateway_input_receipt_path(paths, "receipt-001").read_bytes()

    # LLM: 只在当前行读取边界抛同一typed对象，不补运输身份或改写回执。
    # 函数用途: 模拟未进入恢复之前已经无法核准原执行权。
    def read(paths, request_id, **kwargs):
        if request_id == "receipt-001" and stage == "read":
            raise error
        return original(paths, request_id, **kwargs)

    # LLM: 只替换恢复边界，不重放业务；异常必须同对象传回调用方。
    # 函数用途: 模拟当前未收敛回执恢复时出现运输未知。
    def unsettled(*args):
        raise error

    monkeypatch.setattr(delivery, "read_gateway_input_receipt", read)
    monkeypatch.setattr(delivery, "_reconcile_unsettled_receipt", unsettled)
    events, writes = _observe_cursor(monkeypatch)
    with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
        delivery.reconcile_gateway_input_receipts(paths, object())
    assert caught.value is error
    assert events == [("read", "receipt-000"), ("read", "receipt-001"), ("cursor", "receipt-000")]
    assert writes == ["receipt-000.json"] and delivery.gateway_input_receipt_path(paths, "receipt-001").read_bytes() == before


# LLM: 无选中路径时不能制造空游标，spy不创建任何回执。
# 函数用途: 验证空批次不提交游标文件。
def test_cpu6_empty_batch_does_not_commit_cursor(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    events, writes = _observe_cursor(monkeypatch)
    summary = delivery.reconcile_gateway_input_receipts(paths, object())
    assert summary["checked"] == 0 and events == writes == []
    assert not (paths.root / "input_receipts/state/reconcile_cursor.json").exists()


# LLM: 在正式writer边界注入提交失败；若业务已有typed未结，最终异常仍须是原对象并保留提交失败链。
# 函数用途: 锁住普通/typed游标写失败的计错与传播，以及双重失败时的原业务异常优先级。
@pytest.mark.parametrize("primary_failure", [False, True])
@pytest.mark.parametrize("cursor_failure", ["ordinary", "typed"])
def test_cpu6_cursor_failure_preserves_primary_exception(tmp_path, monkeypatch, primary_failure, cursor_failure):
    paths = _receipts(tmp_path, ["consumed", "active_pending" if primary_failure else "consumed"])
    claim = {"id": "receipt-001", "execution_attempt_id": "test-attempt", "lease_epoch": 1, "lease_owner": "test-attempt"}
    primary = RequestExecutionOutcomeUncertainError(claim, "test-recovery")
    secondary = (RequestExecutionOutcomeUncertainError(claim, "test-cursor")
                 if cursor_failure == "typed" else OSError("test cursor write failed"))
    writes = []

    # LLM: 只让当前未收敛行抛原typed异常，不改回执或触发真实业务。
    # 函数用途: 为双重错误用例保留固定的业务未结异常对象。
    def unsettled(*args):
        raise primary

    # LLM: writer失败不能伪装游标已落盘；记录调用后抛同一提交错误对象。
    # 函数用途: 模拟批尾唯一游标写入失败。
    def fail_cursor(path, payload, **kwargs):
        writes.append(path)
        raise secondary

    monkeypatch.setattr(delivery, "_reconcile_unsettled_receipt", unsettled)
    monkeypatch.setattr(delivery, "write_json_file_atomic", fail_cursor)
    if primary_failure:
        with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
            delivery.reconcile_gateway_input_receipts(paths, object())
        assert caught.value is primary and primary.__cause__ is secondary
    elif cursor_failure == "typed":
        with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
            delivery.reconcile_gateway_input_receipts(paths, object())
        assert caught.value is secondary
    else:
        summary = delivery.reconcile_gateway_input_receipts(paths, object())
        assert summary["checked"] == summary["consumed"] == 2 and summary["errors"] == 1
    assert writes == [paths.root / "input_receipts/state/reconcile_cursor.json"]
    assert not writes[0].exists()
