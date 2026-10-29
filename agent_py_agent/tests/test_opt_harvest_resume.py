# LLM: 只使用隔离文件、注入时钟和假HTTP验证原harvest事务；覆盖冻结与fsync跨截止、冻结后换代及宿主checkpoint。
# 模块用途: 回归事务冻结事实、窗口边界、文件轮转、背压零写入和截止前后退避。
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_py_agent.agent.ingestion import harvest_execution as execution
from agent_py_agent.agent.ingestion import harvest_request_plan as request_plan
from agent_py_agent.agent.ingestion import harvester as hv
from agent_py_agent.agent.ingestion import source_adapter, watch_tool
from agent_py_agent.agent.ingestion.harvest_execution import harvester_has_due_work
from agent_py_agent.agent.ingestion.sources import DrainBudget
from agent_py_agent.agent.ingestion.watch_state import load_state, new_state, persist_state


# LLM: 真实文件来源超过单拍预算，首次到点不能声称已读完；重载后只能读既定上界内记录。
# 函数用途: 复现到点即finalized和增长文件导致窗口漂移。
def test_harvest_expired_window_resumes_to_frozen_file_boundary(tmp_path, monkeypatch):
    path = tmp_path / "source.ndjson"
    path.write_text(''.join(f'{{"seq":{i}}}\n' for i in range(5)))
    upper = path.stat().st_size
    state = new_state(tmp_path / "owner", path.as_uri(), {"watch_window_seconds": 1})
    state.opened_at = 1
    state.audit_guarantee = True
    state.tuning = replace(state.tuning, poll_interval_seconds=0.1)
    persist_state(state)
    monkeypatch.setattr(hv, "_cycle_drain_events", lambda *_args: 2)
    assert hv._harvest_once(state, None) == "ok"
    assert state.window_finalized_at == 0
    assert state.cursor < upper
    with path.open("a") as handle:
        handle.write('{"seq":999}\n')
    # 模拟接替者，不能从更大的当前文件重新决定窗口终点。
    state = load_state(state.owner_home, state.watch_id)
    for _ in range(6):
        # 单拍核心允许手动推进；节拍闸在请求发现与loop wait，不在此重复制造定时器。
        outcome = hv._harvest_once(state, None)
        if outcome == "stop":
            break
    assert state.window_finalized_at > 0
    assert state.cursor == upper
    assert state.line_cursor == 5


# LLM: 截止后错误必须有正等待，不能被deadline钳成0导致热循环。
# 函数用途: 检验本地loop保留正退避。
def test_harvest_error_backoff_survives_expired_deadline(tmp_path):
    state = new_state(tmp_path, "https://example.invalid/source", {"watch_window_seconds": 1})
    state.opened_at = 1
    assert hv._next_harvest_wait(state, 4) >= 4


# LLM: 已冻结窗口后的错误必须服从canonical next_due_at；到点不再反复绕过重试退避。
# 函数用途: 验证截止后请求发现保留正退避。
def test_harvest_request_discovery_honors_durable_retry_due():
    payload = {
        "closed": False, "window_finalized_at": 0, "audit_guarantee": True,
        "tuning": {}, "watch_window_seconds": 1, "opened_at": 1, "saved_at": 5,
        "source_checkpoint": {"_host_harvest": {"next_due_at": 105, "error_backoff_seconds": 4,
            "boundary": {"deadline": 2, "boundary_mode": "deadline_catchup_unbounded"}}},
    }
    assert not harvester_has_due_work(payload, 100)
    assert harvester_has_due_work(payload, 105)


# LLM: opaque源无数字上界时仍补拉一个有界拍；成功后结束窗口但显式保持partial，而非宣称有限全集。
# 函数用途: 验证无界补拉到预算上限后的耐久收尾及停止新采集。
def test_harvest_unknown_cursor_boundary_finalizes_partial_after_catchup(tmp_path, monkeypatch):
    state = new_state(tmp_path, "https://example.invalid/pull?since=<next>", {"watch_window_seconds": 1})
    state.opened_at = 1
    state.audit_guarantee = True
    state.source_mode = "cursor"
    state.source_envelope = {"mode": "cursor", "request": {"method": "GET"}, "valid": True}
    state.tuning = replace(state.tuning, page_limit=2)
    monkeypatch.setattr(hv, "_cycle_drain_events", lambda *_args: 2)
    persist_state(state)
    calls = []

    # LLM: 每次都声明还有数据，证明成功收尾不依赖伪造EOF；捕获实际HTTP入口次数。
    # 函数用途: 提供两条完整记录的有界假页。
    def fetch(request):
        calls.append(request)
        return True, {"items": [{"seq": 0}, {"seq": 1}], "next_cursor": 2, "has_more": True}, ""

    assert hv._harvest_once(state, fetch) == "stop"
    assert state.window_finalized_at > 0
    assert not state.last_error
    assert len(calls) == 1
    assert state.cursor == 2
    assert not state.last_reached_end
    restored = load_state(state.owner_home, state.watch_id)
    control = restored.source_checkpoint["_host_harvest"]
    assert control["boundary"]["boundary_mode"] == "deadline_catchup_unbounded"
    assert control["partial"] is True
    assert control["finalization_status"] == "partial"
    assert restored.window_finalized_at > 0
    assert hv._harvest_once(restored, fetch) == "stop"
    assert len(calls) == 1


# LLM: 独立核对边界/读取组件；不把直接改内存再persist当作已通过完整open重新开窗流程。
# 函数用途: 验证新deadline不会复用旧窗口边界，并可读取新增范围。
def test_harvest_new_window_does_not_reuse_old_frozen_boundary(tmp_path):
    path = tmp_path / "source.ndjson"
    path.write_text('{"seq":1}\n')
    state = new_state(tmp_path / "owner", path.as_uri(), {"watch_window_seconds": 1})
    state.opened_at = 1
    state.audit_guarantee = True
    persist_state(state)
    assert hv._harvest_once(state, None) == "stop"
    with path.open("a") as handle:
        handle.write('{"seq":2}\n')
    state.opened_at = 2
    state.window_finalized_at = 0
    boundary = hv._freeze_harvest_boundary(state)
    assert boundary["end_cursor"] == path.stat().st_size
    drained = hv._drain_harvest_source(
        state, None, DrainBudget(max_events=10, page_limit=10, deadline=float("inf")), finalize_window=True,
    )
    assert drained.cursor == path.stat().st_size
    assert drained.aux_cursor == 2


# LLM: 只创建临时owner及显式query绑定的机械HTTP信封；调用者控制时钟和响应，不探测外网。
# 函数用途: 为两条harvest入口提供实际可渲染分页位置的来源fixture。
def _cursor_state(tmp_path):
    state = new_state(tmp_path / "owner", "https://example.invalid/pull?since=<next>&limit=<limit>",
                      {"watch_window_seconds": 1})
    state.opened_at = 1
    state.audit_guarantee = True
    state.source_mode = "cursor"
    state.source_envelope = {"mode": "cursor", "valid": True, "request": {
        "method": "GET", "cursor_binding": {"location": "query", "name": "since"},
        "page_size_binding": {"location": "query", "name": "limit"}}}
    state.tuning = replace(state.tuning, page_limit=2)
    persist_state(state)
    return state


# LLM: 两入口都使用原WAL/spool/audit/state；请求车道只省去Gateway权限夹具，不替代真实prepare/commit计算。
# 函数用途: 在隔离fixture上执行一拍真实采集提交并重载检查结果。
def _run_cycle(state, fetch, lane):
    if lane == "local":
        return hv._harvest_once(state, fetch)
    transaction = hv._begin_harvest_transaction(state)
    plan = SimpleNamespace(working=state, transaction=transaction, finalize_window=True)
    drain, chunks = request_plan.prepare_harvest_plan(plan, fetch)
    healthy = request_plan.commit_harvest_plan(plan, drain, chunks)
    return "error" if not healthy else "stop" if state.window_finalized_at else "ok"


# LLM: canonical与旧ordinal均配显式上界；测试单拍末页不足页长及跨拍重载，不混同本地/外部游标。
# 函数用途: 验证显式边界两入口不越界，末页预算精确且重载后固定目标。
@pytest.mark.parametrize("lane", ["local", "request"])
@pytest.mark.parametrize("semantics", ["record_ordinal", "contiguous_record_ordinal"])
@pytest.mark.parametrize("cycle_budget", [2, 5])
def test_harvest_explicit_cursor_boundary(tmp_path, monkeypatch, lane, semantics, cycle_budget):
    state = _cursor_state(tmp_path)
    state.source_checkpoint = {"external_cursor": 100}
    declared = {"end_cursor": 105}
    if semantics == "record_ordinal":
        declared["cursor_semantics"] = semantics
    else:
        state.source_envelope["cursor_position_semantics"] = semantics
    state.source_envelope["finalization_boundary"] = declared
    persist_state(state)
    monkeypatch.setattr(hv, "_cycle_drain_events", lambda *_args: cycle_budget)
    calls = []

    # LLM: 响应严格尊重实际请求的页长，源中另有窗口外记录，不能让客户端误用本地cursor作上界。
    # 函数用途: 返回真实机械外部位次范围的假HTTP页。
    def fetch(request):
        query = parse_qs(urlsplit(request.url).query)
        start, limit = int(query["since"][0]), int(query["limit"][0])
        calls.append((start, limit))
        return True, {"items": [{"seq": i} for i in range(start, start + limit)],
                      "next_cursor": start + limit, "has_more": True}, ""

    if cycle_budget == 2:
        assert _run_cycle(state, fetch, lane) == "ok"
        assert state.window_finalized_at == 0
        state = load_state(state.owner_home, state.watch_id)
        assert _run_cycle(state, fetch, lane) == "ok"
    assert _run_cycle(state, fetch, lane) == "stop"
    assert calls == [(100, 2), (102, 2), (104, 1)]
    assert state.cursor == 5
    assert state.source_checkpoint["external_cursor"] == 105
    assert state.source_checkpoint["_host_harvest"]["partial"] is False


# LLM: 无界末拍发生来源错误时不能finalize或推进游标，重载后成功才收尾；错误不是完成证明。
# 函数用途: 验证本地与请求入口的失败重试和partial终态一致。
@pytest.mark.parametrize("lane", ["local", "request"])
def test_harvest_unbounded_error_retries_before_partial_finalization(tmp_path, lane):
    state = _cursor_state(tmp_path)
    calls = []

    # LLM: 首次失败、第二次空页成功，两个响应均在同一假来源且不访问网络。
    # 函数用途: 复现末拍错误后恢复成功的来源。
    def fetch(request):
        calls.append(request)
        if len(calls) == 1:
            return False, "temporary", "NETWORK_REQUEST_FAILED"
        return True, {"items": [], "next_cursor": 0}, ""

    assert _run_cycle(state, fetch, lane) == "error"
    assert state.window_finalized_at == 0
    assert state.cursor == 0
    assert state.last_error_code == "NETWORK_REQUEST_FAILED"
    assert len(calls) == 1
    state = load_state(state.owner_home, state.watch_id)
    assert state.source_checkpoint["_host_harvest"]["next_due_at"] > 0
    assert _run_cycle(state, fetch, lane) == "stop"
    assert len(calls) == 2
    assert state.source_checkpoint["_host_harvest"]["partial"] is True


# LLM: 真正原子replace产生不同inode；两入口均先读旧文件，再于截止前换成更小/更大新文件。
# 函数用途: 防止冻结新inode后从旧字节偏移读取，且缺口只能记一次。
@pytest.mark.parametrize("lane", ["local", "request"])
@pytest.mark.parametrize("new_count", [2, 40])
def test_harvest_atomic_file_replacement_before_freeze(tmp_path, monkeypatch, lane, new_count):
    path = tmp_path / "events.ndjson"
    path.write_text(''.join(f'{{"old":{i}}}\n' for i in range(5)))
    state = new_state(tmp_path / "owner", path.as_uri(), {"watch_window_seconds": 10})
    state.opened_at = 100
    state.audit_guarantee = True
    monkeypatch.setattr(hv.time, "time", lambda: 101)
    persist_state(state)
    assert hv._harvest_once(state, None) == "ok"
    identity = state.file_identity
    replacement = tmp_path / "replacement.ndjson"
    replacement.write_text(''.join(f'{{"new":{i}}}\n' for i in range(new_count)))
    replacement.replace(path)
    assert f"{path.stat().st_dev}:{path.stat().st_ino}" != identity
    monkeypatch.setattr(hv.time, "time", lambda: 110)
    assert _run_cycle(state, None, lane) == "stop"
    restored = load_state(state.owner_home, state.watch_id)
    assert restored.cursor == path.stat().st_size
    assert restored.line_cursor == new_count
    assert restored.totals["gap_events"] == 1
    assert restored.engine.totals["events_seen"] == 5 + new_count
    assert restored.window_finalized_at == 110


# LLM: 退避最晚在deadline唤醒；已经冻结的失败在deadline之后仍遵守正next_due_at。
# 函数用途: 同时检查本地等待、请求发现与持久调度的截止钳制。
def test_harvest_backoff_clamps_to_deadline_before_first_freeze(tmp_path, monkeypatch):
    state = _cursor_state(tmp_path)
    state.opened_at, state.watch_window_seconds = 100, 10
    state.source_checkpoint = {"_host_harvest": {"next_due_at": 120, "error_backoff_seconds": 10}}
    monkeypatch.setattr(hv.time, "time", lambda: 109)
    assert hv._next_harvest_wait(state, 10) == 1
    payload = {"closed": False, "window_finalized_at": 0, "audit_guarantee": True, "tuning": {},
               "watch_window_seconds": 10, "opened_at": 100, "saved_at": 109,
               "source_checkpoint": deepcopy(state.source_checkpoint)}
    assert not harvester_has_due_work(payload, 109)
    assert harvester_has_due_work(payload, 110)
    hv._record_harvest_schedule(state, hv.DrainResult(error="temporary"))
    assert state.source_checkpoint["_host_harvest"]["next_due_at"] == 110


# LLM: _prepare仅注入Gateway只读准入；实际背压为True时不得调用租约或WAL写入口。
# 函数用途: 验证非收尾背压无磁盘事务，截止拍仍能冻结边界。
@pytest.mark.parametrize("expired", [False, True])
def test_harvest_request_prepare_backpressure_avoids_empty_wal(tmp_path, monkeypatch, expired):
    state = _cursor_state(tmp_path)
    state.opened_at, state.watch_window_seconds = 100, 10
    persist_state(state)
    monkeypatch.setattr(hv.time, "time", lambda: 110 if expired else 109)
    monkeypatch.setattr(execution, "_check_request_ref", lambda *_: None)
    monkeypatch.setattr(execution, "_check_source", lambda *_: None)
    monkeypatch.setattr(execution, "_parent_active", lambda *_: True)
    monkeypatch.setattr(hv, "_backpressured", lambda *_: True)
    calls = []

    # LLM: 只有截止拍允许进入lease/WAL；非截止调用应返回busy而不是靠假事务让测试通过。
    # 函数用途: 记录租约准入是否被不必要地调用。
    def claim(*_args):
        calls.append("lease")
        return {"lease_id": "fixture"}

    monkeypatch.setattr(execution, "_claim_request_lease", claim)
    observed = SimpleNamespace(parent=None, lease=None)
    plan = execution._prepare(None, state, {}, None, observed)
    if expired:
        assert isinstance(plan, execution.HarvestRequestPlan)
        assert plan.transaction["source_boundary"]["deadline"] == 110
        assert calls == ["lease"]
    else:
        assert plan == "busy"
        assert calls == []
        assert not hv._harvest_transaction_path(state).exists()


# LLM: inline回落的来源checkpoint只替换来源字段，宿主边界/退避必须从原state保留且深拷贝。
# 函数用途: 验证吸收drain不会抹除或被来源伪造宿主控制元数据。
def test_inline_absorb_preserves_host_harvest_checkpoint(tmp_path):
    state = _cursor_state(tmp_path)
    host = {"boundary": {"deadline": 2, "end_cursor": 15}, "next_due_at": 123, "partial": True}
    state.source_checkpoint = {"external_cursor": 10, "old": 1, "_host_harvest": deepcopy(host)}
    drain = hv.DrainResult(cursor=2, source_checkpoint={"external_cursor": 12, "_host_harvest": {"partial": False}})
    watch_tool._absorb_drain(state, drain, {"seen": 0, "pages": 0})
    assert state.source_checkpoint == {"external_cursor": 12, "_host_harvest": host}
    state.source_checkpoint["_host_harvest"]["boundary"]["end_cursor"] = 20
    assert host["boundary"]["end_cursor"] == 15


# LLM: 仅替代纯适配器运行器和HTTP，真实plan/accept、drain、source-key去重及WAL提交仍执行。
# 函数用途: 验证adapter无数字终点的EOF/预算收尾，后页错误则整拍保留待重试。
@pytest.mark.parametrize("lane", ["local", "request"])
@pytest.mark.parametrize("ending", ["eof", "budget", "later_error"])
def test_harvest_adapter_deadline_catchup(tmp_path, monkeypatch, lane, ending):
    state = _cursor_state(tmp_path)
    state.source_url = "https://example.invalid/adapter"
    state.source_mode = "adapter"
    state.audit_root_task_id = "audit-fixture"
    state.source_checkpoint = {"page": 1}
    state.source_envelope = {"mode": "adapter", "valid": True, "request": {"method": "GET"},
                             "adapter": {"path": "adapter.py", "sha256": "a" * 64}}
    state.tuning = replace(state.tuning, page_limit=1)
    persist_state(state)
    monkeypatch.setattr(hv, "_cycle_drain_events", lambda *_args: 2)
    phases, requests = [], []

    # LLM: checkpoint是来源不透明进度，必须没有宿主键；返回机械位置键而不是内容哈希。
    # 函数用途: 为真实plan/accept合同提供纯JSON适配器输出。
    def invoke(_owner, _audit, _adapter, payload):
        assert "_host_harvest" not in payload["checkpoint"]
        phases.append(payload["phase"])
        if payload["phase"] == "plan":
            return {"request": {}, "context": {"page": payload["checkpoint"]["page"]}}
        page = payload["checkpoint"]["page"]
        return {"records": [{"page": page}], "checkpoint": {"page": page + 1},
                "record_keys": [f"page-{page}"], "has_more": ending != "eof"}

    # LLM: 后页失败发生在已有完整记录之后，不能提交首屏进度；其它分支完全不触网。
    # 函数用途: 提供成功响应或一次可重试的第二页网络错误。
    def fetch(request):
        requests.append(request)
        if ending == "later_error" and len(requests) == 2:
            return False, "temporary page failure", "NETWORK_REQUEST_FAILED"
        return True, {"rows": []}, ""

    monkeypatch.setattr(source_adapter, "_invoke", invoke)
    if ending == "later_error":
        assert _run_cycle(state, fetch, lane) == "error"
        assert state.cursor == 0
        assert state.source_checkpoint["page"] == 1
        assert state.window_finalized_at == 0
        state = load_state(state.owner_home, state.watch_id)
    assert _run_cycle(state, fetch, lane) == "stop"
    assert state.cursor == (1 if ending == "eof" else 2)
    assert state.source_checkpoint["page"] == state.cursor + 1
    control = state.source_checkpoint["_host_harvest"]
    assert control["boundary"]["boundary_mode"] == "deadline_catchup_unbounded"
    assert control["finalization_status"] == "partial"
    assert control["partial"] is True
    assert phases.count("accept") == (1 if ending == "eof" else 3 if ending == "later_error" else 2)
    assert len(requests) == (1 if ending == "eof" else 4 if ending == "later_error" else 2)


# LLM: 准入与发现同用首次deadline闸；已有边界后仍不得绕过next_due_at，避免错误热循环。
# 函数用途: 验证请求_prepare到点可冻结但后续重试继续等待。
def test_harvest_request_prepare_freezes_before_old_due(tmp_path, monkeypatch):
    state = _cursor_state(tmp_path)
    state.opened_at, state.watch_window_seconds = 100, 10
    state.source_checkpoint = {"_host_harvest": {"next_due_at": 120}}
    persist_state(state)
    monkeypatch.setattr(hv.time, "time", lambda: 110)
    monkeypatch.setattr(execution, "_check_request_ref", lambda *_: None)
    monkeypatch.setattr(execution, "_check_source", lambda *_: None)
    monkeypatch.setattr(execution, "_parent_active", lambda *_: True)
    claims = []

    # LLM: 只记录lease准入次数；原WAL冻结和状态提交继续使用真实文件。
    # 函数用途: 验证只有首次冻结拍能越过旧退避。
    def claim(*_args):
        claims.append("lease")
        return {"lease_id": "fixture"}

    monkeypatch.setattr(execution, "_claim_request_lease", claim)
    observed = SimpleNamespace(parent=None, lease=None)
    plan = execution._prepare(None, state, {}, None, observed)
    assert isinstance(plan, execution.HarvestRequestPlan)
    assert claims == ["lease"]
    hv._commit_harvest_transaction(plan.working, plan.transaction)
    plan = execution._prepare(None, state, {}, None, observed)
    assert plan == "busy"
    assert claims == ["lease"]


# LLM: 冻结后崩溃恢复保留同一目标；文件身份仍为旧代时读取器必须独立核对并归零。
# 函数用途: 验证有界读取本身不依赖freeze已经重置内存游标。
def test_harvest_bounded_reader_checks_state_identity(tmp_path):
    path = tmp_path / "events.ndjson"
    path.write_text(''.join(f'{{"seq":{i}}}\n' for i in range(3)))
    state = new_state(tmp_path / "owner", path.as_uri(), {})
    state.cursor, state.line_cursor, state.file_identity = 1_000, 40, "old:inode"
    stat = path.stat()
    boundary = {"identity": f"{stat.st_dev}:{stat.st_ino}", "end_cursor": stat.st_size}
    drain = hv._drain_bounded_harvest_file(state, boundary, DrainBudget(10, 10, float("inf")))
    assert not drain.error
    assert drain.events == [(1, {"seq": 0}), (2, {"seq": 1}), (3, {"seq": 2})]
    assert state.totals["gap_events"] == 1
    assert state.cursor == 0
    assert drain.cursor == stat.st_size


# LLM: 真实换代在freeze内更改游标；提交失败后只能恢复原snapshot与同一边界，重试不双记gap或丢头部。
# 函数用途: 验证轮转与WAL异常恢复组合仍能完整读取新文件。
def test_harvest_rotated_file_commit_failure_recovers(tmp_path, monkeypatch):
    path = tmp_path / "events.ndjson"
    path.write_text('{"old":1}\n')
    state = new_state(tmp_path / "owner", path.as_uri(), {"watch_window_seconds": 10})
    state.opened_at = 100
    state.audit_guarantee = True
    monkeypatch.setattr(hv.time, "time", lambda: 101)
    persist_state(state)
    assert hv._harvest_once(state, None) == "ok"
    old_cursor = state.cursor
    replacement = tmp_path / "next.ndjson"
    replacement.write_text('{"new":2}\n{"new":3}\n')
    replacement.replace(path)
    monkeypatch.setattr(hv.time, "time", lambda: 110)
    original_append = hv.audit_append

    # LLM: 在spool已写、游标尚未发布时抛出，检验真实WAL回滚，不吞异常或伪造提交成功。
    # 函数用途: 注入可恢复的审计写失败。
    def fail_audit(*_args):
        raise OSError("fixture audit write failed")

    monkeypatch.setattr(hv, "audit_append", fail_audit)
    with pytest.raises(OSError, match="fixture audit write failed"):
        hv._harvest_once(state, None)
    assert state.cursor == old_cursor
    assert state.window_finalized_at == 0
    monkeypatch.setattr(hv, "audit_append", original_append)
    assert hv._harvest_once(state, None) == "stop"
    restored = load_state(state.owner_home, state.watch_id)
    assert restored.cursor == path.stat().st_size
    assert restored.line_cursor == 2
    assert restored.totals["gap_events"] == 1
    assert restored.engine.totals["events_seen"] == 3


# LLM: 去重/存储错误也必须清除成功证明；请求空拍不能使用遗留标记结束窗口。
# 函数用途: 验证成功标记只对当前真实请求且完整提交的一拍有效。
def test_harvest_error_and_empty_beat_cannot_reuse_catchup_proof(tmp_path):
    state = _cursor_state(tmp_path)
    hv._freeze_harvest_boundary(state)
    state.source_checkpoint["_host_harvest"]["boundary"]["catchup_complete"] = True
    hv._finalize_harvest_window(state, hv.DrainResult(reached_end=True))
    assert state.window_finalized_at == 0
    hv._record_harvest_schedule(state, hv.DrainResult(error="storage retry", pages=1))
    assert "catchup_complete" not in state.source_checkpoint["_host_harvest"]["boundary"]
    assert state.source_checkpoint["_host_harvest"]["partial"] is True


# LLM: lease准入可能跨过deadline，真实WAL冻结之后必须重新判定本拍的finalize模式。
# 函数用途: 防止准备拍首尚未到点而事务中已到点时按普通无界读取。
def test_harvest_request_rechecks_deadline_after_lease(tmp_path, monkeypatch):
    state = _cursor_state(tmp_path)
    state.opened_at, state.watch_window_seconds = 100, 10
    persist_state(state)
    clock = [109]
    monkeypatch.setattr(hv.time, "time", lambda: clock[0])
    monkeypatch.setattr(execution, "_check_request_ref", lambda *_: None)
    monkeypatch.setattr(execution, "_check_source", lambda *_: None)
    monkeypatch.setattr(execution, "_parent_active", lambda *_: True)

    # LLM: 只在租约准入时推进假时钟，原_begin仍真实冻结边界，不伪造事务结果。
    # 函数用途: 制造首次短门执行期间刚好到截止时刻的夹具。
    def claim(*_args):
        clock[0] = 110
        return {"lease_id": "fixture"}

    monkeypatch.setattr(execution, "_claim_request_lease", claim)
    plan = execution._prepare(None, state, {}, None, SimpleNamespace(parent=None, lease=None))
    assert plan.transaction["source_boundary"]["deadline"] == 110
    assert plan.finalize_window is True


# LLM: 本地入口先看见截止前时间，真实begin才跨点冻结；追加发生于冻结后，不能走普通drain越界。
# 函数用途: 用真实文件/WAL/spool复现入口布尔值与事务边界不同步。
def test_harvest_local_begin_crosses_deadline_and_bounds_postfreeze_append(tmp_path, monkeypatch):
    path = tmp_path / "events.ndjson"
    path.write_text('{"seq":1}\n{"seq":2}\n')
    upper = path.stat().st_size
    state = new_state(tmp_path / "owner", path.as_uri(), {"watch_window_seconds": 10})
    state.opened_at, state.audit_guarantee = 100, True
    clock = [109]
    monkeypatch.setattr(hv.time, "time", lambda: clock[0])
    persist_state(state)
    original_begin = hv._begin_harvest_transaction
    transactions = []

    # LLM: 只在原begin入口推进时钟，保留真实冻结和fsync；冻结返回后才向同一inode追加完整记录。
    # 函数用途: 稳定构造截止前准入、截止时冻结、截止后增长的交错。
    def begin_and_append(working):
        assert clock[0] == 109
        clock[0] = 110
        transaction = original_begin(working)
        transactions.append(transaction)
        with path.open("a") as handle:
            handle.write('{"seq":999}\n')
        return transaction

    monkeypatch.setattr(hv, "_begin_harvest_transaction", begin_and_append)
    outcome = hv._harvest_once(state, None)
    assert transactions[0]["source_boundary"]["end_cursor"] == upper
    assert path.stat().st_size > upper
    assert state.cursor == upper
    assert outcome == "stop"
    restored = load_state(state.owner_home, state.watch_id)
    assert restored.cursor == upper
    assert restored.line_cursor == 2
    assert restored.engine.totals["events_seen"] == 2
    assert restored.source_checkpoint["_host_harvest"]["finalization_status"] == "complete"
    assert hv._harvest_once(restored, None) == "stop"


# LLM: 请求准入仅隔离Gateway权限夹具，真实_prepare仍重载来源、开始WAL并构造plan；不替代冻结结果。
# 函数用途: 为跨截止回归复用原请求准备入口，不触网或启动服务。
def _prepare_request_fixture(state, monkeypatch):
    monkeypatch.setattr(execution, "_check_request_ref", lambda *_: None)
    monkeypatch.setattr(execution, "_check_source", lambda *_: None)
    monkeypatch.setattr(execution, "_parent_active", lambda *_: True)
    monkeypatch.setattr(execution, "_claim_request_lease", lambda *_: {"lease_id": "fixture"})
    return execution._prepare(None, state, {}, None, SimpleNamespace(parent=None, lease=None))


# LLM: 真正冻结发生在截止前，原WAL写入/fsync结束才到点；该事务不能因晚读时钟无边界complete。
# 函数用途: 验证两入口都把收尾模式绑定冻结事实，下一拍真实补拉成功后仍明确partial。
@pytest.mark.parametrize("lane", ["local", "request"])
def test_harvest_fsync_crosses_deadline_without_frozen_boundary(tmp_path, monkeypatch, lane):
    state = _cursor_state(tmp_path)
    state.opened_at, state.watch_window_seconds = 100, 10
    clock = [109]
    monkeypatch.setattr(hv.time, "time", lambda: clock[0])
    monkeypatch.setattr(hv, "_cycle_drain_events", lambda *_: 2)
    persist_state(state)
    original_write = hv._write_harvest_transaction
    boundaries, calls = [], []

    # LLM: 先执行原WAL写和fsync，再推进假时钟；保存真实None边界，不伪造freeze或事务内容。
    # 函数用途: 将跨deadline时刻精确放在冻结之后的持久化窗口。
    def write_then_cross(path, transaction):
        original_write(path, transaction)
        if transaction["status"] == "preparing":
            boundaries.append(deepcopy(transaction["source_boundary"]))
            clock[0] = 110

    # LLM: 第一拍真实空页EOF只证明本拍读空，第二拍保持has_more=True以证明partial不依赖伪造EOF。
    # 函数用途: 提供可检查请求次数的两拍假来源。
    def fetch(request):
        calls.append(request)
        if len(calls) == 1:
            return True, {"items": [], "next_cursor": 0, "has_more": False}, ""
        return True, {"items": [{"seq": 1}, {"seq": 2}], "next_cursor": 2, "has_more": True}, ""

    monkeypatch.setattr(hv, "_write_harvest_transaction", write_then_cross)
    if lane == "local":
        assert hv._harvest_once(state, fetch) == "ok"
    else:
        plan = _prepare_request_fixture(state, monkeypatch)
        drain, chunks = request_plan.prepare_harvest_plan(plan, fetch)
        assert request_plan.commit_harvest_plan(plan, drain, chunks)
        state = plan.working
    assert boundaries == [None]
    assert len(calls) == 1
    assert state.window_finalized_at == 0
    if lane == "request":
        assert plan.finalize_window is False
    restored = load_state(state.owner_home, state.watch_id)
    control = restored.source_checkpoint["_host_harvest"]
    assert "boundary" not in control
    assert "finalization_status" not in control
    monkeypatch.setattr(hv, "_write_harvest_transaction", original_write)
    clock[0] = 111
    if lane == "local":
        assert hv._harvest_once(restored, fetch) == "stop"
    else:
        plan = _prepare_request_fixture(restored, monkeypatch)
        assert plan.finalize_window is True
        drain, chunks = request_plan.prepare_harvest_plan(plan, fetch)
        assert request_plan.commit_harvest_plan(plan, drain, chunks)
    restored = load_state(state.owner_home, state.watch_id)
    assert len(calls) == 2
    assert restored.cursor == 2
    assert restored.last_reached_end is False
    assert restored.window_finalized_at == 111
    assert restored.source_checkpoint["_host_harvest"]["boundary"]["boundary_mode"] == "deadline_catchup_unbounded"
    assert restored.source_checkpoint["_host_harvest"]["finalization_status"] == "partial"
    assert restored.source_checkpoint["_host_harvest"]["partial"] is True


# LLM: A边界已有部分提交后临时换B；失败事务必须保留A进度，恢复同一A inode后只能继续未提交部分。
# 函数用途: 用真实rename和两入口提交验证冻结后换代不污染cursor/identity或重复spool记录。
@pytest.mark.parametrize("lane", ["local", "request"])
def test_harvest_frozen_file_rotation_preserves_committed_progress(tmp_path, monkeypatch, lane):
    path = tmp_path / "events.ndjson"
    path.write_text(''.join(f'{{"seq":{i}}}\n' for i in range(5)))
    upper = path.stat().st_size
    state = new_state(tmp_path / "owner", path.as_uri(), {"watch_window_seconds": 10})
    state.opened_at, state.audit_guarantee = 100, True
    monkeypatch.setattr(hv.time, "time", lambda: 110)
    monkeypatch.setattr(hv, "_cycle_drain_events", lambda *_: 2)
    persist_state(state)
    assert _run_cycle(state, None, lane) == "ok"
    state = load_state(state.owner_home, state.watch_id)
    cursor, identity, gaps = state.cursor, state.file_identity, state.totals["gap_events"]
    boundary = deepcopy(state.source_checkpoint["_host_harvest"]["boundary"])
    spool_before = hv.spool_path(state).read_bytes()
    assert 0 < cursor < upper
    assert state.line_cursor == 2
    saved_a, saved_b = tmp_path / "saved-a.ndjson", tmp_path / "saved-b.ndjson"
    path.rename(saved_a)
    path.write_text('{"replacement":true}\n')
    assert f"{path.stat().st_dev}:{path.stat().st_ino}" != identity
    assert _run_cycle(state, None, lane) == "error"
    state = load_state(state.owner_home, state.watch_id)
    assert state.last_error_code == "SOURCE_FRAGMENT_SOURCE_CHANGED"
    assert state.cursor == cursor
    assert state.line_cursor == 2
    assert state.file_identity == identity
    assert state.totals["gap_events"] == gaps
    assert state.engine.totals["events_seen"] == 2
    assert state.source_checkpoint["_host_harvest"]["boundary"] == boundary
    assert state.window_finalized_at == 0
    assert hv.spool_path(state).read_bytes() == spool_before
    path.rename(saved_b)
    saved_a.rename(path)
    assert _run_cycle(state, None, lane) == "ok"
    state = load_state(state.owner_home, state.watch_id)
    assert _run_cycle(state, None, lane) == "stop"
    state = load_state(state.owner_home, state.watch_id)
    assert state.cursor == upper
    assert state.line_cursor == 5
    assert state.file_identity == identity
    assert state.totals["gap_events"] == gaps
    assert state.engine.totals["events_seen"] == 5
    rows = [hv.json.loads(line) for line in hv.spool_path(state).read_text().splitlines()]
    assert [candidate["event"] for row in rows for candidate in row["candidates"]] == [
        {"seq": i} for i in range(5)
    ]
