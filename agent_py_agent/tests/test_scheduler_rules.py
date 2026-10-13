from __future__ import annotations

from copy import deepcopy

import pytest

from agent_py_agent.agent.scheduler.rules import normalize_rules
from agent_py_agent.agent.scheduler.schedule import ScheduleValidationError


def _rules():
    return [
        {"id": "rule1", "name": "每五分钟探测", "use_llm": False,
         "schedule": {"kind": "every", "every_seconds": 300, "anchor_at": 1000, "timezone": "UTC"},
         "tool": {"name": "arbitrary_declared_tool", "arguments": {"target": "synthetic"}}},
        {"id": "rule2", "name": "每半小时分析", "use_llm": True, "prompt": "分析合成数据",
         "schedule": {"kind": "every", "every_seconds": 1800, "anchor_at": 1000, "timezone": "UTC"},
         "depends_on": ["rule1"], "condition": {"rule_id": "rule1", "pointer": "/data/alive", "op": "eq", "value": True}},
        {"id": "rule3", "name": "九点十八点汇报", "use_llm": True, "prompt": "汇总合成记录",
         "schedule": {"kind": "cron", "expression": "0 9,18 * * *", "timezone": "Asia/Shanghai"},
         "depends_on": ["rule2"], "delivery": {"mode": "origin"}},
    ]


def test_one_monitor_has_distinct_time_llm_delivery_and_dependency_rules():
    raw = _rules()
    before = deepcopy(raw)
    rules = normalize_rules(raw)
    assert raw == before
    assert [r["use_llm"] for r in rules] == [False, True, True]
    assert rules[0]["tool"]["name"] == "arbitrary_declared_tool"
    assert rules[0]["delivery"] is None and rules[2]["delivery"] == {"mode": "origin"}
    assert rules[1]["condition"]["max_age_seconds"] == 0
    assert rules[2]["schedule"]["expression"] == "0 9,18 * * *"


@pytest.mark.parametrize("change", ["duplicate", "cycle", "foreign", "missing_prompt", "ambiguous_action", "missing_tool", "host_identity", "text_llm", "condition_dependency"])
def test_invalid_rules_fail_before_repository_or_tools(change):
    rules = _rules()
    if change == "duplicate":
        rules[2]["id"] = "rule1"
    elif change == "cycle":
        rules[0]["depends_on"] = ["rule3"]
    elif change == "foreign":
        rules[2]["depends_on"] = ["another-monitor-rule"]
    elif change == "missing_prompt":
        rules[1]["prompt"] = ""
    elif change == "ambiguous_action":
        rules[1]["tool"] = rules[0]["tool"]
    elif change == "missing_tool":
        rules[0].pop("tool")
    elif change == "host_identity":
        rules[0]["tool"]["arguments"]["__run_scope"] = {"owner_id": "other"}
    elif change == "text_llm":
        rules[0]["use_llm"] = "NO"
    else:
        rules[1]["depends_on"] = []
    with pytest.raises(ScheduleValidationError):
        normalize_rules(rules)


@pytest.mark.parametrize("interval,valid", [(59, False), (60, True), (31622400, True), (31622401, False)])
def test_rule_time_bounds_are_original_schedule_contract(interval, valid):
    rules = _rules()[:1]
    rules[0]["schedule"]["every_seconds"] = interval
    if valid:
        assert normalize_rules(rules)[0]["schedule"]["every_seconds"] == interval
    else:
        with pytest.raises(ScheduleValidationError):
            normalize_rules(rules)
