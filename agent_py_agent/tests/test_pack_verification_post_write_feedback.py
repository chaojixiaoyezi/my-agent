"""能力包 v2 块 8 试点后的写后反馈修正（3a 定 P1–P3）。

- P1：回执带全检查结果里保存的错误样例，并标明截断；
- P2：回执软提示一次改完；
- P3：同一检查对象写后连续失败到上限后只记账，交给收尾那一次返工。

复用块 3 的流程夹具（真实安装的包、规范任务根、替身检查程序）；只读结构化事实，不起 Gateway、不碰真实 owner home。
"""

from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.capability import pack_verification_service as service
from agent_py_agent.agent.capability.pack_verification_hooks import (
    attach_post_write_verification,
    capture_baseline_before_tool,
    closeout_rework_block,
)
from agent_py_agent.agent.capability.pack_verification_report import (
    pack_verification_notice_text,
    run_pack_verification_facts,
)
from agent_py_agent.agent.capability.pack_verifier_runner import (
    MAX_VERIFIER_ERROR_SAMPLES_COUNT,
    PackVerificationResult,
)
from agent_py_agent.agent.common.cancellation import CancellationToken
from agent_py_agent.agent.tooling.runtime_facts import render_tool_runtime_facts
from agent_py_agent.tests.test_pack_verification_service import (
    _tool_result,
    _write,
    build_env,
    install_fake_runner,
)

LIMIT = service.MAX_POST_WRITE_CONSECUTIVE_FAILURES_COUNT


@pytest.fixture
def env(tmp_path, monkeypatch):
    value = build_env(tmp_path, monkeypatch)
    capture_baseline_before_tool(value.agent, value.params, "write_file")
    return value


# 函数用途: 造一个有 errors 条错误、保存了 samples 条样例的失败结果。
def _result(errors: int, samples: int) -> PackVerificationResult:
    items = tuple({"code": f"e{index}", "location": f"S{index:02d}.seconds"} for index in range(samples))
    return PackVerificationResult("check", "story-content", "failed", target="out/d.json", valid=False,
                                  error_counts={"e": errors}, error_samples=items)


# 函数用途: 写一版替身检查程序判为失败的交付物（每版内容不同，复用键不同）。
def _write_bad(env, version, name: str = "out/d.json"):
    return _write(env.workspace / name, {"schema": "delivery.v1", "bad": True, "v": version})


# 函数用途: 走一次写工具成功后的写后核验，返回附进回执的那条摘要。
def _post_write(env, path) -> dict:
    attached = attach_post_write_verification(env.agent, env.params, _tool_result(path))
    [summary] = attached.metadata["handler_details"]["pack_verification"]
    return summary


def test_summary_carries_every_stored_sample_and_flags_truncation():
    complete = _result(errors=5, samples=5).summary()
    assert len(complete["error_samples"]) == 5, "A05 那种 4–5 条错误要一次全给，不能只给前 3 条"
    assert complete["error_samples_truncated"] is False
    truncated = _result(errors=14, samples=MAX_VERIFIER_ERROR_SAMPLES_COUNT).summary()
    assert len(truncated["error_samples"]) == MAX_VERIFIER_ERROR_SAMPLES_COUNT
    assert truncated["error_samples_truncated"] is True, "错误比样例多时要标明还有没列出的"


def test_rework_text_lists_every_sample_and_says_how_many_are_left_out():
    text = service._rework_text([_result(errors=14, samples=10)])
    assert all(f"e{index} @ S{index:02d}.seconds" in text for index in range(10))
    assert "另有 4 条没列出" in text
    assert "没列出" not in service._rework_text([_result(errors=5, samples=5)])


def test_receipt_asks_to_fix_every_listed_error_in_one_write():
    text = render_tool_runtime_facts({"pack_verification": [_result(errors=5, samples=5).summary()]})
    assert "把这次列出的错误在下一次写入里一起改完" in text
    assert "error_samples_truncated=true 表示还有没列出的错误" in text
    assert "reason_code=post_write_feedback_limit" in text


def test_post_write_feedback_pauses_after_consecutive_failures_and_closeout_still_checks(env, monkeypatch):
    calls = install_fake_runner(monkeypatch)
    for version in range(LIMIT):
        assert _post_write(env, _write_bad(env, version))["status"] == "failed"
    paused = _post_write(env, _write_bad(env, LIMIT))
    assert (paused["status"], paused["reason_code"]) == ("not_run", "post_write_feedback_limit")
    assert len(calls) == LIMIT, "到上限后不再跑检查程序"
    last = [row for row in env.ledger.records() if row["kind"] == "result"][-1]
    assert last["fact"]["reason_code"] == "post_write_feedback_limit" and last["key"] == ""
    assert env.repo.events[-1]["payload"]["reason_code"] == "post_write_feedback_limit"
    again = _post_write(env, _write_bad(env, LIMIT + 1))
    assert again["reason_code"] == "post_write_feedback_limit", "暂停记录本身不清零"
    text = closeout_rework_block(env.agent, env.params)
    assert len(calls) == LIMIT + 1 and "placeholder_text" in text, "收尾照常跑真实检查，并给那一次返工"


def test_a_pass_in_between_resets_the_count(env, monkeypatch):
    calls = install_fake_runner(monkeypatch)
    for version in range(LIMIT - 1):
        _post_write(env, _write_bad(env, version))
    good = _write(env.workspace / "out/d.json", {"schema": "delivery.v1", "v": "good"})
    assert _post_write(env, good)["status"] == "passed"
    for version in range(LIMIT):
        assert _post_write(env, _write_bad(env, 100 + version))["status"] == "failed", "中间通过一次就重新数"
    assert _post_write(env, _write_bad(env, 200))["reason_code"] == "post_write_feedback_limit"
    assert len(calls) == 2 * LIMIT


def test_only_the_same_target_counts(env, monkeypatch):
    install_fake_runner(monkeypatch)
    for version in range(LIMIT):
        _post_write(env, _write_bad(env, version))
    other = _write_bad(env, 0, name="out/e.json")
    assert _post_write(env, other)["status"] == "failed", "别的目标的失败不算到这个目标头上"


def test_closeout_failures_do_not_count_toward_the_post_write_limit(env, monkeypatch):
    install_fake_runner(monkeypatch)
    for version in range(LIMIT - 1):
        _post_write(env, _write_bad(env, version))
    _write_bad(env, "shell")  # 不经写工具写的（像 Shell），只有收尾才查到
    assert "placeholder_text" in closeout_rework_block(env.agent, env.params)
    assert _post_write(env, _write_bad(env, LIMIT))["status"] == "failed", "收尾检查的失败不算写后连续失败"


# 9b 复审 P1–P3 必须改 1：没有正常收尾（取消、轮数用完、进程崩溃）时，暂停行不能盖掉前面的真实失败。
def test_without_closeout_the_last_real_failure_survives_the_pause(env, monkeypatch):
    install_fake_runner(monkeypatch)
    for version in range(LIMIT):
        _post_write(env, _write_bad(env, version))
    assert _post_write(env, _write_bad(env, LIMIT))["reason_code"] == "post_write_feedback_limit"
    facts = run_pack_verification_facts(env.agent, env.params)
    [result] = facts["results"]
    assert facts["closeout_checked"] is False
    assert result["status"] == "failed" and result["error_counts"] == {"placeholder_text": 1}, "不能被暂停行盖成 not_run"
    assert "post_write_feedback_limit" not in pack_verification_notice_text(facts)


# 块 6a 与 P1–P3 合并时补（9b 提醒）：取消优先于写后反馈暂停。本 run 已取消、这个目标又刚好到上限时记 cancelled，不记暂停行。
def test_cancelled_run_at_the_limit_records_cancelled_not_the_pause(env, monkeypatch):
    calls = install_fake_runner(monkeypatch)
    for version in range(LIMIT):
        _post_write(env, _write_bad(env, version))
    token = CancellationToken()
    env.params.cancellation_token = token
    token.cancel()
    _post_write(env, _write_bad(env, LIMIT))
    last = [row for row in env.ledger.records() if row["kind"] == "result"][-1]
    assert (last["fact"]["status"], last["fact"]["reason_code"]) == ("cancelled", "verifier_cancelled")
    assert last["key"] != "", "取消结果带真实复用键，不是暂停行"
    assert len(calls) == LIMIT, "已取消不启动检查程序"


# 函数用途: 替身检查程序：按交付物里的 outcome 字段给 failed / error / not_run，供“哪些状态算连续失败”的用例用。
def _install_outcome_runner(monkeypatch):
    calls = []

    def run(request):
        calls.append(request)
        outcome = json.loads(request.target.read_text()).get("outcome", "failed")
        target = request.target.relative_to(request.workspace_root).as_posix()
        common = {"package_version": request.installation.manifest.version, "member": "scripts/check.py", "target": target}
        if outcome == "failed":
            return PackVerificationResult(request.verifier_id, request.installation.manifest.plugin_id, "failed", valid=False,
                                          error_counts={"placeholder_text": 1}, **common)
        reason = "verifier_timeout" if outcome == "error" else "sandbox_unavailable"
        return PackVerificationResult(request.verifier_id, request.installation.manifest.plugin_id, outcome, reason, **common)

    monkeypatch.setattr(service, "run_pack_verifier", run)
    return calls


# 9b 复审 P1–P3 必须改 2：检查程序超时（error）、沙箱不可用（not_run）是环境问题，不算模型写错，不计入连续失败。
def test_error_and_not_run_results_do_not_count_toward_the_streak(env, monkeypatch):
    calls = _install_outcome_runner(monkeypatch)
    sequence = ["failed"] * (LIMIT - 1) + ["error", "not_run", "error"]
    for index, outcome in enumerate(sequence):
        path = _write(env.workspace / "out/d.json", {"schema": "delivery.v1", "outcome": outcome, "v": index})
        assert _post_write(env, path)["status"] == outcome
    last = _write(env.workspace / "out/d.json", {"schema": "delivery.v1", "outcome": "failed", "v": "last"})
    assert _post_write(env, last)["status"] == "failed", "超时和沙箱不可用不能凑进连续失败次数"
    paused = _write(env.workspace / "out/d.json", {"schema": "delivery.v1", "outcome": "failed", "v": "paused"})
    assert _post_write(env, paused)["reason_code"] == "post_write_feedback_limit", "凑够 6 次真实失败才暂停"
    assert len(calls) == len(sequence) + 1
