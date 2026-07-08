#!/usr/bin/env python3
"""进程重启后"所有在盯的源都要被重新驱动"端到端自测(P2 补全:只恢复一部分源的真因)。

场景(收尾 handoff §P2):churn(接管来回)后重启,五路盯守的 puller run 落在混合状态。
补岗扫描(watch_lane_sweep)原本只认【终态】死岗;孤儿复活(auto_start_stalled_orphans)
又会因 attempt 上限 / 能力缺口 / channel BROKEN 排除掉一部分——于是"非终态但卡死、复活
也拉不起来"的那几路落进夹缝,无任何机制驱动。真机 5 源只 2 源恢复即此。

五路 puller 状态(重启后盘面):
  L0 DONE 终态 + 未盯完 lane         → 补岗(既有行为,基线对照)
  L1 PENDING,attempts≥cap(churn)   → 复活排除 → 【本棒】补岗接管(fresh run)
  L2 TAKEN_OVER→链尾 PENDING attempts≥cap → 复活排除 → 【本棒】补岗接管
  L3 PENDING,attempts=1(可复活)    → 交给 auto_start,补岗【不】抢(防双驱)
  L4 PENDING,消费仍新鲜(刚在拉)    → 有人在岗,补岗【不】抢

裁判(全结构化):
  ① 修复后:L0/L1/L2 建接管(每路恰一个 takeover),L3/L4 不建;
  ② 对照(旧 _respawn_lane_if_dead:只认终态):L1/L2 无接管 = 停摆夹缝复现;
  ③ 可复活的 L3 在两种实现下都不被补岗抢(auto_start 负责,不双驱)。

跑法:
  python3 scripts/watch_harness/restart_lane_drive_harness.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "agent_py_agent"))

from agent.agent_core.orchestration.dispatch import watch_lane_sweep as sweep  # noqa: E402
from agent.ingestion import harvester as hv  # noqa: E402
from agent.ingestion import watch_state as ws  # noqa: E402

_CAP = 120  # watch_response_cap_seconds 默认(config.subagent_watch_interval_seconds)
_STALE = 900.0  # 消费停摆:回拨到分钟级前(> 2×cap 新鲜窗)
_ATTEMPT_CAP = 4  # 与 capability_auto_sweep._ORPHAN_REVIVE_ATTEMPT_CAP 对齐


def _seed_lane(owner_home: Path, watch_id: str, puller: str, consumed_ago: float) -> None:
    """盘上造一路未盯完的 lane(有窗未走完 → needs_watching);consumed_ago=最近消费距今秒数
    (大=停摆/小=在岗)。固定造 30 条未 ack 积压(积压真实存在)。"""
    state = ws.new_state(owner_home, f"http://127.0.0.1:9/{watch_id}", {"watch_window_seconds": 3600})
    state.watch_id = watch_id
    state.opened_at = time.time() - 60.0  # 窗口未走完
    state.last_puller_run_id = puller
    state.opened_by_run = puller
    state.totals["spool_candidates"] = 30
    consumed_at = time.time() - consumed_ago
    state.last_pull_at = consumed_at
    ws.persist_state(state)
    hv._write_spool_cursor(state, {"candidates_consumed": 0, "candidates_acked": 0, "updated_at": consumed_at})


def _run(run_id: str, status: str, attempts: int = 0, dead_residue: bool = True) -> SimpleNamespace:
    bg = {"status": "running", "pid": _dead_pid(), "updated_at": time.time() - 600, "launch_id": f"L-{run_id}"} if dead_residue else {}
    return SimpleNamespace(
        id=run_id, status=status, takeover_by="",
        attributes={"background_start": bg}, runner_active_attempt_id="", runner_attempts=attempts,
        runner_session={}, verification_status="", channel_status="", capability_requests=[], capability_gaps=[],
    )


def _dead_pid() -> int:
    import subprocess

    proc = subprocess.Popen(["true"])  # noqa: S603,S607
    proc.wait()
    return proc.pid


class _Manager:
    def __init__(self, runs: dict[str, SimpleNamespace]):
        self._runs = dict(runs)
        self.created: list[str] = []

    def load(self, run_id: str) -> SimpleNamespace:
        run = self._runs.get(run_id)
        if run is None:
            raise FileNotFoundError(run_id)
        return run

    def create_takeover_run(self, request):
        # 幂等:同一 source 已建过就不重复(真 takeover 服务同款语义)。
        if any(request.source_run_id == c for c in self.created):
            return SimpleNamespace(source_run_id=request.source_run_id, takeover_run_id="", created=False)
        self.created.append(request.source_run_id)
        tk = f"tk-{request.source_run_id}"
        self._runs[tk] = _run(tk, "PENDING", dead_residue=False)
        return SimpleNamespace(source_run_id=request.source_run_id, takeover_run_id=tk, created=True)


def _agent(owner_home: Path, manager: _Manager) -> SimpleNamespace:
    return SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-restart"),
        subagents=manager, conversation_store=None,
        config=SimpleNamespace(subagent_watch_interval_seconds=_CAP),
    )


def _lanes():
    # (watch_id, puller_run, duty_run(链尾), consumed_ago, 期望补岗);consumed_ago 大=停摆/小=在岗。
    return [
        ("ws-l0dead0000", "run-l0", _run("run-l0", "DONE", dead_residue=False), _STALE, True),
        ("ws-l1capped00", "run-l1", _run("run-l1", "PENDING", _ATTEMPT_CAP + 1), _STALE, True),
        ("ws-l2takeover0", "run-l2", None, _STALE, True),  # duty via chain
        ("ws-l3revivab0", "run-l3", _run("run-l3", "PENDING", 1), _STALE, False),
        ("ws-l4fresh000", "run-l4", _run("run-l4", "PENDING", 1), 1.0, False),
    ]


def _build(owner_home: Path) -> _Manager:
    runs: dict[str, SimpleNamespace] = {}
    for watch_id, puller, duty, consumed_ago, _exp in _lanes():
        _seed_lane(owner_home, watch_id, puller, consumed_ago)
        if watch_id == "ws-l2takeover0":
            # L2:puller TAKEN_OVER → 链尾接管者 PENDING 且 attempts 超上限(churn 复活排除)。
            l2 = _run("run-l2", "TAKEN_OVER", dead_residue=False)
            l2.takeover_by = "run-l2b"
            runs["run-l2"] = l2
            runs["run-l2b"] = _run("run-l2b", "PENDING", _ATTEMPT_CAP + 2)
        else:
            runs[puller] = duty
    return _Manager(runs)


def _respawned_sources(owner_home: Path, control: bool) -> set[str]:
    """跑一遍补岗扫描,返回被建了接管的 puller 源集合。control=True 时把非终态卡死分支
    关回旧行为(只认终态),对照停摆夹缝。"""
    ws.registry = ws.WatchRegistry()
    manager = _build(owner_home)
    original = sweep._lane_stuck_nonterminal
    if control:
        sweep._lane_stuck_nonterminal = lambda *a, **k: False
    try:
        sweep.respawn_dead_watch_lanes(_agent(owner_home, manager))
    finally:
        sweep._lane_stuck_nonterminal = original
    return set(manager.created)


def main() -> None:
    fixed = _respawned_sources(Path(tempfile.mkdtemp(prefix="rld-fix-")), control=False)
    control = _respawned_sources(Path(tempfile.mkdtemp(prefix="rld-ctl-")), control=True)

    # L2 沿 takeover 链走到链尾 run-l2b,接管建在链尾(既有 _chain_end_task 语义)。
    expected_respawn = {"run-l0", "run-l1", "run-l2b"}
    expected_not = {"run-l3", "run-l4"}

    print("=== 重启后所有在盯源重新驱动(非终态卡死也接管;可复活/在岗的不抢)===", flush=True)
    print(f"修复后建接管的源: {sorted(fixed)}", flush=True)
    print(f"对照(旧·只认终态)建接管的源: {sorted(control)}", flush=True)
    print(f"期望接管: {sorted(expected_respawn)}; 期望不接管(复活/在岗): {sorted(expected_not)}", flush=True)

    fixed_ok = fixed == expected_respawn
    no_double = not (fixed & expected_not)
    control_gap = ("run-l1" not in control) and ("run-l2" not in control) and ("run-l0" in control)
    print(
        f"\n[判定] 修复后五源全驱动(终态+非终态卡死都接管)={fixed_ok}, "
        f"可复活/在岗不被抢(无双驱)={no_double}, "
        f"对照复现停摆夹缝(L1/L2 无接管、L0 有)={control_gap}",
        flush=True,
    )
    ok = fixed_ok and no_double and control_gap
    print("VERDICT:", "PASS 重启后每一路都有驱动·不双驱·对照坐实夹缝" if ok else "FAIL 见上", flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
