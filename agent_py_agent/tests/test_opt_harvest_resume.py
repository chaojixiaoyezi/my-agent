# LLM: 只使用临时文件与注入时钟验证真实harvest事务，不访问外网或启动守护线程。
# 模块用途: 覆盖窗口partial续作、固定文件上界以及请求入口退避。
from __future__ import annotations

from dataclasses import replace

from agent_py_agent.agent.ingestion import harvester as hv
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


# LLM: 后台请求发现必须服从canonical next_due_at；窗口到点不能绕开已记录退避。
# 函数用途: 防止只修本地sleep却让C/W入口仍每拍重试。
def test_harvest_request_discovery_honors_durable_retry_due():
    payload = {
        "closed": False, "window_finalized_at": 0, "audit_guarantee": True,
        "tuning": {}, "watch_window_seconds": 1, "opened_at": 1, "saved_at": 5,
        "source_checkpoint": {"_host_harvest": {"next_due_at": 105, "error_backoff_seconds": 4}},
    }
    assert not harvester_has_due_work(payload, 100)
    assert harvester_has_due_work(payload, 105)


# LLM: opaque cursor没有源端有限上界时，必须报partial并保持正退避，不能靠客户端数字假装完成。
# 函数用途: 验证不支持终点证明的来源不会误finalize，也不会额外拉取网络。
def test_harvest_unknown_cursor_boundary_stays_partial_without_fetch(tmp_path):
    state = new_state(tmp_path, "https://example.invalid/pull?since=<next>", {"watch_window_seconds": 1})
    state.opened_at = 1
    state.audit_guarantee = True
    state.source_mode = "cursor"
    state.source_envelope = {"mode": "cursor", "record_boundary": "array_item", "valid": True}
    persist_state(state)

    # LLM: 来源边界尚未获证时不得发送请求；若误调用直接使测试失败。
    # 函数用途: 捕获不应发生的网络读取。
    def forbidden_fetch(_request):
        raise AssertionError("unsupported boundary must not fetch")

    assert hv._harvest_once(state, forbidden_fetch) == "error"
    assert state.window_finalized_at == 0
    assert state.last_error
    restored = load_state(state.owner_home, state.watch_id)
    assert restored.source_checkpoint["_host_harvest"]["next_due_at"] > 0
    assert restored.window_finalized_at == 0


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
