"""R4 隔离的直接语义测试：坏子树被过滤，健康动作照常执行；policy 级仍整份拒绝。

这些用例专门守住 `_without_errored_subtrees`：如果它被改成直接返回原计划（隔离失效），
坏子树里的动作会被放行，本文件的断言必须红。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agent_py_agent.agent.memory_store.retention import (
    _has_policy_level_error,
    _path_overlaps,
    _without_errored_subtrees,
)
from agent_py_agent.agent.memory_store.retention_models import (
    MemoryRetentionAction,
    MemoryRetentionError,
    MemoryRetentionReport,
)

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)


def action(path: str, category: str = "completed_task") -> MemoryRetentionAction:
    return MemoryRetentionAction(
        action_id=f"id-{path}",
        category=category,
        operation="trash_tree",
        path=Path(path),
        reason="test",
        status="planned",
        destination=Path("/trash") / Path(path).name,
    )


def plan(actions, errors):
    return MemoryRetentionReport(
        applied=False,
        actions=tuple(actions),
        errors=tuple(errors),
        policy_fingerprint="fp",
    )


def error(code: str, path: str) -> MemoryRetentionError:
    return MemoryRetentionError(code, path, "test")


# LLM: 坏子树的动作必须被剔除，健康子树的动作必须原样保留——这是隔离的核心语义。
# 函数用途: 验证同一计划里坏的那一棵被过滤、其余保留。
def test_only_errored_subtree_actions_are_dropped():
    broken = action("/home/tasks/2026-01-01/broken")
    healthy = action("/home/tasks/2026-01-01/healthy")
    result = _without_errored_subtrees(
        plan([broken, healthy], [error("MEMORY_RETENTION_TASK_STATE_INVALID", "/home/tasks/2026-01-01/broken/work/state.json")])
    )
    kept = [str(item.path) for item in result.actions]
    assert kept == ["/home/tasks/2026-01-01/healthy"]
    assert result.errors  # 错误仍如实回执，不隐藏被保护的事实


# LLM: 错误路径与动作路径互为祖先/后代都算同一棵子树（动作是目录、错误是它下面的 state.json）。
# 函数用途: 验证祖先与后代两个方向的路径重叠都被识别。
def test_error_under_action_and_action_under_error_both_match():
    assert _path_overlaps("/a/b", "/a/b/work/state.json") is True
    assert _path_overlaps("/a/b/work/state.json", "/a/b") is True
    assert _path_overlaps("/a/b", "/a/b") is True
    # 仅前缀相同但不是同一棵树时不算重叠（/a/b 与 /a/bc）。
    assert _path_overlaps("/a/b", "/a/bc") is False


# LLM: 没有错误时计划原样返回，不做任何过滤（不能借隔离之名放宽）。
# 函数用途: 验证无错误时计划不被改写。
def test_no_errors_returns_plan_unchanged():
    item = action("/home/tasks/2026-01-01/any")
    original = plan([item], [])
    assert _without_errored_subtrees(original) is original


# LLM: 错误与动作不重叠时也不应误伤任何动作。
# 函数用途: 验证不相关的错误不会导致动作被丢弃。
def test_unrelated_error_keeps_all_actions():
    item = action("/home/tasks/2026-01-01/healthy")
    original = plan([item], [error("MEMORY_RETENTION_TASK_STATE_INVALID", "/home/tasks/2026-01-01/other/work/state.json")])
    result = _without_errored_subtrees(original)
    assert result is original


# LLM: 策略级错误必须整份拒绝；单棵子树错误不属于策略级。
# 函数用途: 验证策略级错误码集合的判定。
def test_policy_level_error_classification():
    assert _has_policy_level_error((error("MEMORY_RETENTION_POLICY_INVALID", "/p"),)) is True
    assert _has_policy_level_error((error("MEMORY_RETENTION_POLICY_UNREADABLE", "/p"),)) is True
    assert _has_policy_level_error((error("MEMORY_RETENTION_TASK_STATE_INVALID", "/t"),)) is False
    assert _has_policy_level_error(()) is False



# LLM: 这条是 dev 2026-09-29 要的反证：除 _task_actions 外，其它类别会不会在
#   "state.json 读不懂的那棵任务根"底下照常规划动作？答案是**不会**，本用例把原因钉住：
#   - _task_actions：读不懂 state 只记错误、不规划动作；
#   - _tool_output_actions：同一个 _read_state 拿到 None 就 continue，整棵跳过；
#   - _subagent_scratch_actions：先读父任务 state，None 就 continue（父状态是授权前提），
#     所以坏根下的健康子代理 scratch 也不会进计划。
#   因此 apply 的隔离在本仓库里是**纵深防御**——用合法输入构造不出"坏子树里带动作"的计划，
#   no-isolation 变异也就无法被端到端用例杀死。真要杀掉它只能注入伪造计划，那是纯函数测试的活，
#   已由 test_only_errored_subtree_actions_are_dropped 覆盖。
# 函数用途: 证明坏任务根下的健康子代理 scratch 不会进入计划。
def test_unreadable_task_root_yields_no_actions_underneath(tmp_path):
    from agent_py_agent.agent.memory_store.candidates import CandidateService
    from agent_py_agent.agent.memory_store.retention import MemoryRetentionService
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path / "home")
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    service = MemoryRetentionService(home_paths=home, candidates=candidates)

    task_root = Path(home.owner_tasks_dir) / "2026-01-01" / "broken-task"
    work = task_root / "work"
    work.mkdir(parents=True)
    (work / "state.json").write_text("{broken", encoding="utf-8")

    # 健康且超期的子代理运行，带 scratch 与大输出——两个类别在别处都会命中。
    run_root = work / "agents" / "sub-1"
    scratch = run_root / "inbox"
    scratch.mkdir(parents=True)
    (scratch / "data.txt").write_text("payload", encoding="utf-8")
    blobs = work / "blobs" / "tool_outputs"
    blobs.mkdir(parents=True)
    (blobs / "big.txt").write_text("x" * 100, encoding="utf-8")
    old = NOW.timestamp() - 400 * 86400
    (run_root / "state.json").write_text(
        json.dumps({"status": "DONE", "updated_at": old, "task_id": "sub-1"}),
        encoding="utf-8",
    )

    report = service.plan(now=NOW)

    # 坏状态被如实记下。
    assert "MEMORY_RETENTION_TASK_STATE_INVALID" in {
        error.error_code for error in report.errors
    }
    # 关键断言：坏任务根之下一个动作都没有（父状态读不懂即整棵不规划）。
    assert not [a for a in report.actions if str(a.path).startswith(str(task_root))]

    applied = service.apply(now=NOW)
    # 原件全部原地保留。
    assert scratch.exists() and (scratch / "data.txt").read_text(encoding="utf-8") == "payload"
    assert blobs.exists() and (blobs / "big.txt").exists()
    assert not [a for a in applied.actions if str(a.path).startswith(str(task_root))]


# LLM: MR2（9b 复审 2026-09-29）：_iter_task_states 的规范深度过滤不能去掉。运行中任务的 output
#   里嵌套一份「像任务」的目录（带终态 state.json）时，它不能被当成任务根、整棵移走。
# 函数用途: 验证嵌套在非规范深度的「像任务」目录不成为任务根。
def test_nested_task_like_dir_is_not_treated_as_task_root(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.candidates import CandidateService
    from agent_py_agent.agent.memory_store.retention import MemoryRetentionService
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path / "home")
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    service = MemoryRetentionService(home_paths=home, candidates=candidates)

    # 规范深度上的一个运行中任务（不是终态，所以它自己不会被回收）。
    outer = Path(home.owner_tasks_dir) / "2026-01-01" / "running-task"
    (outer / "work").mkdir(parents=True)
    (outer / "work" / "state.json").write_text(
        json.dumps({"task_id": "outer", "status": "RUNNING", "updated_at": NOW.timestamp()}),
        encoding="utf-8",
    )

    # 它 output 里嵌套一份「像任务」的目录，带终态且超期的 state.json —— 深度是非规范的。
    nested = outer / "work" / "blobs" / "tool_outputs" / "nested-task"
    (nested / "work").mkdir(parents=True)
    (nested / "work" / "state.json").write_text(
        json.dumps(
            {"task_id": "nested", "status": "DONE", "updated_at": NOW.timestamp() - 400 * 86400}
        ),
        encoding="utf-8",
    )
    (nested / "work" / "blobs" / "tool_outputs").mkdir(parents=True)
    (nested / "work" / "blobs" / "tool_outputs" / "big.txt").write_text("x" * 50, encoding="utf-8")

    report = service.plan(now=NOW)

    # 嵌套的那棵绝不能被规划成任务根移走。
    assert not [a for a in report.actions if str(a.path).startswith(str(nested))], (
        "非规范深度的「像任务」目录不能被当成任务根"
    )
    assert nested.exists()


# LLM: MR4（9b 复审）：_recovery_roots 不能丢掉 O/audits。audits 下的终态任务同样要被规划回收。
# 函数用途: 验证 audits 根（第一层）确实在扫描范围内。
def test_audits_root_is_scanned_for_terminal_tasks(tmp_path):
    from agent_py_agent.agent.memory_store.candidates import CandidateService
    from agent_py_agent.agent.memory_store.retention import MemoryRetentionService
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path / "home")
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    service = MemoryRetentionService(home_paths=home, candidates=candidates)

    audits_root = Path(home.owner_home_dir) / "audits"
    audit_task = audits_root / "audit-1"
    (audit_task / "work").mkdir(parents=True)
    (audit_task / "work" / "state.json").write_text(
        json.dumps(
            {"task_id": "audit-1", "status": "DONE", "updated_at": NOW.timestamp() - 400 * 86400}
        ),
        encoding="utf-8",
    )

    report = service.plan(now=NOW)

    assert [a for a in report.actions if str(a.path) == str(audit_task)], (
        "audits 下的终态任务必须进入计划"
    )


# LLM: MR5（9b 复审）：audits 的规范深度是 1（audits/<audit_id>），不是 2。深一层不算任务根。
# 函数用途: 验证 audits 深一层不被当成任务根。
def test_audits_root_deeper_level_is_not_a_task_root(tmp_path):
    from agent_py_agent.agent.memory_store.candidates import CandidateService
    from agent_py_agent.agent.memory_store.retention import MemoryRetentionService
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path / "home")
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    service = MemoryRetentionService(home_paths=home, candidates=candidates)

    deeper = Path(home.owner_home_dir) / "audits" / "audit-1" / "nested"
    (deeper / "work").mkdir(parents=True)
    (deeper / "work" / "state.json").write_text(
        json.dumps(
            {"task_id": "deep", "status": "DONE", "updated_at": NOW.timestamp() - 400 * 86400}
        ),
        encoding="utf-8",
    )

    report = service.plan(now=NOW)
    assert not [a for a in report.actions if str(a.path).startswith(str(deeper))], (
        "audits 深一层不是规范任务根"
    )


# LLM: 接上一条：写死成 2 时同一情形会被误判为任务根，所以这两条一起把深度锁住。
# 函数用途: 与上一条互补，确认 audits 第一层才算任务根。
def test_audits_root_first_level_is_a_task_root(tmp_path):
    from agent_py_agent.agent.conversation.workspace_paths import canonical_task_root
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path / "home")
    owner_home = Path(home.owner_home_dir)

    assert canonical_task_root(owner_home, owner_home / "audits" / "a1") is not None
    assert canonical_task_root(owner_home, owner_home / "audits" / "a1" / "deep") is None
    assert canonical_task_root(owner_home, owner_home / "runs" / "2026-01-01" / "k1") is not None
    assert canonical_task_root(owner_home, owner_home / "runs" / "2026-01-01") is None


# LLM: MR6（9b 复审）：CANDIDATES_UNREADABLE 属于策略级错误，必须留在整份拒绝集合里。
# 函数用途: 验证候选账本读不懂时整份拒绝。
def test_candidates_unreadable_is_importable():
    from agent_py_agent.agent.memory_store.retention import _POLICY_LEVEL_ERROR_CODES

    assert "MEMORY_RETENTION_CANDIDATES_UNREADABLE" in _POLICY_LEVEL_ERROR_CODES
    assert _has_policy_level_error(
        (error("MEMORY_RETENTION_CANDIDATES_UNREADABLE", "/candidates.jsonl"),)
    ) is True


# LLM: dev 2026-09-29 第 5 条：_path_overlaps 不能把分隔符写死成 "/"，否则 Windows 路径下
#   识别不到祖先/后代、隔离静默失效。
# 函数用途: 验证 Windows 反斜杠路径的重叠判定。
def test_path_overlaps_handles_windows_separators():
    assert _path_overlaps("C:\\tasks\\a", "C:\\tasks\\a\\work\\state.json") is True
    assert _path_overlaps("C:\\tasks\\a\\work\\state.json", "C:\\tasks\\a") is True
    assert _path_overlaps("C:\\tasks\\a", "C:\\tasks\\ab") is False
    assert _path_overlaps("C:\\tasks\\a", "C:\\tasks\\a") is True
