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
    assert seed == {
        "run_id": "run-seed-1",
        "seeded": 2,
        "items": [
            {
                "id": "subagent-aa11",
                "title": "子代理[ent-aa11]:建后端API",
                "status": "in_progress",
            },
            {
                "id": "subagent-bb22",
                "title": "子代理[ent-bb22]:建前端页面",
                "status": "in_progress",
            },
        ],
    }
    progress = read_task_progress(tmp_path, "run-seed-1")
    by_id = {item["id"]: item for item in progress["items"]}
    assert by_id["subagent-aa11"]["status"] == "in_progress"
    assert "建后端API" in by_id["subagent-aa11"]["title"]
    assert set(by_id) == {"subagent-aa11", "subagent-bb22"}


def test_create_subagents_seed_exposes_todo_snapshot_to_tui() -> None:
    import json

    from agent.agent_core.tool_loop.round_execution import (
        _task_progress_items_from_output,
    )

    output = {
        "created": 1,
        "task_progress_seed": {
            "run_id": "run-main",
            "seeded": 1,
            "items": [
                {"id": "child-1", "title": "实现游戏引擎", "status": "in_progress"}
            ],
        },
    }

    assert _task_progress_items_from_output(json.dumps(output, ensure_ascii=False)) == [
        {"id": "child-1", "title": "实现游戏引擎", "status": "in_progress"}
    ]


def test_create_subagents_result_envelope_preserves_bounded_todo_snapshot() -> None:
    from agent.agent_core.orchestration_tools import _create_subagents_success

    outcome = _create_subagents_success(
        {
            "created": 1,
            "task_progress_seed": {
                "run_id": "run-main",
                "seeded": 1,
                "items": [
                    {
                        "id": "child-1",
                        "title": "实现游戏引擎",
                        "status": "in_progress",
                        "private_note": "must-not-pass",
                    }
                ],
            },
        }
    )

    assert outcome.result_envelope == {
        "task_progress_seed": {
            "run_id": "run-main",
            "seeded": 1,
            "items": [
                {"id": "child-1", "title": "实现游戏引擎", "status": "in_progress"}
            ],
        }
    }


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


def test_seed_reuses_existing_todo_items_for_explicit_covers(tmp_path):
    from agent.task_progress import write_task_progress

    write_task_progress(
        tmp_path,
        "run-seed-1",
        {
            "items": [
                {"id": "2", "title": "游戏核心引擎", "status": "pending"},
                {"id": "3", "title": "植物系统", "status": "pending"},
            ]
        },
    )

    seed = seed_dispatch_task_progress(
        _agent(tmp_path),
        [
            _covered_task("subagent-engine", "实现引擎", ["2"]),
            _covered_task("subagent-plants", "实现植物", ["3"]),
        ],
    )

    assert seed == {
        "run_id": "run-seed-1",
        "seeded": 0,
        "items": [
            {"id": "2", "title": "游戏核心引擎", "status": "pending"},
            {"id": "3", "title": "植物系统", "status": "pending"},
        ],
    }
    assert [
        item["id"] for item in read_task_progress(tmp_path, "run-seed-1")["items"]
    ] == ["2", "3"]


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
    assert by_id["subagent-aa11"]["notes"] == "独立子代理已进入 canonical DONE"
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


def test_binding_accepts_plain_todo_item_ids(tmp_path):
    from agent.agent_core.orchestration.dispatch_progress_seed import dispatch_coverage_binding
    from agent.task_progress import write_task_progress

    write_task_progress(
        tmp_path,
        "run-seed-1",
        {
            "items": [
                {"id": "req-core", "title": "实现核心", "status": "pending"},
                {"id": "req-test", "title": "补齐测试", "status": "pending"},
            ]
        },
    )

    binding = dispatch_coverage_binding(
        _agent(tmp_path),
        [_covered_task("sub-core", "实现核心", ["req-core"])],
    )

    assert binding["bound"] == {"sub-core": ["req-core"]}
    assert binding["open_target_ids"] == ["req-core", "req-test"]


def test_done_child_covers_closes_plain_todo_exact_id(tmp_path):
    import json

    from agent.agent_core.orchestration.dispatch_progress_seed import (
        reconcile_completed_child_covers,
    )
    from agent.task_progress import write_task_progress

    task_root = tmp_path / "tasks" / "demo"
    agent = SimpleNamespace(
        home_paths=None,
        root=tmp_path,
        _current_run_params=SimpleNamespace(
            run_id="run-seed-1",
            task_id="run-seed-1",
            task_attributes={"run_workspace": {"task_root": str(task_root)}},
        ),
    )
    write_task_progress(
        tmp_path,
        "run-seed-1",
        {
            "items": [
                {"id": "req-core", "title": "实现核心", "status": "in_progress"},
                {"id": "req-test", "title": "补齐测试", "status": "pending"},
            ]
        },
    )
    state = task_root / "work" / "agents" / "sub-core" / "canonical_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {
                "run_id": "sub-core",
                "parent_id": "run-seed-1",
                "root_id": "run-seed-1",
                "status": "DONE",
                "attributes": {"covers": ["req-core"]},
            }
        ),
        encoding="utf-8",
    )

    changed = reconcile_completed_child_covers(agent, tmp_path, "run-seed-1")
    progress = read_task_progress(tmp_path, "run-seed-1")
    by_id = {item["id"]: item for item in progress["items"]}

    assert changed == ["req-core"]
    assert by_id["req-core"]["status"] == "done"
    assert by_id["req-test"]["status"] == "pending"


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


# --- planned-delegation exact covers 合同 -------------------------------------------


def _item(goal, params=None):
    return SimpleNamespace(goal=goal, params=params if params is not None else {})


def _autobind_agent(tmp_path, run_id="run-seed-1"):
    return SimpleNamespace(
        home_paths=None, root=tmp_path, _current_run_params=SimpleNamespace(run_id=run_id, task_id=run_id)
    )


def test_planned_dispatch_validates_only_supplied_exact_open_covers(tmp_path):
    from agent.agent_core.orchestration.dispatch_progress_seed import planned_dispatch_contract
    from agent.task_progress import write_task_progress

    write_task_progress(
        tmp_path,
        "run-seed-1",
        {
            "items": [
                {"id": "impl-core", "title": "实现核心", "status": "pending"},
                {"id": "impl-ui", "title": "实现界面", "status": "done"},
            ]
        },
    )
    items = [
        _item("实现核心", {"covers": ["impl-core"]}),
        _item("补一项", {"covers": ["impl-ui", "missing-id"]}),
        _item("未绑定"),
    ]

    contract = planned_dispatch_contract(_autobind_agent(tmp_path), items)

    assert contract["valid"] is False
    assert contract["schema_version"] == "planned_dispatch.v2"
    assert contract["binding_mode"] == "optional_exact"
    assert contract["open_target_ids"] == ["impl-core"]
    assert contract["unbound_item_indexes"] == [2]
    assert contract["unknown_covers_by_item"] == [{"index": 1, "ids": ["missing-id"]}]
    assert contract["unavailable_covers_by_item"] == [{"index": 1, "ids": ["impl-ui"]}]


def test_planned_dispatch_allows_unbound_repair_without_claiming_next_open_id(tmp_path):
    """返工已关闭项时可先不绑定；不能为了通过创建门把下一个兄弟 Todo 当 covers。"""
    from agent.agent_core.orchestration.dispatch_progress_seed import planned_dispatch_contract
    from agent.task_progress import write_task_progress

    write_task_progress(
        tmp_path,
        "run-seed-1",
        {
            "items": [
                {"id": "3", "title": "Git 命令封装", "status": "done"},
                {"id": "4", "title": "GUI 控制器", "status": "pending"},
            ]
        },
    )

    contract = planned_dispatch_contract(
        _autobind_agent(tmp_path),
        [_item("返工 Git 输出路径")],
    )

    assert contract["valid"] is True
    assert contract["open_target_ids"] == ["4"]
    assert contract["unbound_item_indexes"] == [0]
    assert contract["unavailable_covers_by_item"] == []


def test_planned_dispatch_rejects_duplicate_child_bindings(tmp_path):
    from agent.agent_core.orchestration.dispatch_progress_seed import planned_dispatch_contract
    from agent.task_progress import write_task_progress

    write_task_progress(
        tmp_path,
        "run-seed-1",
        {"items": [{"id": "impl-core", "title": "实现核心", "status": "pending"}]},
    )

    contract = planned_dispatch_contract(
        _autobind_agent(tmp_path),
        [
            _item("实现核心 A", {"covers": ["impl-core"]}),
            _item("实现核心 B", {"covers": ["impl-core"]}),
        ],
    )

    assert contract["valid"] is False
    assert contract["duplicate_covers"] == [
        {"id": "impl-core", "item_indexes": [0, 1]}
    ]


def test_planned_dispatch_excludes_exact_seeded_child_rows(tmp_path):
    from agent.agent_core.orchestration.dispatch_progress_seed import planned_dispatch_contract
    from agent.task_progress import write_task_progress

    write_task_progress(
        tmp_path,
        "run-seed-1",
        {
            "items": [
                {
                    "id": "subagent-existing",
                    "title": "子代理[existing]:实现核心",
                    "status": "in_progress",
                }
            ]
        },
    )
    agent = _autobind_agent(tmp_path)
    agent.subagents = SimpleNamespace(
        list_runs=lambda: [SimpleNamespace(id="subagent-existing")]
    )

    assert planned_dispatch_contract(agent, [_item("另一个任务")]) is None
