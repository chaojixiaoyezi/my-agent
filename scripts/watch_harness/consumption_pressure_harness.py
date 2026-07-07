#!/usr/bin/env python3
"""消费压力 端到端自测(P1 头号:抬得出来、消费不掉)。

场景(收尾 handoff 自测①):抬取量持续大于单个消费者的判读速度 → spool 一度堆积;
消费者带着积压试图体面收工 → 收口闸拦下;消费者死掉(终态)→ 机制层补清账岗;
继任者把积压消费到零 → 收口闸放行。裁判全部用结构化账目 + 答案键,不需要模型:

  ① 压力有效:消费期间 spool 积压峰值 > 单批消费口粮(抬取确实一度快于消费);
  ② 不许带账收工:积压>0 时 _progress_ready_for_closeout=False(子代理收口闸);
  ③ 岗死必有人接:窗口走完+岗上 run 终态+积压>0 → respawn 建清账岗(带清账指令);
     【对照】旧判定(elapsed<window 一刀切)同场景 0 建岗 = 积压永远没人管;
  ④ 最终全消费:继任者 pull 到 spool 未判账归零,surface 进 spool 的真 hit 全部
     被交付过(零孤儿),清零后收口闸放行。

跑法:
  python3 scripts/watch_harness/consumption_pressure_harness.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "agent_py_agent"))

from agent.agent_core.orchestration.dispatch.watch_lane_sweep import respawn_dead_watch_lanes  # noqa: E402
from agent.agent_core.subagent.progress_closeout import _progress_ready_for_closeout  # noqa: E402
from agent.ingestion import harvester as hv  # noqa: E402
from agent.ingestion import watch_state as ws  # noqa: E402
from agent.ingestion.watch_tool import WatchStreamTool  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import content_source_simulator as sim  # noqa: E402

_WINDOW_SECONDS = 6  # 短窗:压力阶段跑完窗口即走完,收工/补岗都发生在"窗口外清账"时段


def _source_handle(source):
    def handle(url: str):
        q = parse_qs(urlsplit(url).query)
        since = int((q.get("since") or ["0"])[0] or 0)
        limit = max(1, min(500, int((q.get("limit") or ["50"])[0] or 50)))
        return True, source.pull(since, limit), ""

    return handle


def _tool(owner_home: Path, source, run_id: str) -> WatchStreamTool:
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-selftest"),
        _current_subagent_run_id=run_id,
    )
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool._fetch_json = _source_handle(source)
    return tool


def _build_source(index: int, seed: int):
    spec = sim._build_specs()[index]
    source = sim.SourceState(spec, seed=seed)
    ak = str(Path(tempfile.mkdtemp(prefix=f"cp-ans-{index}-")) / "answer.jsonl")
    Path(ak).write_text("", encoding="utf-8")
    args = SimpleNamespace(answer_key=ak, decoys_per_hit=40, suspect_frac=0.5)
    sim._seed_backlog(source, 300, 40, ak)  # 存量:since=0 冷启动整批直通
    return source, ak, args


def _hit_ids(rows: list[dict], hit_set: set[str]) -> set[str]:
    out: set[str] = set()
    for row in rows:
        ev = row.get("event") or {}
        eid = str(ev.get("event_id") or "") if isinstance(ev, dict) else ""
        if eid in hit_set:
            out.add(eid)
    return out


def _unjudged(state) -> int:
    written = int(state.totals.get("spool_candidates", 0) or 0)
    if written <= 0:
        return 0
    return max(0, written - hv.acked_candidates(hv.read_spool_cursor(state)))


def _spool_surfaced_hits(state, hit_set: set[str]) -> set[str]:
    out: set[str] = set()
    path = hv.spool_path(state)
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        out |= _record_hits(line, hit_set)
    return out


def _record_hits(line: str, hit_set: set[str]) -> set[str]:
    try:
        rec = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return set()
    rows = rec.get("candidates") or [] if isinstance(rec, dict) else []
    return _hit_ids(rows, hit_set)


def _closeout_probe(owner_home: Path, run_id: str, artifact: Path) -> bool:
    """真收口闸探针:窗口已走完 + 产物已落盘的 long_running 任务,ready 与否只剩积压说了算。"""
    task = SimpleNamespace(
        id=run_id,
        attributes={"long_running": True, "service_window_seconds": 1, "output_files": [str(artifact)]},
        created_at=time.time() - 3600.0,
        output_json="",
        context_packs=[],
    )
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(owner_home)))
    progress = {"latest_written_path": str(artifact)}
    return _progress_ready_for_closeout(progress, task, agent=agent)


class _StubManager:
    def __init__(self, tasks: dict):
        self.tasks = dict(tasks)
        self.created = []

    def load(self, run_id: str):
        task = self.tasks.get(run_id)
        if task is None:
            raise FileNotFoundError(run_id)
        return task

    def create_takeover_run(self, request):
        self.created.append(request)
        tk_id = f"tk-{request.source_run_id}"
        self.tasks[tk_id] = SimpleNamespace(id=tk_id, status="PLANNING", takeover_by="")
        return SimpleNamespace(source_run_id=request.source_run_id, takeover_run_id=tk_id, created=True)


def _sweep_agent(owner_home: Path, manager) -> SimpleNamespace:
    return SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-selftest"),
        subagents=manager,
        conversation_store=None,
        dispatch_subagents=lambda *a, **k: SimpleNamespace(records=[], summary={}),
    )


def _legacy_lane_needs_watching(lane: dict) -> bool:
    """修复前的旧判定(elapsed<window 一刀切),对照组用。"""
    if bool(lane.get("closed")):
        return False
    window = int(lane.get("watch_window_seconds") or 0)
    if window <= 0:
        return False
    return (time.time() - float(lane.get("opened_at") or 0.0)) < window


class _Lane:
    """一路压测流的共享上下文(阶段函数共用,免长参数串)。"""

    def __init__(self, index: int):
        self.index = index
        self.owner = Path(tempfile.mkdtemp(prefix=f"cp-owner-{index}-"))
        self.source, self.ak, self.args = _build_source(index, seed=20260707 + index * 71)
        self.run_a = f"run-a-{index}"
        self.port = 8931 + index
        self.tool_a = _tool(self.owner, self.source, run_id=self.run_a)
        opened = json.loads(
            self.tool_a.execute(
                {"action": "open", "url": f"http://127.0.0.1:{self.port}/pull", "background_harvest": 1, "watch_window_seconds": _WINDOW_SECONDS}
            ).output
        )
        self.watch_id = opened["watch_id"]
        self.state = ws.registry.get(self.watch_id)


def _pressure_phase(lane: _Lane) -> tuple[int, set[str], set[str]]:
    """压力阶段:活流持续灌注(远快于消费节奏)× 慢消费者每拍只消费一批。
    返回 (峰值积压, A 已交付真 hit, 答案键全集)。"""
    stop_feed = threading.Event()

    def _feed():
        while not stop_feed.is_set():
            for _ in range(15):
                lane.source.append(sim._roll_kind(lane.source.rng, lane.args), lane.ak)
            time.sleep(0.12)

    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()
    peak_backlog = 0
    a_delivered: set[str] = set()
    hit_set: set[str] = set()
    for _ in range(4):
        payload = json.loads(lane.tool_a.execute({"action": "pull", "watch_id": lane.watch_id, "max_wait_seconds": 1.0}).output)
        hit_set = _answer_hits(lane.ak)
        a_delivered |= _hit_ids(payload.get("candidates") or [], hit_set)
        peak_backlog = max(peak_backlog, _unjudged(lane.state))
        time.sleep(0.8)  # 判读间隙:抬取继续、消费停着
    stop_feed.set()
    feeder.join(timeout=2.0)
    return peak_backlog, a_delivered, hit_set


def _answer_hits(ak: str) -> set[str]:
    return {json.loads(l)["event_id"] for l in Path(ak).read_text().splitlines() if l.strip()}


def _settle_and_finish_window(state) -> None:
    """等 harvester 把剩余活流抽干(grace 期内线程还在),再等窗口彻底走完。"""
    deadline = time.time() + 8.0
    last_seq, stable = -1, 0
    while time.time() < deadline and stable < 2:
        time.sleep(0.3)
        seq = int(getattr(state, "spool_seq", 0) or 0)
        settled = seq == last_seq and bool(getattr(state, "last_reached_end", False))
        stable = stable + 1 if settled else 0
        last_seq = seq
    remaining = state.watch_window_seconds - (time.time() - state.opened_at)
    if remaining > 0:
        time.sleep(remaining + 0.2)


def _quit_and_respawn_phase(lane: _Lane, artifact: Path) -> dict:
    """A 带账收工(收口闸应拦)→ A 死(DONE、消费停摆)→ 补岗扫描应建清账岗;
    对照旧判定同场景 0 建岗。"""
    backlog_at_quit = _unjudged(lane.state)
    ready_with_backlog = _closeout_probe(lane.owner, lane.run_a, artifact)
    lane_row = next(row for row in ws.list_states(lane.owner) if row["watch_id"] == lane.watch_id)
    legacy_would_respawn = _legacy_lane_needs_watching(lane_row)
    manager = _StubManager({lane.run_a: SimpleNamespace(id=lane.run_a, status="DONE", takeover_by="")})
    # 让消费停摆信号成立(快照 last_pull_at 与 sidecar updated_at 回拨到分钟级前,
    # 模拟真机停摆;补岗扫描的新鲜窗按配置 cap=120s)。
    stale_at = time.time() - 900.0
    lane.state.last_pull_at = stale_at
    ws.persist_state(lane.state)
    cursor = hv.read_spool_cursor(lane.state)
    cursor["updated_at"] = stale_at
    hv._write_spool_cursor(lane.state, cursor)
    actions = respawn_dead_watch_lanes(_sweep_agent(lane.owner, manager))
    return {
        "backlog_at_quit": backlog_at_quit,
        "ready_with_backlog": ready_with_backlog,
        "legacy_would_respawn": legacy_would_respawn,
        "respawned": len(actions) == 1,
        "drain_reason": bool(manager.created) and ("不带 watch_window_seconds" in manager.created[0].reason),
    }


def _takeover_drain_phase(lane: _Lane, run_b: str, hit_set: set[str]) -> tuple[set[str], int]:
    """继任者接管消费到零(at-least-once:前任在途批重投)。返回 (B 交付真 hit, 最终未判)。"""
    tool_b = _tool(lane.owner, lane.source, run_id=run_b)
    json.loads(tool_b.execute({"action": "open", "url": f"http://127.0.0.1:{lane.port}/pull", "background_harvest": 1}).output)
    b_delivered: set[str] = set()
    for _ in range(200):
        payload = json.loads(tool_b.execute({"action": "pull", "watch_id": lane.watch_id, "max_wait_seconds": 0.0}).output)
        cand = payload.get("candidates") or []
        b_delivered |= _hit_ids(cand, hit_set)
        if not cand and _unjudged(lane.state) == 0:
            break
    return b_delivered, _unjudged(lane.state)


def run_one_source(index: int) -> dict:
    ws.registry = ws.WatchRegistry()
    import agent.ingestion.watch_tool as wt

    wt.registry = ws.registry
    hv.harvesters = hv._HarvesterRegistry()
    lane = _Lane(index)
    consume_quota = hv.judge_quota(lane.state.tuning)

    peak_backlog, a_delivered, hit_set = _pressure_phase(lane)
    _settle_and_finish_window(lane.state)
    hit_set = _answer_hits(lane.ak)

    artifact = lane.owner / "report.md"
    artifact.write_text("首批发现", encoding="utf-8")
    quit_facts = _quit_and_respawn_phase(lane, artifact)

    b_delivered, final_unjudged = _takeover_drain_phase(lane, f"tk-{lane.run_a}", hit_set)
    ready_after_drain = _closeout_probe(lane.owner, f"tk-{lane.run_a}", artifact)
    surfaced = _spool_surfaced_hits(lane.state, hit_set)
    orphaned = surfaced - (a_delivered | b_delivered)
    return {
        "source": sim._build_specs()[index].name,
        "true_hits_total": len(hit_set),
        "surfaced_hits": len(surfaced),
        "peak_backlog": peak_backlog,
        "consume_quota": consume_quota,
        **quit_facts,
        "final_unjudged": final_unjudged,
        "ready_after_drain": ready_after_drain,
        "orphaned_hits": len(orphaned),
        "orphan_ids": sorted(orphaned)[:8],
    }


def main() -> None:
    results = [run_one_source(i) for i in range(5)]
    print("=== 消费压力(抬取一度快于消费:堆积→拦收工→死岗补清账岗→消费到零)===", flush=True)
    hdr = (
        f"{'source':<16}{'hits':>6}{'surfaced':>9}{'peak_bl':>9}{'quota':>7}{'quit_bl':>9}"
        f"{'ready@bl':>9}{'legacy':>7}{'respawn':>8}{'final_bl':>9}{'ready@0':>8}{'orphan':>7}"
    )
    print(hdr, flush=True)
    for r in results:
        print(
            f"{r['source']:<16}{r['true_hits_total']:>6}{r['surfaced_hits']:>9}{r['peak_backlog']:>9}"
            f"{r['consume_quota']:>7}{r['backlog_at_quit']:>9}{str(r['ready_with_backlog']):>9}"
            f"{str(r['legacy_would_respawn']):>7}{str(r['respawned']):>8}{r['final_unjudged']:>9}"
            f"{str(r['ready_after_drain']):>8}{r['orphaned_hits']:>7}",
            flush=True,
        )
        if r["orphan_ids"]:
            print(f"    ORPHAN ids on {r['source']}: {r['orphan_ids']}", flush=True)

    pressure_valid = all(r["peak_backlog"] > r["consume_quota"] for r in results)
    gate_blocks = all(r["backlog_at_quit"] > 0 and r["ready_with_backlog"] is False for r in results)
    legacy_dead = all(r["legacy_would_respawn"] is False for r in results)
    respawn_ok = all(r["respawned"] and r["drain_reason"] for r in results)
    drained = all(r["final_unjudged"] == 0 and r["ready_after_drain"] is True for r in results)
    no_orphans = all(r["orphaned_hits"] == 0 for r in results) and all(r["surfaced_hits"] > 0 for r in results)
    print(
        f"\n[判定] 压力有效(峰值积压>单批口粮)={pressure_valid}, 带账收工被拦={gate_blocks}, "
        f"旧判定同场景0建岗(对照)={legacy_dead}, 清账岗建立且带清账指令={respawn_ok}, "
        f"最终未判归零且清零后放行收口={drained}, surface真hit零孤儿={no_orphans}",
        flush=True,
    )
    ok = pressure_valid and gate_blocks and legacy_dead and respawn_ok and drained and no_orphans
    print("VERDICT:", "PASS 堆积被消化·停止前清空·真hit全交付" if ok else "FAIL 见上", flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
