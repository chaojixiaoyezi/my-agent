#!/usr/bin/env python3
"""进程整体重启恢复 端到端自测(P2:重启后在册子代理停摆、游标冻死)。

场景(收尾 handoff 自测②):盯守中途把整个服务进程杀掉重启。重启前盘面:owner 名下
有 PENDING 子代理(带宿主级杀留下的 background_start=launching/running 残留)、未盯完的
watch 路(spool 有积压、游标冻结),但【没有】enabled policy、也没有排队 wake 信号
——真机实锤形态:这样的 owner 对旧发现口径完全隐形,重启后永不入表,名下
supervision/续派/唤醒全部不跑,PENDING 卡死 12 分钟+、五源游标全冻。

裁判(全结构化,monkeypatch 只打在真 spawn 这一系统边界):
  ① owner 被磁盘发现捞回登记表(【对照】旧口径 policy/信号二取一 → 0 捞回);
  ② supervision 把在册 PENDING 全部选为复活候选并走 auto_start 拉起
     (【对照】不修 launch 残留判活 → 残留把候选挡死,0 拉起);
  ③ 重启后的新进程从盘上快照复活 lane,接管消费者 pull 立刻拿到积压、游标解冻推进,
     消费到未判账归零。

跑法:
  python3 scripts/watch_harness/restart_recovery_harness.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "agent_py_agent"))

from agent.agent_core.orchestration.dispatch import capability_auto_sweep as sweep_mod  # noqa: E402
from agent.ingestion import harvester as hv  # noqa: E402
from agent.ingestion import watch_state as ws  # noqa: E402
from agent.ingestion.watch_tool import WatchStreamTool  # noqa: E402
from agent.owner_scoped_pool import ActiveOwnerRegistry  # noqa: E402
from agent.owner_wake_discovery import discover_wake_pending_owners, seed_registry_from_disk  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import content_source_simulator as sim  # noqa: E402


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])  # noqa: S603,S607
    proc.wait()
    return proc.pid


def _source_handle(source):
    def handle(url: str):
        q = parse_qs(urlsplit(url).query)
        since = int((q.get("since") or ["0"])[0] or 0)
        limit = max(1, min(500, int((q.get("limit") or ["50"])[0] or 50)))
        return True, source.pull(since, limit), ""

    return handle


def _write_run(owner_home: Path, run_id: str, status: str, background: dict) -> None:
    run_dir = owner_home / "agents" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "task.json").write_text(
        json.dumps({"id": run_id, "status": status, "attributes": {"background_start": background}}),
        encoding="utf-8",
    )


def _orphan_task(run_id: str, background: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        status="PENDING",
        attributes={"background_start": dict(background)},
        runner_active_attempt_id="",
        runner_attempts=0,
        verification_status="",
        channel_status="",
        capability_requests=[],
        capability_gaps=[],
        takeover_by="",
    )


class _Manager:
    def __init__(self, tasks: list[SimpleNamespace]):
        self._tasks = {t.id: t for t in tasks}
        self.lifecycle = SimpleNamespace(abandon_runner_attempt=lambda *a, **k: None)

    def list_runs(self):
        return list(self._tasks.values())

    def load(self, run_id: str):
        task = self._tasks.get(run_id)
        if task is None:
            raise FileNotFoundError(run_id)
        return task

    def save(self, task):
        self._tasks[task.id] = task

    def create_takeover_run(self, request):
        tk_id = f"tk-{request.source_run_id}"
        self._tasks[tk_id] = _orphan_task(tk_id, {})
        return SimpleNamespace(source_run_id=request.source_run_id, takeover_run_id=tk_id, created=True)


def _prepare_pre_restart_disk(owners: Path) -> tuple[Path, object, str, int, str]:
    """造「重启前」盘面:PENDING 残留子代理 + 未盯完 lane(积压+冻结游标),无 policy 无信号。"""
    owner_home = owners / "providers" / "feishu" / "users" / "u-restart"
    owner_home.mkdir(parents=True, exist_ok=True)
    _write_run(owner_home, "subagent-r1-aaa", "PENDING", {"status": "running", "pid": _dead_pid(), "updated_at": time.time() - 600, "launch_id": "L1"})
    _write_run(owner_home, "subagent-r2-bbb", "PENDING", {"status": "launching", "updated_at": time.time() - 600, "launch_id": "L2"})

    # 用「重启前的进程」灌一条真 lane:harvester 抬存量进 spool,消费者只 pull 一批就死。
    source, _ak, _args = _build_source(seed=20260707)
    tool = _tool(owner_home, source, run_id="subagent-r1-aaa")
    opened = json.loads(tool.execute({"action": "open", "url": "http://127.0.0.1:8951/pull", "background_harvest": 1, "watch_window_seconds": 3600}).output)
    watch_id = opened["watch_id"]
    state = ws.registry.get(watch_id)
    _settle_harvester(tool, watch_id)
    json.loads(tool.execute({"action": "pull", "watch_id": watch_id, "max_wait_seconds": 1.0}).output)  # 拉一批(在途未 ack)
    frozen_cursor = int(state.cursor or 0)
    backlog = _unjudged(state)
    hv.stop_harvester(watch_id)
    ws.persist_state(state)
    return owner_home, source, watch_id, frozen_cursor, str(backlog)


def _build_source(seed: int):
    spec = sim._build_specs()[0]
    source = sim.SourceState(spec, seed=seed)
    ak = str(Path(tempfile.mkdtemp(prefix="rr-ans-")) / "answer.jsonl")
    Path(ak).write_text("", encoding="utf-8")
    args = SimpleNamespace(answer_key=ak, decoys_per_hit=40, suspect_frac=0.5)
    sim._seed_backlog(source, 400, 40, ak)
    return source, ak, args


def _tool(owner_home: Path, source, run_id: str) -> WatchStreamTool:
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-restart"),
        _current_subagent_run_id=run_id,
    )
    tool = WatchStreamTool(agent)
    tool.allow_private_resolution = True
    tool._fetch_json = _source_handle(source)
    return tool


def _settle_harvester(tool: WatchStreamTool, watch_id: str, *, timeout: float = 8.0) -> None:
    state = ws.registry.get(watch_id)
    hv.ensure_harvester(state, tool._harvester_fetch())
    stable, last = 0, -1
    deadline = time.time() + timeout
    while time.time() < deadline and stable < 2:
        time.sleep(0.3)
        seq = int(getattr(state, "spool_seq", 0) or 0)
        settled = seq == last and bool(getattr(state, "last_reached_end", False))
        stable = stable + 1 if settled else 0
        last = seq


def _unjudged(state) -> int:
    written = int(state.totals.get("spool_candidates", 0) or 0)
    if written <= 0:
        return 0
    return max(0, written - hv.acked_candidates(hv.read_spool_cursor(state)))


def _legacy_discover(owners: Path) -> int:
    """旧发现口径(只认 enabled policy / wake 信号)的忠实重放,对照组用。"""
    import agent.owner_wake_discovery as owd

    count = 0
    providers_root = owners / "providers"
    for provider, kind, home in owd._candidate_owner_homes(providers_root):
        if any(owd._has_pending_wake_signal(root) or owd._has_enabled_progress_policy(root) for root in owd._store_roots(home)):
            count += 1
    return count


def _simulate_process_restart() -> "ActiveOwnerRegistry":
    """进程整体重启:进程内一切易失状态清零(登记表/watch 注册表/收割线程注册表)。"""
    ws.registry = ws.WatchRegistry()
    import agent.ingestion.watch_tool as wt

    wt.registry = ws.registry
    hv.harvesters = hv._HarvesterRegistry()
    return ActiveOwnerRegistry()


def _supervision_revive(owner_home: Path, *, disable_residue_fix: bool) -> tuple[list[str], dict]:
    """跑真 supervision(auto_start 打在系统边界只记录不 spawn);返回 (被拉起 run 集, summary)。
    disable_residue_fix=True 时把 launch 残留判活关回修复前行为(对照组)。"""
    tasks = [
        _orphan_task("subagent-r1-aaa", {"status": "running", "pid": _dead_pid(), "updated_at": time.time() - 600, "launch_id": "L1"}),
        _orphan_task("subagent-r2-bbb", {"status": "launching", "updated_at": time.time() - 600, "launch_id": "L2"}),
    ]
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u-restart"),
        subagents=_Manager(tasks),
        conversation_store=None,
        dispatch_subagents=lambda *a, **k: SimpleNamespace(records=[], summary={}),
    )
    import agent.agent_core.orchestration.background.dispatch as bg_dispatch
    import agent.agent_core.runner.dispatch as runner_dispatch

    started: list[list[str]] = []

    def _capture_auto_start(agent_arg, tasks_arg, options):
        started.append([t.id for t in tasks_arg])
        return {"status": "started", "run_ids": [t.id for t in tasks_arg]}

    original_auto_start = bg_dispatch.auto_start_tasks
    original_stale = runner_dispatch._background_start_record_stale
    bg_dispatch.auto_start_tasks = _capture_auto_start
    if disable_residue_fix:
        runner_dispatch._background_start_record_stale = lambda task, background: False
    try:
        summary = sweep_mod.supervise_stalled_orphans(agent)
    finally:
        bg_dispatch.auto_start_tasks = original_auto_start
        runner_dispatch._background_start_record_stale = original_stale
    return sorted({rid for batch in started for rid in batch}), summary


def _takeover_consume(owner_home: Path, source, watch_id: str) -> dict:
    """③ 游标解冻:重启后的新进程从盘上快照复活 lane,接管消费者直接消费到零。"""
    tool_b = _tool(owner_home, source, run_id="tk-subagent-r1-aaa")
    reopened = json.loads(tool_b.execute({"action": "open", "url": "http://127.0.0.1:8951/pull", "background_harvest": 1}).output)
    state = ws.registry.get(watch_id)
    delivered = 0
    for _ in range(200):
        payload = json.loads(tool_b.execute({"action": "pull", "watch_id": watch_id, "max_wait_seconds": 0.0}).output)
        cand = payload.get("candidates") or []
        delivered += len(cand)
        if not cand and _unjudged(state) == 0:
            break
    return {
        "resumed": bool(reopened.get("resumed_existing_watch")),
        "open_backlog": int(reopened.get("spool_backlog_candidates") or 0),
        "delivered": delivered,
        "final_unjudged": _unjudged(state),
        "cursor_after": int(state.cursor or 0),
    }


def main() -> None:
    owners = Path(tempfile.mkdtemp(prefix="rr-owners-"))
    owner_home, source, watch_id, frozen_cursor, backlog_str = _prepare_pre_restart_disk(owners)
    pre_backlog = int(backlog_str)

    registry = _simulate_process_restart()
    # ① 磁盘发现:旧口径(对照)0 捞回;新口径把只有 PENDING run/未盯完 lane 的 owner 捞回。
    legacy_found = _legacy_discover(owners)
    discovered = [(o.provider, o.owner_id) for o in discover_wake_pending_owners(owners)]
    seeded = seed_registry_from_disk(registry, owners)
    # ② supervision(重启后该 owner 的调度器 tick 会周期跑它):PENDING 全部复活候选。
    revived, summary = _supervision_revive(owner_home, disable_residue_fix=False)
    revived_legacy, _ = _supervision_revive(owner_home, disable_residue_fix=True)
    consume = _takeover_consume(owner_home, source, watch_id)

    print("=== 进程整体重启恢复(发现捞回 → PENDING 复活 → 游标解冻消费到零)===", flush=True)
    print(f"重启前:lane 未判积压={pre_backlog}, 冻结游标={frozen_cursor}", flush=True)
    print(f"① 磁盘发现:旧口径捞回 owner 数(对照)={legacy_found} → 新口径={discovered}, 种入登记表={seeded}", flush=True)
    print(f"② supervision 复活:auto_start 拉起={revived}(对照·不修残留判活={revived_legacy or 0}), summary={summary}", flush=True)
    print(f"③ 接管消费:open resumed={consume['resumed']} 亮积压={consume['open_backlog']}, "
          f"交付候选={consume['delivered']}, 游标 {frozen_cursor}→{consume['cursor_after']}, "
          f"最终未判={consume['final_unjudged']}", flush=True)

    ok = (
        legacy_found == 0
        and discovered == [("feishu", "u-restart")]
        and seeded == 1
        and revived == ["subagent-r1-aaa", "subagent-r2-bbb"]
        and revived_legacy == []
        and consume["resumed"]
        and consume["open_backlog"] == pre_backlog > 0
        and consume["final_unjudged"] == 0
        and consume["cursor_after"] >= frozen_cursor
        and consume["delivered"] > 0
    )
    print("VERDICT:", "PASS 重启后零停摆(隐形owner捞回·PENDING全复活·积压清零)" if ok else "FAIL 见上", flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
