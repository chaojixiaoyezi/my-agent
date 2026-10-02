"""decision_patch / decision_reset 接受可选 reason（P16/J18）：只记录与展示，不参与任何机器判断。

锁定：
- 带 reason 的 patch/reset 成功，且 reason 进模型档案旁的修改账本（/settings history 一类入口可显示）；
- 超长 reason 截断到 200 字并在回执标明 reason_truncated；
- 其它多余字段仍整笔拒绝并在 unknown_fields 列出（不替模型删字段）；
- 相同 changes 带不同 reason，overrides/revision 完全一致（机器逻辑不受 reason 影响）。
变异覆盖：去掉 reason 允许、不写账本、不截断、reason 影响结果，都会被上述用例抓住。
"""
import json

from agent_py_agent.agent.settings.decision_settings import execute_decision_settings_operation
from agent_py_agent.agent.settings.parameter_changes import profile_change_history
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.tests.test_decision_settings import host_at


# 函数用途: 读 owner/thread revision 与某覆盖字段的有效值。
def _read(host, field="background_timeout_seconds"):
    view = execute_decision_settings_operation(host, "read", {})
    return view["revision"], view["effective"][field]


# 函数用途: 读最新一条 decision_settings 账本记录。
def _latest_decision_record(host):
    records = profile_change_history(host.home_paths)
    return records[0] if records else None


def test_patch_with_reason_succeeds_and_records_it(tmp_path):
    host = host_at(tmp_path)
    revision, _before = _read(host)
    result = UserConfigTool(host).execute({
        "action": "decision_patch", "expected_revision": revision,
        "changes": {"background_timeout_seconds": 15}, "reason": "用户要求把后台等待调到 15 秒"})
    assert result.ok
    after, seconds = _read(host)
    assert seconds == 15 and after == {"owner": revision["owner"] + 1, "thread": revision["thread"]}
    record = _latest_decision_record(host)
    assert record["key"] == "decision_settings" and record["action"] == "decision_patch"
    assert record["reason"] == "用户要求把后台等待调到 15 秒"
    assert record["value"] == "background_timeout_seconds"


def test_reset_with_reason_succeeds_and_records_it(tmp_path):
    host = host_at(tmp_path)
    execute_decision_settings_operation(host, "patch", {
        "expected_revision": {"owner": 0, "thread": 0}, "changes": {"background_timeout_seconds": 15}})
    revision, seconds = _read(host)
    assert seconds == 15
    result = UserConfigTool(host).execute({
        "action": "decision_reset", "expected_revision": revision,
        "fields": ["background_timeout_seconds"], "reason": "不再需要自定义等待"})
    assert result.ok
    _revision, seconds = _read(host)
    assert seconds != 15
    record = _latest_decision_record(host)
    assert record["action"] == "decision_reset" and record["reason"] == "不再需要自定义等待"
    assert record["value"] == "background_timeout_seconds"


def test_oversized_reason_is_truncated_and_flagged(tmp_path):
    host = host_at(tmp_path)
    revision, _before = _read(host)
    long_reason = "超长原因" * 200  # 600 字
    result = UserConfigTool(host).execute({
        "action": "decision_patch", "expected_revision": revision,
        "changes": {"background_timeout_seconds": 30}, "reason": long_reason})
    assert result.ok
    report = json.loads(result.output)
    assert report["reason_truncated"] is True
    assert report["recorded_reason"] == long_reason[:200]
    record = _latest_decision_record(host)
    assert record["reason"] == long_reason[:200] and len(record["reason"]) == 200


def test_other_extra_fields_are_still_rejected_and_named(tmp_path):
    host = host_at(tmp_path)
    revision, before = _read(host)
    result = UserConfigTool(host).execute({
        "action": "decision_patch", "expected_revision": revision,
        "changes": {"background_timeout_seconds": 15}, "bogus": 1})
    payload = json.loads(result.output)
    assert not result.ok and payload["unknown_fields"] == ["bogus"]
    assert payload["allowed_fields"] == ["changes", "expected_revision", "reason", "scope"]
    assert _read(host) == (revision, before)  # 什么都没写
    assert _latest_decision_record(host) is None  # 也没记账


def test_same_changes_with_different_reasons_have_identical_effect(tmp_path):
    host = host_at(tmp_path)
    revision, _before = _read(host)
    first = UserConfigTool(host).execute({
        "action": "decision_patch", "expected_revision": revision,
        "changes": {"background_timeout_seconds": 15}, "reason": "原因甲"})
    assert first.ok
    revision_b, seconds = _read(host)
    assert seconds == 15
    second = UserConfigTool(host).execute({
        "action": "decision_patch", "expected_revision": revision_b,
        "changes": {"background_timeout_seconds": 15}, "reason": "原因乙"})
    assert second.ok
    revision_c, seconds = _read(host)
    # 相同 changes 带不同 reason：生效值一致，revision 只按提交递增，reason 不进覆盖也不改结果
    assert seconds == 15 and revision_c == {"owner": revision_b["owner"] + 1, "thread": revision_b["thread"]}
    records = profile_change_history(host.home_paths)
    assert [item["reason"] for item in records[:2]] == ["原因乙", "原因甲"]