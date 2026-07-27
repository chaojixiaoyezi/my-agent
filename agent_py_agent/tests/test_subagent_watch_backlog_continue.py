"""fix#2:判读子代理 watch 积压续判门(长期助手 外部完成信号)的判据单测。

判读子代理 agent.run 结束(想收尾)时,若自己那路 spool 还有未逐条判读的候选,
continue_subagent_watch_if_needed 会【立即原地重跑续判】直到判空/watch close。
进展守卫看【已判(acked)数在不在涨】(入流可能比判快、积压照涨但没卡死),
连续 _WATCH_STALL_CAP 轮已判数没涨=判读卡死 → 停,交补岗兜底;硬深度上限兜底防失控。
"""

from __future__ import annotations

from types import SimpleNamespace

import agent_py_agent.agent.ingestion.wake_backstop as wb
from agent_py_agent.agent.agent_core.subagent import watch_continuation as sc


def _bundle(run_id="sub-1"):
    return SimpleNamespace(options=SimpleNamespace(run_id=run_id))


def test_non_subagent_empty_run_id_stops(monkeypatch):
    # 主代理/无 run_id 上下文 → 不续(此门只治判读子代理)。
    assert sc._watch_backlog_wants_continue(SimpleNamespace(), _bundle(""), sc._WatchBacklogContinue(), 0) is False


def test_backlog_zero_stops(monkeypatch):
    # 队列判空(含 watch 已 close)→ 停,正常收尾。
    monkeypatch.setattr(wb, "run_unjudged_watch_backlog", lambda a, r: 0)
    monkeypatch.setattr(wb, "run_judged_watch_count", lambda a, r: 5)
    assert sc._watch_backlog_wants_continue(SimpleNamespace(), _bundle(), sc._WatchBacklogContinue(), 0) is False


def test_backlog_positive_and_judging_progresses_continues(monkeypatch):
    # 还有积压 + 已判数在涨(判读真在动)→ 一直续判。
    monkeypatch.setattr(wb, "run_unjudged_watch_backlog", lambda a, r: 100)
    judged = {"n": 0}
    monkeypatch.setattr(wb, "run_judged_watch_count", lambda a, r: judged["n"])
    w = sc._WatchBacklogContinue()
    for n in (0, 10, 20, 30):
        judged["n"] = n
        assert sc._watch_backlog_wants_continue(SimpleNamespace(), _bundle(), w, 1) is True
    assert w.stall == 0


def test_backlog_positive_but_judging_stalled_stops(monkeypatch):
    # 有积压但已判数连续不涨(判读卡死/模型拒判)→ 到上限放行退出,交补岗,绝不死循环。
    monkeypatch.setattr(wb, "run_unjudged_watch_backlog", lambda a, r: 100)
    monkeypatch.setattr(wb, "run_judged_watch_count", lambda a, r: 42)  # 恒不涨
    w = sc._WatchBacklogContinue()
    results = [sc._watch_backlog_wants_continue(SimpleNamespace(), _bundle(), w, 1) for _ in range(5)]
    assert results[0] is True  # 首轮先续
    assert results[-1] is False  # 连续无进展后停


def test_hard_depth_cap_stops(monkeypatch):
    # 硬深度上限兜底:即便还有积压+判读在涨,超上限也停(防失控,交补岗)。
    monkeypatch.setattr(wb, "run_unjudged_watch_backlog", lambda a, r: 9999)
    monkeypatch.setattr(wb, "run_judged_watch_count", lambda a, r: 1)
    assert sc._watch_backlog_wants_continue(
        SimpleNamespace(), _bundle(), sc._WatchBacklogContinue(), sc._WATCH_CONTINUE_DEPTH_CAP
    ) is False


def test_backlog_read_failure_stops(monkeypatch):
    # 读积压失败 → 保守停(不续,走原收尾),绝不因异常挡死收口。
    def _boom(a, r):
        raise RuntimeError("disk")

    monkeypatch.setattr(wb, "run_unjudged_watch_backlog", _boom)
    assert sc._watch_backlog_wants_continue(SimpleNamespace(), _bundle(), sc._WatchBacklogContinue(), 0) is False
