"""纯评估器的确定性行为回归；不用网络、sleep 或真实用户 HOME。"""

from __future__ import annotations

import builtins
import copy
import itertools
import socket
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agent_py_agent.agent.tooling import policy_rules
from agent_py_agent.agent.tooling.policy_rules import (
    PolicyDecision,
    evaluate_command_rules,
    evaluate_scope_rules,
)


def command_rule(rule_id="command", **changes):
    rule = {
        "rule_id": rule_id, "name": rule_id, "tool": "run_command", "decision": "deny",
        "priority": 0, "enabled": True, "created_at": 0, "updated_at": 0,
    }
    return {**rule, **changes}


def scope_rule(rule_id="scope", kind="path_allow", pattern="/repo", **changes):
    return {
        "rule_id": rule_id, "kind": kind, "pattern": pattern,
        "priority": 0, "enabled": True, **changes,
    }


def constraint(constraint_id="tools", kind="tool_allowlist", tools=("run_command",), **changes):
    return {
        "constraint_id": constraint_id, "task_id": "task-1", "kind": kind,
        "tools": list(tools), **changes,
    }


def facts(**changes):
    return {
        "tool": "run_command", "argv": ["git", "status"], "paths": [], "domains": [],
        "allowed_tools": {"run_command", "read_file"}, "task_id": "task-1", **changes,
    }


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")

    def no_network(*args, **kwargs):
        pytest.fail("纯规则评估不得发起网络或 DNS 请求")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)


@pytest.mark.parametrize("evaluate", [evaluate_command_rules, evaluate_scope_rules])
def test_no_rules_default_allow_and_frozen_result(evaluate):
    result = evaluate({}, facts())
    assert result == PolicyDecision("allow", "", "", ())
    assert isinstance(result.reasons, tuple)
    with pytest.raises(FrozenInstanceError):
        result.decision = "deny"


@pytest.mark.parametrize(
    ("rule_changes", "fact_changes", "matched"),
    [
        ({"tool": "run_*"}, {}, True),
        ({"tool": "RUN_*"}, {}, False),
        ({"tool": "^run_.*$"}, {}, False),
        ({"tool": "read_file"}, {}, False),
        ({"command_argv0": "git"}, {}, True),
        ({"command_argv0": "git"}, {"argv": ["git-lfs", "status"]}, False),
        ({"command_argv0": "git"}, {"argv": ["/usr/bin/git", "status"]}, False),
        ({"command_prefix": ["git", "status"]}, {}, True),
        ({"command_prefix": ["git", "stat"]}, {}, False),
        ({"command_prefix": ["git", ".*"]}, {}, False),
        ({"command_prefix": "git status"}, {}, False),
        ({"enabled": False}, {}, False),
        ({"task_id": "another-task"}, {}, False),
        ({"command_prefix": ["git"]}, {"argv": []}, False),
        ({}, {"argv": []}, True),
    ],
)
def test_command_match_is_only_glob_exact_and_token_prefix(rule_changes, fact_changes, matched):
    result = evaluate_command_rules({"rules": [command_rule(**rule_changes)]}, facts(**fact_changes))
    assert result.decision == ("deny" if matched else "allow")
    assert result.rule_id == ("command" if matched else "")


@pytest.mark.parametrize(
    ("prefix", "path", "matched"),
    [
        ("/repo", "/repo/file", True),
        ("/repo/", "/repo", True),
        ("/repo/./src/..", "/repo/src/../file", True),
        ("/repo", "/repository/file", False),
        ("/repo", "/repo/../elsewhere", False),
        ("/", "/anywhere/file", True),
        ("/repo/*", "/repo/src/file", False),
        ("repo", "/repo/file", False),
        ("//", "/repo/file", False),
        ("//", "//repo/file", True),
    ],
)
def test_command_path_prefix_uses_posix_segment_boundary(prefix, path, matched):
    result = evaluate_command_rules(
        {"rules": [command_rule(path_prefix=prefix)]}, facts(paths=[path]),
    )
    assert result.decision == ("deny" if matched else "allow")


def test_command_checks_all_segments_and_keeps_entry_argv():
    snapshot = {"rules": [command_rule(command_prefix=["rm", "-rf"], reason="删除需要拦截")]}
    result = evaluate_command_rules(snapshot, facts(
        argv=["sh", "-c", "git status; rm -rf target"],
        commands=[["git", "status"], ["rm", "-rf", "target"]],
    ))
    assert result.rule_id == "command"
    assert "删除需要拦截" in result.reasons[0]
    assert evaluate_command_rules(snapshot, facts(argv=["rm", "-rf", "target"], commands=[])).decision == "deny"


def test_command_predicates_must_match_the_same_segment():
    rule = command_rule(command_argv0="git", command_prefix=["rm", "-rf"])
    result = evaluate_command_rules({"rules": [rule]}, facts(commands=[["rm", "-rf"]]))
    assert result.decision == "allow"


@pytest.mark.parametrize("other", ["allow", "ask", "judge"])
def test_parent_deny_beats_task_allow_or_confirmation(other):
    result = evaluate_command_rules({
        "rules": [command_rule("parent", priority=0)],
        "task_rules": [command_rule("child", decision=other, priority=1000)],
    }, facts())
    assert (result.decision, result.rule_id, result.rule_source) == ("deny", "parent", "global")


@pytest.mark.parametrize("decision", ["ask", "judge"])
def test_ask_and_judge_both_outrank_allow(decision):
    result = evaluate_command_rules({"rules": [
        command_rule("allow", decision="allow", priority=1000),
        command_rule("review", decision=decision, priority=0),
    ]}, facts())
    assert (result.decision, result.rule_id) == (decision, "review")


@pytest.mark.parametrize(
    ("rules", "task_rules", "expected"),
    [
        ([command_rule("global", priority=1000)], [command_rule("task")], "task"),
        ([command_rule("low"), command_rule("high", priority=1)], [], "high"),
        ([command_rule("short", command_prefix=["git"]), command_rule("long", command_prefix=["git", "status"])], [], "long"),
        ([command_rule("broad", path_prefix="/repo"), command_rule("narrow", path_prefix="/repo/src")], [], "narrow"),
        ([command_rule("z"), command_rule("a")], [], "a"),
        ([command_rule("short-high", priority=1), command_rule("long-low", command_prefix=["git", "status"])], [], "short-high"),
        ([command_rule("argv", command_prefix=["git", "status"]), command_rule("path", command_prefix=["git"], path_prefix="/repo/src")], [], "argv"),
        ([command_rule("judge", decision="judge"), command_rule("ask", decision="ask")], [], "ask"),
    ],
)
def test_command_winner_respects_each_tie_breaker(rules, task_rules, expected):
    for ordered in itertools.permutations(rules):
        result = evaluate_command_rules({"rules": ordered, "task_rules": task_rules}, facts(paths=["/repo/src/a.py"]))
        assert result.rule_id == expected


@pytest.mark.parametrize("kind", ["path_allow", "path_deny"])
def test_scope_path_rules_use_boundary_and_normalization(kind):
    snapshot = {"rules": [scope_rule(kind=kind, pattern="/repo/./src/..", note="目录边界")]}
    inside = evaluate_scope_rules(snapshot, facts(paths=["/repo/src/../file"]))
    outside = evaluate_scope_rules(snapshot, facts(paths=["/repository/file"]))
    assert inside.decision == ("allow" if kind == "path_allow" else "deny")
    assert outside.decision == ("deny" if kind == "path_allow" else "allow")
    assert inside.rule_id == "scope"
    assert "目录边界" in inside.reasons[0]


@pytest.mark.parametrize("kind", ["path", "domain"])
def test_allowlist_requires_every_resource(kind):
    pattern = "/repo" if kind == "path" else "example.com"
    values = ["/repo/good", "/outside/bad"] if kind == "path" else ["api.example.com", "other.test"]
    key = "paths" if kind == "path" else "domains"
    snapshot = {"rules": [scope_rule(kind=kind + "_allow", pattern=pattern)]}
    assert evaluate_scope_rules(snapshot, facts(**{key: values[:1]})).decision == "allow"
    assert evaluate_scope_rules(snapshot, facts(**{key: values})).decision == "deny"
    assert evaluate_scope_rules(snapshot, facts(**{key: values[::-1]})).decision == "deny"


def test_allow_entries_in_one_layer_form_union_not_intersection():
    snapshot = {"rules": [
        scope_rule("src", pattern="/repo/src"), scope_rule("tests", pattern="/repo/tests"),
    ]}
    assert evaluate_scope_rules(snapshot, facts(paths=["/repo/src/a", "/repo/tests/a"])).decision == "allow"


@pytest.mark.parametrize(
    ("pattern", "domain", "matched"),
    [
        ("Example.COM.", "api.EXAMPLE.com.", True),
        ("example.com", "example.com", True),
        ("example.com", "evilexample.com", False),
        ("example.com", "example.com.evil.test", False),
        ("BÜCHER.Example.", "api.xn--bcher-kva.example", True),
        ("xn--bcher-kva.example", "api.bücher.example.", True),
        ("example.com", "other.test", False),
    ],
)
def test_domains_use_idna_lowercase_trailing_dot_and_label_boundary(pattern, domain, matched):
    snapshot = {"rules": [scope_rule(kind="domain_deny", pattern=pattern)]}
    assert evaluate_scope_rules(snapshot, facts(domains=[domain])).decision == ("deny" if matched else "allow")


@pytest.mark.parametrize("family", ["path", "domain"])
def test_task_allow_can_narrow_but_cannot_widen_parent(family):
    parent, child, widened = ("/repo", "/repo/src", "/") if family == "path" else ("example.com", "api.example.com", "com")
    inside, outside = ("/repo/src/file", "/repo/tests/file") if family == "path" else ("v1.api.example.com", "www.example.com")
    key = "paths" if family == "path" else "domains"
    snapshot = {
        "rules": [scope_rule("parent", family + "_allow", parent)],
        "task_rules": [scope_rule("child", family + "_allow", child)],
    }
    permitted = evaluate_scope_rules(snapshot, facts(**{key: [inside]}))
    assert (permitted.decision, permitted.rule_id, permitted.rule_source) == ("allow", "child", "task")
    rejected = evaluate_scope_rules(snapshot, facts(**{key: [outside]}))
    assert (rejected.decision, rejected.rule_id, rejected.rule_source) == ("deny", "child", "task")
    snapshot["task_rules"] = [scope_rule("widen", family + "_allow", widened)]
    ignored = evaluate_scope_rules(snapshot, facts(**{key: [outside]}))
    assert ignored.decision == "allow"
    assert ignored.rule_id == "parent"
    assert any("忽略" in reason and "拓宽" in reason for reason in ignored.reasons)
    escaped = "/elsewhere" if family == "path" else "evil.test"
    assert evaluate_scope_rules(snapshot, facts(**{key: [escaped]})).decision == "deny"


def test_task_allow_without_parent_allow_still_restricts():
    snapshot = {"task_rules": [scope_rule("child", pattern="/repo")]}
    assert evaluate_scope_rules(snapshot, facts(paths=["/repo/file"])).decision == "allow"
    assert evaluate_scope_rules(snapshot, facts(paths=["/elsewhere"])).decision == "deny"


def test_valid_task_allow_survives_a_widening_sibling_rule():
    snapshot = {
        "rules": [scope_rule("parent", pattern="/repo")],
        "task_rules": [scope_rule("invalid-widen", pattern="/"), scope_rule("narrow", pattern="/repo/src")],
    }
    result = evaluate_scope_rules(snapshot, facts(paths=["/repo/tests/test.py"]))
    assert result.decision == "deny"
    assert result.rule_id == "narrow"
    assert any("忽略" in reason for reason in result.reasons)


def test_parent_deny_is_not_overridden_by_child_scope_allow():
    snapshot = {
        "rules": [scope_rule("parent-deny", "path_deny", "/repo/private")],
        "task_rules": [scope_rule("child-allow", pattern="/repo/private", priority=1000)],
    }
    result = evaluate_scope_rules(snapshot, facts(paths=["/repo/private/secret"]))
    assert (result.decision, result.rule_id, result.rule_source) == ("deny", "parent-deny", "global")


def test_scope_snapshot_union_filters_disabled_and_other_task_rows():
    snapshot = {"rules": [
        scope_rule("disabled", "path_deny", "/repo", enabled=False),
        scope_rule("foreign", "path_deny", "/repo", task_id="foreign-task"),
        scope_rule("bound", "path_deny", "/repo", task_id="task-1"),
    ], "task_rules": [scope_rule("other", "domain_deny", "example.com")]}
    path = evaluate_scope_rules(snapshot, facts(paths=["/repo/file"]))
    domain = evaluate_scope_rules(snapshot, facts(domains=["api.example.com"]))
    assert (path.rule_id, path.rule_source) == ("bound", "task")
    assert (domain.rule_id, domain.rule_source) == ("other", "task")


def test_scope_deny_tie_breaking_and_warnings_are_permutation_stable():
    rules = [
        scope_rule("z", "path_deny", "/repo"),
        scope_rule("a", "path_deny", "/repo/src"),
        scope_rule("parent", "path_allow", "/repo"),
    ]
    task_rules = [
        scope_rule("widen-path", "path_allow", "/"),
        scope_rule("widen-domain", "domain_allow", "com"),
    ]
    rules.append(scope_rule("domain-parent", "domain_allow", "example.com"))
    expected = evaluate_scope_rules({"rules": rules, "task_rules": task_rules}, facts(paths=["/repo/src/file"]))
    assert expected.rule_id == "a"
    for ordering in itertools.permutations(rules):
        for task_order in itertools.permutations(task_rules):
            assert evaluate_scope_rules({"rules": ordering, "task_rules": task_order}, facts(paths=["/repo/src/file"])) == expected


@pytest.mark.parametrize(
    ("snapshot", "fact_changes", "decision", "rule_id"),
    [
        ({"constraints": [constraint()]}, {}, "allow", "tools"),
        ({"constraints": [constraint(tools=["read_file"])]}, {}, "deny", "tools"),
        ({"constraints": [constraint(tools=[])]}, {}, "deny", "tools"),
        ({"constraints": [constraint(kind="tool_denylist")]}, {}, "deny", "tools"),
        ({"constraints": [constraint(kind="tool_denylist", tools=["read_file"])]}, {}, "allow", ""),
        ({"constraints": [constraint(task_id="other")]}, {}, "allow", ""),
        ({"constraints": [constraint(tools=["run_*"])]}, {}, "deny", "tools"),
        ({"constraints": [constraint()]}, {"allowed_tools": {"read_file"}}, "deny", ""),
        ({}, {"allowed_tools": set()}, "deny", ""),
    ],
)
def test_tool_constraints_only_intersect_existing_capabilities(snapshot, fact_changes, decision, rule_id):
    result = evaluate_scope_rules(snapshot, facts(**fact_changes))
    assert (result.decision, result.rule_id) == (decision, rule_id)


def test_multiple_tool_allowlists_intersect_and_deny_wins():
    snapshot = {"constraints": [
        constraint("a", tools=["run_command", "read_file"]),
        constraint("b", tools=["read_file"]),
    ]}
    assert evaluate_scope_rules(snapshot, facts()).rule_id == "b"
    snapshot["constraints"][1] = constraint("b", kind="tool_denylist")
    assert evaluate_scope_rules(snapshot, facts()).decision == "deny"


def test_missing_capability_overlay_does_not_invent_a_rule():
    current_facts = facts()
    del current_facts["allowed_tools"]
    assert evaluate_scope_rules({}, current_facts) == PolicyDecision("allow")


@pytest.mark.parametrize(
    ("subpaths", "paths", "root", "decision"),
    [
        (["src"], ["/repo/src/a.py"], "/repo", "allow"),
        (["src"], ["/repo/src/../elsewhere"], "/repo", "deny"),
        (["src"], ["/repo/src/a", "/repo/tests/a"], "/repo", "deny"),
        (["src"], ["/repo/src-extra/a"], "/repo", "deny"),
        (["src", "tests"], ["/repo/src/a", "/repo/tests/a"], "/repo", "allow"),
        (["."], ["/repo/file"], "/repo", "allow"),
        (["src"], ["/repo/src/a"], "", "deny"),
        (["../outside"], ["/repo/file"], "/repo", "deny"),
        (["src/../tests"], ["/repo/tests/file"], "/repo", "deny"),
        (["/repo/src"], ["/repo/src/a"], "/repo", "deny"),
        (["-src"], ["/repo/-src/a"], "/repo", "deny"),
        (["src\0bad"], ["/repo/file"], "/repo", "deny"),
        ([], ["/anywhere"], "", "allow"),
        (["src"], [], "", "allow"),
    ],
)
def test_task_scope_confines_relative_subpaths_without_io(subpaths, paths, root, decision):
    snapshot = {"task_scope": {"task_id": "task-1", "workspace_subpaths": subpaths, "network": "inherit"}}
    result = evaluate_scope_rules(snapshot, facts(paths=paths, workspace_root=root))
    assert result.decision == decision
    if decision == "deny":
        assert result.rule_source == "task"
        assert any("task_scope" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    ("network", "rules", "domains", "decision"),
    [
        ("none", [], ["example.com"], "deny"),
        ("none", [], [], "allow"),
        ("inherit", [], ["example.com"], "allow"),
        ("allowlist", [], ["example.com"], "deny"),
        ("allowlist", [scope_rule(kind="domain_allow", pattern="example.com")], ["api.example.com"], "allow"),
        ("allowlist", [scope_rule(kind="domain_allow", pattern="example.com")], ["example.com", "other.test"], "deny"),
    ],
)
def test_task_scope_network_modes(network, rules, domains, decision):
    snapshot = {"rules": rules, "task_scope": {"task_id": "task-1", "workspace_subpaths": [], "network": network}}
    assert evaluate_scope_rules(snapshot, facts(domains=domains)).decision == decision


def test_foreign_task_scope_does_not_leak_into_current_task():
    snapshot = {"task_scope": {"task_id": "foreign", "workspace_subpaths": ["src"], "network": "none"}}
    assert evaluate_scope_rules(snapshot, facts(paths=["/outside"], domains=["example.com"])).decision == "allow"


@pytest.mark.parametrize(
    ("rules", "fact_changes"),
    [
        ([scope_rule(pattern="relative")], {"paths": ["/repo/file"]}),
        ([scope_rule(kind="path_deny", pattern="relative")], {"paths": ["/repo/file"]}),
        ([scope_rule(kind="domain_allow", pattern="*.example.com")], {"domains": ["api.example.com"]}),
        ([scope_rule(kind="domain_deny", pattern="^example.*$")], {"domains": ["example.com"]}),
        ([], {"paths": ["relative"]}),
        ([], {"paths": ["/repo/\0bad"]}),
        ([], {"domains": ["https://example.com"]}),
        ([], {"domains": ["..example.com"]}),
    ],
)
def test_invalid_scope_configuration_or_resource_fails_closed(rules, fact_changes):
    result = evaluate_scope_rules({"rules": rules}, facts(**fact_changes))
    assert result.decision == "deny"
    assert any("无效" in reason for reason in result.reasons)


def test_task_scope_success_cannot_broaden_a_global_allowlist():
    snapshot = {
        "rules": [scope_rule(pattern="/repo/src")],
        "task_scope": {"task_id": "task-1", "workspace_subpaths": ["."], "network": "inherit"},
    }
    assert evaluate_scope_rules(snapshot, facts(paths=["/repo/tests/a"], workspace_root="/repo")).decision == "deny"


@pytest.mark.parametrize("evaluate", [evaluate_command_rules, evaluate_scope_rules])
def test_evaluators_do_not_mutate_inputs(evaluate):
    snapshot = {
        "rules": [command_rule(), scope_rule()],
        "task_rules": [scope_rule("task", pattern="/repo/src")],
        "constraints": [constraint()],
        "task_scope": {"task_id": "task-1", "workspace_subpaths": ["src"], "network": "none"},
    }
    current_facts = facts(paths=["/repo/src/a"], workspace_root="/repo", commands=[["git", "status"]])
    before = copy.deepcopy((snapshot, current_facts))
    evaluate(snapshot, current_facts)
    assert (snapshot, current_facts) == before


class OnePassRules:
    """同时阻止重新迭代与原地排序，不用耗时阈值判断热路径复杂度。"""

    def __init__(self, rows):
        self.rows = rows
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        assert self.iterations == 1, "快照规则不应被重复扫描"
        yield from self.rows

    def sort(self, *args, **kwargs):
        pytest.fail("不应排序全部规则")


@pytest.mark.parametrize("evaluate", [evaluate_command_rules, evaluate_scope_rules])
def test_rule_selection_is_one_pass_without_sorting(evaluate, monkeypatch):
    def no_sorted(*args, **kwargs):
        pytest.fail("不应通过 sorted 排序全部规则")

    monkeypatch.setattr(policy_rules, "sorted", no_sorted, raising=False)
    rows = [command_rule(f"command-{index:03d}") for index in range(128)]
    if evaluate is evaluate_scope_rules:
        rows = [scope_rule(f"scope-{index:03d}", "path_deny", "/repo") for index in range(128)]
    global_rules = OnePassRules(rows[::-1])
    task_rules = OnePassRules([])
    result = evaluate({"rules": global_rules, "task_rules": task_rules}, facts(paths=["/repo/file"]))
    assert result.rule_id == ("command-000" if evaluate is evaluate_command_rules else "scope-000")
    assert global_rules.iterations == task_rules.iterations == 1


def test_pure_evaluation_never_reads_files_resolves_symlinks_or_uses_clock(monkeypatch):
    # 先加载标准库 IDNA codec；评估器不能借路径检查触发实际文件系统观察。
    "bücher.example".encode("idna")

    def forbidden(*args, **kwargs):
        pytest.fail("纯评估不允许文件系统 I/O、符号链接解析或时钟读取")

    snapshot = {
        "rules": [command_rule(command_argv0="rm"), scope_rule()],
        "task_rules": [scope_rule("domain", "domain_allow", "bücher.example")],
        "task_scope": {"task_id": "task-1", "workspace_subpaths": ["src"], "network": "allowlist"},
    }
    current_facts = facts(paths=["/repo/src/file"], domains=["api.xn--bcher-kva.example"], workspace_root="/repo")
    with monkeypatch.context() as guard:
        guard.setattr(builtins, "open", forbidden)
        guard.setattr(Path, "resolve", forbidden)
        guard.setattr(Path, "stat", forbidden)
        guard.setattr(time, "time", forbidden)
        guard.setattr(time, "monotonic", forbidden)
        guard.setattr(time, "sleep", forbidden)
        assert evaluate_command_rules(snapshot, current_facts).decision == "allow"
        assert evaluate_scope_rules(snapshot, current_facts).decision == "allow"


def test_snapshot_to_both_decisions_smoke():
    """真实公共 API 冒烟：同一混合快照完成命令与范围两层纯评估。"""
    snapshot = {
        "rules": [
            command_rule("rm-deny", command_prefix=["rm", "-rf"]),
            scope_rule("workspace", pattern="/repo"),
            scope_rule("secrets", "path_deny", "/repo/src/secrets"),
        ],
        "task_rules": [scope_rule("task-src", pattern="/repo/src")],
        "constraints": [constraint()],
        "task_scope": {"task_id": "task-1", "workspace_subpaths": ["src"], "network": "none"},
    }
    safe = facts(argv=["git", "status"], paths=["/repo/src/main.py"], workspace_root="/repo")
    assert evaluate_command_rules(snapshot, safe).decision == "allow"
    assert evaluate_scope_rules(snapshot, safe).decision == "allow"
    dangerous = {**safe, "commands": [["git", "status"], ["rm", "-rf", "build"]]}
    assert evaluate_command_rules(snapshot, dangerous).rule_id == "rm-deny"
    secret = {**safe, "paths": ["/repo/src/main.py", "/repo/src/secrets/key"]}
    assert evaluate_scope_rules(snapshot, secret).rule_id == "secrets"
