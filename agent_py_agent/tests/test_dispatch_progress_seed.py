"""create_subagents 派工即种 task_progress 账本。

契约:每个真实子代理一条 in_progress 待办；不附加固定工作流；幂等(重复派同 id 不重复种)；
种子永不抛错(失败绝不影响派工)。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.agent_core.orchestration.dispatch_progress_seed import seed_dispatch_task_progress
from agent.task_progress import read_task_progress


def _agent(tmp_path, run_id="run-seed-1"):
    return SimpleNamespace(home_paths=None, root=tmp_path, _main_agent_run_id=run_id)


def test_seed_uses_current_run_params_run_id_over_fallback(tmp_path):
    # canonical run_id=本run的RunParams.run_id(交付闸读的同一本),必须优先于 _main_agent_run_id 兜底。
    agent = SimpleNamespace(
        home_paths=None,
        root=tmp_path,
        _current_run_params=SimpleNamespace(run_id="req_canonical_1"),
        _main_agent_run_id="stale-fallback",
    )
    seed = seed_dispatch_task_progress(agent, [_task("subagent-aa11", "建后端")])
    assert seed["run_id"] == "req_canonical_1"
    assert read_task_progress(tmp_path, "req_canonical_1").get("items")
    assert not read_task_progress(tmp_path, "stale-fallback").get("items")


def test_seed_after_resume_stays_on_original_durable_task_ledger(tmp_path):
    agent = SimpleNamespace(
        home_paths=None,
        root=tmp_path,
        _current_run_params=SimpleNamespace(
            run_id="request-after-resume",
            task_id="request-after-resume",
            context_scope="default",
            source="gateway",
            task_attributes={"conversation_task_id": "task-original"},
        ),
        _main_agent_run_id="stale-fallback",
    )

    seed = seed_dispatch_task_progress(agent, [_task("subagent-aa11", "继续建后端")])

    assert seed["run_id"] == "task-original"
    assert read_task_progress(tmp_path, "task-original").get("items")
    assert not read_task_progress(tmp_path, "request-after-resume").get("items")


def _task(task_id, goal):
    return SimpleNamespace(id=task_id, goal=goal)


def test_seed_creates_only_real_subagent_items(tmp_path):
    seed = seed_dispatch_task_progress(
        _agent(tmp_path), [_task("subagent-aa11", "建后端API"), _task("subagent-bb22", "建前端页面")]
    )
    assert seed == {"run_id": "run-seed-1", "seeded": 2}
    progress = read_task_progress(tmp_path, "run-seed-1")
    by_id = {item["id"]: item for item in progress["items"]}
    assert by_id["subagent-aa11"]["status"] == "in_progress"
    assert "建后端API" in by_id["subagent-aa11"]["title"]
    assert set(by_id) == {"subagent-aa11", "subagent-bb22"}


def test_seed_idempotent_on_same_ids(tmp_path):
    agent = _agent(tmp_path)
    assert seed_dispatch_task_progress(agent, [_task("subagent-aa11", "建后端")])["seeded"] == 1
    assert seed_dispatch_task_progress(agent, [_task("subagent-aa11", "建后端")]) is None
    assert len(read_task_progress(tmp_path, "run-seed-1")["items"]) == 1


def test_seed_appends_new_dispatch_without_touching_existing(tmp_path):
    agent = _agent(tmp_path)
    seed_dispatch_task_progress(agent, [_task("subagent-aa11", "建后端")])
    seed = seed_dispatch_task_progress(agent, [_task("subagent-cc33", "写测试")])
    assert seed["seeded"] == 1  # 只补新子代理
    ids = {item["id"] for item in read_task_progress(tmp_path, "run-seed-1")["items"]}
    assert ids == {"subagent-aa11", "subagent-cc33"}


def test_done_child_closes_only_its_exact_seeded_progress_item(tmp_path):
    import json

    from agent.agent_core.orchestration.dispatch_progress_seed import (
        reconcile_completed_child_items,
    )

    agent = _agent(tmp_path, run_id="task-root")
    seed_dispatch_task_progress(
        agent,
        [_task("subagent-aa11", "建后端"), _task("subagent-bb22", "建前端")],
    )
    task_root = tmp_path / "tasks" / "2026-07-16" / "demo"
    for child_id, status in (("subagent-aa11", "DONE"), ("subagent-bb22", "RUNNING")):
        path = task_root / "work" / "agents" / child_id / "canonical_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "id": child_id,
                    "parent_id": "task-root",
                    "root_id": "task-root",
                    "status": status,
                }
            ),
            encoding="utf-8",
        )

    completed = reconcile_completed_child_items(
        agent,
        tmp_path,
        "task-root",
        task_root=task_root,
    )

    progress = read_task_progress(tmp_path, "task-root")
    by_id = {item["id"]: item for item in progress["items"]}
    assert completed == ["subagent-aa11"]
    assert by_id["subagent-aa11"]["status"] == "done"
    assert by_id["subagent-aa11"]["evidence"] == ["subagent-done:subagent-aa11"]
    assert by_id["subagent-bb22"]["status"] == "in_progress"


def test_task_path_ledger_key_reconciles_exact_seeded_child_id(tmp_path):
    """Ledger fingerprints and lineage ids are different namespaces; exact seeded ids still close."""
    import json

    from agent.agent_core.orchestration.dispatch_progress_seed import (
        reconcile_completed_child_items,
    )

    ledger_id = "task-path:4f10fbc9"
    agent = _agent(tmp_path, run_id=ledger_id)
    seed_dispatch_task_progress(agent, [_task("subagent-exact-1", "实现页面")])
    task_root = tmp_path / "tasks" / "2026-08-21" / "demo"
    path = task_root / "work" / "agents" / "subagent-exact-1" / "canonical_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "id": "subagent-exact-1",
                "parent_id": "gateway-request-77",
                "root_id": "gateway-request-77",
                "status": "DONE",
            }
        ),
        encoding="utf-8",
    )

    changed = reconcile_completed_child_items(
        agent,
        tmp_path,
        ledger_id,
        task_root=task_root,
    )

    assert changed == ["subagent-exact-1"]
    assert read_task_progress(tmp_path, ledger_id)["items"][0]["status"] == "done"


def test_child_terminal_states_project_without_false_completion(tmp_path):
    import json

    from agent.agent_core.orchestration.dispatch_progress_seed import (
        reconcile_completed_child_items,
    )

    agent = _agent(tmp_path, run_id="task-root")
    child_states = (
        ("subagent-cancelled", "CANCELLED"),
        ("subagent-taken-over", "TAKEN_OVER"),
        ("subagent-blocked", "BLOCKED"),
        ("subagent-failed", "FAILED"),
        ("subagent-running", "RUNNING"),
    )
    seed_dispatch_task_progress(
        agent,
        [_task(child_id, child_id) for child_id, _status in child_states],
    )
    task_root = tmp_path / "tasks" / "2026-07-16" / "demo"
    for child_id, status in child_states:
        path = task_root / "work" / "agents" / child_id / "canonical_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "id": child_id,
                    "parent_id": "task-root",
                    "root_id": "task-root",
                    "status": status,
                }
            ),
            encoding="utf-8",
        )

    changed = reconcile_completed_child_items(
        agent,
        tmp_path,
        "task-root",
        task_root=task_root,
    )

    progress = read_task_progress(tmp_path, "task-root")
    by_id = {item["id"]: item for item in progress["items"]}
    assert changed == [
        "subagent-blocked",
        "subagent-cancelled",
        "subagent-failed",
        "subagent-taken-over",
    ]
    assert by_id["subagent-cancelled"]["status"] == "skipped"
    assert by_id["subagent-taken-over"]["status"] == "skipped"
    assert by_id["subagent-blocked"]["status"] == "blocked"
    assert by_id["subagent-failed"]["status"] == "blocked"
    assert by_id["subagent-running"]["status"] == "in_progress"

    # Re-reading an already projected failure terminal is a no-op; it must not
    # refresh the ledger forever while the parent decides how to repair it.
    assert (
        reconcile_completed_child_items(
            agent,
            tmp_path,
            "task-root",
            task_root=task_root,
        )
        == []
    )


def test_seed_never_raises_on_broken_agent():
    assert seed_dispatch_task_progress(SimpleNamespace(home_paths=None, root=None), [_task("x", "y")]) is None


# --- P1 covers 绑定回执(dispatch_coverage_binding) -----------------------------------


def _seed_coverage(tmp_path, run_id="run-seed-1", titles=("注册登录", "全文搜索")):
    from agent.task_progress import write_task_progress

    targets = [
        {"id": f"req-{index:02d}", "title": title, "status": "pending",
         "coverage_kind": "requirement_item", "source_ref": "auto:requirement-enumeration"}
        for index, title in enumerate(titles, start=1)
    ]
    write_task_progress(tmp_path, run_id, {"coverage": {"goal": "需求枚举项对账", "targets": targets}})


def _covered_task(task_id, goal, covers):
    return SimpleNamespace(id=task_id, goal=goal, attributes={"covers": covers})


def test_binding_echoes_bound_ids_and_open_targets(tmp_path):
    from agent.agent_core.orchestration.dispatch_progress_seed import dispatch_coverage_binding

    _seed_coverage(tmp_path)
    binding = dispatch_coverage_binding(
        _agent(tmp_path), [_covered_task("sub-1", "实现注册登录", ["req-01"])]
    )
    assert binding["bound"] == {"sub-1": ["req-01"]}
    assert binding["open_target_ids"] == ["req-01", "req-02"]
    assert binding["open_count"] == 2
    assert "unknown_covers_ids" not in binding


def test_binding_warns_unknown_covers_ids(tmp_path):
    from agent.agent_core.orchestration.dispatch_progress_seed import (
        COVERS_UNKNOWN_NOTE,
        dispatch_coverage_binding,
    )

    _seed_coverage(tmp_path)
    binding = dispatch_coverage_binding(
        _agent(tmp_path), [_covered_task("sub-1", "实现注册登录", ["req-01", "req-99"])]
    )
    assert binding["unknown_covers_ids"] == ["req-99"]
    assert binding["note"] == COVERS_UNKNOWN_NOTE


def test_binding_reminds_usage_when_open_targets_unbound(tmp_path):
    from agent.agent_core.orchestration.dispatch_progress_seed import (
        COVERS_BINDING_NOTE,
        dispatch_coverage_binding,
    )

    _seed_coverage(tmp_path)
    binding = dispatch_coverage_binding(_agent(tmp_path), [_task("sub-1", "实现注册登录")])
    assert binding["note"] == COVERS_BINDING_NOTE
    assert binding["open_target_ids"] == ["req-01", "req-02"]
    assert "bound" not in binding


def test_binding_none_when_no_coverage_ledger(tmp_path):
    from agent.agent_core.orchestration.dispatch_progress_seed import dispatch_coverage_binding

    assert dispatch_coverage_binding(_agent(tmp_path), [_covered_task("sub-1", "x", ["req-01"])]) is None


def test_binding_never_raises_on_broken_agent():
    from agent.agent_core.orchestration.dispatch_progress_seed import dispatch_coverage_binding

    assert dispatch_coverage_binding(SimpleNamespace(home_paths=None, root=None), [_task("x", "y")]) is None


# --- P-bigbuild 主账本回落:子/孙代理派工现场也看得到任务主清单 -------------------------


def _subagent_run_agent(tmp_path, run_id="subagent-child-1", task_id="req_root_1"):
    """子代理 run 语境:run_id=自己,task_id=根主任务(账本键语义与收口门同源)。"""
    return SimpleNamespace(
        home_paths=None,
        root=tmp_path,
        _current_run_params=SimpleNamespace(run_id=run_id, task_id=task_id),
    )


def test_binding_falls_back_to_task_ledger_for_subagent_dispatch(tmp_path):
    """大工程递归转包:子代理自己没立 coverage → 派孙代理时回落读任务主账本,
    open 项提醒/covers 校验都对主清单——树深处的派工现场从此绑得上 covers。"""
    from agent.agent_core.orchestration.dispatch_progress_seed import dispatch_coverage_binding

    _seed_coverage(tmp_path, run_id="req_root_1")
    binding = dispatch_coverage_binding(
        _subagent_run_agent(tmp_path), [_covered_task("grand-1", "实现注册登录", ["req-01"])]
    )
    assert binding["ledger_run_id"] == "req_root_1"
    assert binding["bound"] == {"grand-1": ["req-01"]}
    assert binding["open_target_ids"] == ["req-01", "req-02"]
    assert "unknown_covers_ids" not in binding


def test_binding_prefers_own_ledger_over_task_ledger(tmp_path):
    """本 run 自己立了 coverage(模型自立)→ 用自己的账,不回落(分析路行为不变)。"""
    from agent.agent_core.orchestration.dispatch_progress_seed import dispatch_coverage_binding

    _seed_coverage(tmp_path, run_id="req_root_1", titles=("根账项",))
    _seed_coverage(tmp_path, run_id="subagent-child-1", titles=("自账项A", "自账项B"))
    binding = dispatch_coverage_binding(_subagent_run_agent(tmp_path), [_task("g-1", "x")])
    assert "ledger_run_id" not in binding
    assert binding["open_count"] == 2


def test_binding_fallback_unknown_note_teaches_ledger_read(tmp_path):
    """回落场景绑了主清单里不存在的 id → note 教它读【主账本】(带 run_id 的 task_progress
    读法),不误导它去查自己的空账。"""
    from agent.agent_core.orchestration.dispatch_progress_seed import (
        COVERS_PARENT_LEDGER_UNKNOWN_NOTE,
        dispatch_coverage_binding,
    )

    _seed_coverage(tmp_path, run_id="req_root_1")
    binding = dispatch_coverage_binding(
        _subagent_run_agent(tmp_path), [_covered_task("grand-1", "x", ["req-99"])]
    )
    assert binding["unknown_covers_ids"] == ["req-99"]
    assert binding["note"] == COVERS_PARENT_LEDGER_UNKNOWN_NOTE


def test_binding_none_when_neither_ledger_has_coverage(tmp_path):
    """自己账和主账都没 coverage → 照旧 None(不打扰无清单任务)。"""
    from agent.agent_core.orchestration.dispatch_progress_seed import dispatch_coverage_binding

    assert (
        dispatch_coverage_binding(_subagent_run_agent(tmp_path), [_covered_task("g", "x", ["req-01"])])
        is None
    )


# --- P-bigbuild goal 字面 id 兜底:模型把 id 写进 goal 却丢了 covers 参数 → 系统补绑 -----


def _item(goal, params=None):
    return SimpleNamespace(goal=goal, params=params if params is not None else {})


def _autobind_agent(tmp_path, run_id="run-seed-1"):
    return SimpleNamespace(
        home_paths=None, root=tmp_path, _current_run_params=SimpleNamespace(run_id=run_id, task_id=run_id)
    )


def test_autobind_binds_goal_literal_ids(tmp_path):
    from agent.agent_core.orchestration.dispatch_progress_seed import autobind_covers_from_goal_ids

    _seed_coverage(tmp_path)
    item = _item("实现 req-01 注册登录模块,含表单校验")
    assert autobind_covers_from_goal_ids(_autobind_agent(tmp_path), [item]) == 1
    assert item.params["covers"] == ["req-01"]
    assert item.params["attributes"]["covers_auto_bound"] == ["req-01"]


def test_autobind_cjk_adjacent_and_multiple_ids(tmp_path):
    """中文紧邻(无空格)照样命中;一个 goal 点名多项就绑多项。"""
    from agent.agent_core.orchestration.dispatch_progress_seed import autobind_covers_from_goal_ids

    _seed_coverage(tmp_path)
    item = _item("实现req-01与req-02两个模块")
    autobind_covers_from_goal_ids(_autobind_agent(tmp_path), [item])
    assert item.params["covers"] == ["req-01", "req-02"]


def test_autobind_respects_explicit_covers(tmp_path):
    """显式带了 covers 的 item 一字不动(哪怕 goal 里还写了别的 id)。"""
    from agent.agent_core.orchestration.dispatch_progress_seed import autobind_covers_from_goal_ids

    _seed_coverage(tmp_path)
    item = _item("实现 req-02", {"covers": ["req-01"]})
    assert autobind_covers_from_goal_ids(_autobind_agent(tmp_path), [item]) == 0
    assert item.params["covers"] == ["req-01"]
    assert "attributes" not in item.params


def test_autobind_word_boundary_no_prefix_collision(tmp_path):
    """词边界:goal 写的是 req-011 / xreq-01 → 不算 req-01 的字面出现,不误绑。"""
    from agent.agent_core.orchestration.dispatch_progress_seed import autobind_covers_from_goal_ids

    _seed_coverage(tmp_path)
    item = _item("处理 req-011 与 xreq-01 相关事宜")
    assert autobind_covers_from_goal_ids(_autobind_agent(tmp_path), [item]) == 0
    assert "covers" not in item.params


def test_autobind_skips_closed_targets(tmp_path):
    """已 done/skipped 的项就算被 goal 点名也不绑(只对 open 项接推力)。"""
    from agent.agent_core.orchestration.dispatch_progress_seed import autobind_covers_from_goal_ids
    from agent.task_progress import write_task_progress

    _seed_coverage(tmp_path)
    write_task_progress(tmp_path, "run-seed-1", {"coverage": {"targets": [{"id": "req-01", "status": "done"}]}})
    item = _item("复查 req-01 和 req-02")
    autobind_covers_from_goal_ids(_autobind_agent(tmp_path), [item])
    assert item.params["covers"] == ["req-02"]


def test_autobind_falls_back_to_task_ledger(tmp_path):
    """子代理递归派孙代理:goal 写了主清单 id → 按主账本(task_id 回落)补绑。"""
    from agent.agent_core.orchestration.dispatch_progress_seed import autobind_covers_from_goal_ids

    _seed_coverage(tmp_path, run_id="req_root_1")
    item = _item("实现 req-02 全文搜索")
    assert autobind_covers_from_goal_ids(_subagent_run_agent(tmp_path), [item]) == 1
    assert item.params["covers"] == ["req-02"]


def test_autobind_ignores_short_ids(tmp_path):
    """太短的 id(<4 字符)撞车概率高 → 不参与字面兜底。"""
    from agent.agent_core.orchestration.dispatch_progress_seed import autobind_covers_from_goal_ids
    from agent.task_progress import write_task_progress

    write_task_progress(
        tmp_path, "run-seed-1", {"coverage": {"targets": [{"id": "a1", "title": "短id项", "status": "pending"}]}}
    )
    item = _item("处理 a1 相关")
    assert autobind_covers_from_goal_ids(_autobind_agent(tmp_path), [item]) == 0
    assert "covers" not in item.params


def test_autobind_never_raises():
    from agent.agent_core.orchestration.dispatch_progress_seed import autobind_covers_from_goal_ids

    assert autobind_covers_from_goal_ids(SimpleNamespace(home_paths=None, root=None), [_item("x")]) == 0
    assert autobind_covers_from_goal_ids(SimpleNamespace(home_paths=None, root="/nonexistent"), [object()]) == 0


def test_autobind_echoed_in_binding_receipt(tmp_path):
    """补绑痕迹随任务属性回到绑定回执:auto_bound_from_goal 让模型看到系统替它绑了什么。"""
    from agent.agent_core.orchestration.dispatch_progress_seed import dispatch_coverage_binding

    _seed_coverage(tmp_path)
    task = SimpleNamespace(
        id="sub-1", goal="实现 req-01", attributes={"covers": ["req-01"], "covers_auto_bound": ["req-01"]}
    )
    binding = dispatch_coverage_binding(_agent(tmp_path), [task])
    assert binding["bound"] == {"sub-1": ["req-01"]}
    assert binding["auto_bound_from_goal"] == {"sub-1": ["req-01"]}
