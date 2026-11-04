"""W10: 调度历史索引的读预算、回放与真实仓库语义。"""
from __future__ import annotations

import json
import os
import threading
from contextlib import closing
from copy import deepcopy

import pytest

from agent_py_agent.agent.scheduler.repository import SchedulerRepository


# LLM: 独立owner临时目录，不构造真实模型或对外连接。
# 函数用途: 创建用于历史测试的仓库。
def _repo(root):
    return SchedulerRepository(root, owner_provider="local", owner_kind="main", owner_id="main")


# LLM: 构造原解析器接受的终态，不改变生产数据或公开schema。
# 函数用途: 提供可与旧算法逐字段比较的历史样本。
def _row(repo, number, **fields):
    return {"schema_version": "scheduler_run.v1", "owner": repo.owner, "run_id": f"srun_{number:06}",
            "job_id": "job_one" if number % 2 else "job_two", "status": "done", "created_at": 1,
            "ended_at": number + 10, **fields}


# LLM: 回填只由显式维护调用推进，每轮都应有界且在有限轮次完成。
# 函数用途: 在计量前完成旧数据迁移；旧实现不支持维护时保留其原始读取用于红测试。
def _backfill(repo, count):
    maintain = getattr(repo, "maintain_history_index", None)
    if maintain is not None:
        for _ in range(count // 16 + 20):
            if maintain()["complete"]:
                return
        pytest.fail("历史索引回填未收敛")


# LLM: JSON解析计量包括真正读取的来源与SQLite载荷，不替换解析结果。
# 函数用途: 防止按历史行数或字节数增长的history/effect热读。
def test_history_reads_flat_from_1k_to_50k(tmp_path, monkeypatch):
    repos = []
    for count in (1000, 50000):
        repo = _repo(tmp_path / str(count))
        repo.history_path.write_text("".join(json.dumps(_row(repo, i)) + "\n" for i in range(count)), encoding="utf-8")
        _backfill(repo, count)
        repos.append(repo)
    original = json.loads
    counts = dict(rows=0, bytes=0)

    def loads(text, *args, **kwargs):
        counts["rows"] += 1
        counts["bytes"] += len(text.encode("utf-8") if isinstance(text, str) else text)
        return original(text, *args, **kwargs)

    monkeypatch.setattr(json, "loads", loads)
    samples = []
    before_fds = len(os.listdir("/dev/fd"))
    for repo, count in zip(repos, (1000, 50000)):
        before = dict(counts)
        for _ in range(3):
            rows, errors = repo.history(job_id="job_one", limit=3)
            assert not errors and [row["run_id"] for row in rows] == [f"srun_{count-i:06}" for i in (1, 3, 5)]
            all_rows, errors = repo.history(limit=3)
            assert not errors and len(all_rows) == 3
            assert repo.effect_run("srun_000001")["job_id"] == "job_one"
        samples.append({key: counts[key] - before[key] for key in counts})
        assert len(os.listdir("/dev/fd")) - before_fds == 0
    print("history_cost", samples)
    assert samples[1]["rows"] <= samples[0]["rows"] + 20
    assert samples[1]["bytes"] <= samples[0]["bytes"] * 1.2 + 4096
    assert samples[1]["rows"] < 150 and samples[1]["bytes"] < 100000
    index = repos[-1]._history_index
    with closing(index._connect(read_only=True)) as conn:
        for sql, args in index.page_queries(job_id="job_one", limit=3) + index.page_queries(job_id="", limit=3):
            plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, args)]
            print("history_plan", plan)
            assert not any("TEMP B-TREE" in part or "SCAN " in part for part in plan)
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN SELECT payload FROM history_effects WHERE generation=? AND run_id=?", (1,"srun_000001"))]
        assert all("SCAN " not in part for part in plan)


# LLM: 原算法对有效行按id后写覆盖，effect按最后原行解析；坏行、owner不匹配不应被静默吞掉。
# 函数用途: 将小样本的排序、重复与错误码同旧实现比对。
def test_small_history_matches_anchor_selection_and_latest_effect(tmp_path):
    repo = _repo(tmp_path)
    raws = [_row(repo, 1), _row(repo, 2), _row(repo, 1, ended_at=100),
            _row(repo, 3, owner={"provider":"other"}), _row(repo, 2, status="invalid")]
    repo.history_path.write_text("".join(json.dumps(row) + "\n" for row in raws) + "broken\n", encoding="utf-8")
    expected, errors = {}, ["JSONDecodeError"]
    for raw in raws:
        row, error = repo._parse_run(deepcopy(raw), terminal=True)
        if error:
            errors.append(error)
        elif row is not None:
            expected[row["run_id"]] = row
    expected_rows = sorted(expected.values(), key=lambda r:(r.get("ended_at") or r.get("created_at") or 0, r["run_id"]), reverse=True)
    _backfill(repo, len(raws))
    assert repo.history(limit=3) == (expected_rows, sorted(set(errors)))
    assert repo.effect_run("srun_000002") is None
    assert repo.effect_run("srun_000001") == expected["srun_000001"]


# LLM: 首次查询不能同步解析历史；显式后台维护重启后按字节游标继续。
# 函数用途: 未覆盖时披露incomplete，回填进度持久且重启后不丢。
def test_incomplete_coverage_and_restart_resume(tmp_path):
    repo = _repo(tmp_path)
    repo.history_path.write_text("".join(json.dumps(_row(repo,i)) + "\n" for i in range(1000)), encoding="utf-8")
    rows, errors = repo.history(limit=3)
    assert rows == [] and "SCHEDULER_HISTORY_INDEX_INCOMPLETE" in errors
    first = repo.maintain_history_index()
    assert not first["complete"] and 0 < first["rows"] <= 256
    restarted = _repo(tmp_path)
    _backfill(restarted, 1000)
    assert [row["run_id"] for row in restarted.history(limit=3)[0]] == ["srun_000999", "srun_000998", "srun_000997"]
    assert restarted.effect_run("srun_000001")["status"] == "done"


# LLM: 发布源成功但索引事务中止的单点I/O情景，通过原维护游标可重放，不改原账本。
# 函数用途: 防止append与投影之间的窗口丢失终态历史。
def test_append_interleaving_and_replay_after_index_error(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    assert hasattr(repo, "_history_index"), "缺少可重放的历史索引"
    index = repo._history_index
    index.append([_row(repo,1)])
    old = repo.history(limit=3)[0]
    entered, release = threading.Event(), threading.Event()
    original = index._put_record
    failures, observed = [], []

    def put(conn, state, raw, position):
        entered.set()
        assert release.wait(5)
        return original(conn, state, raw, position)

    monkeypatch.setattr(index, "_put_record", put)

    def append():
        try:
            index.append([_row(repo,2)])
        except Exception as exc:
            failures.append(exc)

    writer = threading.Thread(target=append)
    writer.start()
    assert entered.wait(5)
    reader = threading.Thread(target=lambda: observed.append(repo.history(limit=3)))
    reader.start()
    release.set()
    writer.join(5)
    reader.join(5)
    assert not failures and not writer.is_alive() and not reader.is_alive()
    new = repo.history(limit=3)[0]
    assert observed[0][0] in (old, new) and not observed[0][1]

    def fail(*args):
        raise OSError("simulated I/O error")

    monkeypatch.setattr(index, "_put_record", fail)
    with pytest.raises(OSError, match="simulated I/O error"):
        index.append([_row(repo,3)])
    assert "SCHEDULER_HISTORY_INDEX_INCOMPLETE" in repo.history(limit=3)[1]
    monkeypatch.setattr(index, "_put_record", original)
    _backfill(repo, 10)
    assert repo.effect_run("srun_000003")["status"] == "done"


# LLM: 真实Agent、ThreadStore、scheduler service及/schedule控制入口，不替换执行/历史/owner裁决。
# 函数用途: 定时运行完成后重启同一home，最近三条展示和准确effect点查必须不变。
def test_real_schedule_control_and_restart_continuity(tmp_path):
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.gateway_parts.schedule_control import execute_schedule_control
    from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    config = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), enable_plugins=False)
    agent = SimpleAgent(config, tmp_path)
    store, repo, service = agent.conversation_store, agent.scheduler_repository, agent.scheduler_service
    thread = store.threads.get_or_create({"channel":"tui", "channel_conversation_id":"local",
        "channel_user_id":"local", "canonical_user_id":"local", "owner_id":agent.home_paths.owner_id,
        "owner_home":str(agent.home_paths.owner_home_dir)})
    job = repo.create_job(SchedulerJobCreateRequest(name="合成历史往返", prompt="本地验证", thread_id=thread.thread_id,
        schedule={"kind":"every", "every_seconds":600, "anchor_at":1000, "timezone":"UTC"},
        creator={"local":True}, now=900))[0]
    completed = []
    for tick in (1000, 1600, 2200, 2800):
        service.enqueue_ready_runs(now=tick)
        signal = next(item for item in store.wakes.pending() if item.root_task_id not in completed)
        claim = service.claim_wake(signal, now=tick + 1).claim
        assert claim is not None
        repo.mark_run_running(claim.run_id, claim.claim_id, now=tick + 1)
        terminal = service.finish(claim, status="done", response=f"completed-{tick}", delivery_status="sent", now=tick + 2)
        assert terminal is not None and repo.get_active_run(claim.run_id) is None
        completed.append(claim.run_id)
    expected, errors = repo.history(job_id=job["job_id"], limit=3)
    assert not errors and [row["run_id"] for row in expected] == list(reversed(completed[-3:]))
    scope = GatewayControlScope("local", "tui", "local", resolved_owner=OwnerIdentity.local_main())
    command = parse_conversation_control("/schedule history " + job["alias"])
    shown = execute_schedule_control(agent, command, scope)
    assert shown.ok and not shown.details["load_error_codes"]
    assert "最近3次执行" in shown.message and shown.message.count("投递:sent") == 3
    restarted = SimpleAgent(config, tmp_path)
    assert restarted.scheduler_repository.history(job_id=job["job_id"], limit=3) == (expected, [])
    assert execute_schedule_control(restarted, command, scope).message == shown.message
    assert restarted.scheduler_repository.effect_run(completed[-1]) == repo.effect_run(completed[-1])
    print("schedule_roundtrip_restart", shown.message)
