"""能力包 v2 块 8 试点后的写后反馈修正（3a 定 P1–P3）。

- P1：回执带全检查结果里保存的错误样例，并标明截断；
- P2：回执软提示一次改完；
- P3：同一检查对象写后连续失败到上限后只记账，交给收尾那一次返工。

复用块 3 的流程夹具（真实安装的包、规范任务根、替身检查程序）；只读结构化事实，不起 Gateway、不碰真实 owner home。
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from agent_py_agent.agent.capability import pack_verification_service as service
from agent_py_agent.agent.capability import pack_verifier_redaction as redaction
from agent_py_agent.agent.capability import pack_verifier_runner as runner
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


# ---- 检查程序修改提示（hint）转进返工提示（3a 2026-10-04 统一合同）----


# 函数用途: 造一个错误样例带可选 hint 的失败结果（不经过解析层，直接测返工提示拼装）。
def _hinted_result(samples: list) -> PackVerificationResult:
    return PackVerificationResult("check", "story-content", "failed", target="out/d.json", valid=False,
                                  error_counts={"pointer_mismatch": 1}, error_samples=tuple(samples))


def test_rework_text_keeps_old_text_without_hint_and_appends_hint_with_it():
    plain = _hinted_result([{"code": "pointer_mismatch", "location": "/shots/0/reference_ids"}])
    expected = ("宿主用钉住的能力包原版检查程序核验了本回合写出的交付物，下面这些仍有错误：\n"
                "- out/d.json（story-content  · check）：错误 1 条：pointer_mismatch @ /shots/0/reference_ids\n"
                "请按这些错误码和位置修正交付物，再结束本回合。检查结论以宿主为准；不要复制、改写或自己编写检查程序来代替。")
    assert service._rework_text([plain]) == expected, "没有 hint 时与旧格式逐字相同"
    hinted = _hinted_result([{"code": "pointer_mismatch", "location": "/shots/0/reference_ids",
                              "hint": "所在对象是 SH01，这里写的是 reference_ids"}])
    assert ("pointer_mismatch @ /shots/0/reference_ids"
            "（所在对象是 SH01，这里写的是 reference_ids）") in service._rework_text([hinted])


def test_error_samples_clean_and_truncate_hint():
    raw = "第一行\n第二行\r\n第三行\u2028四行\x07\x1b[31m红\u202e反向\u200f"
    [sample] = runner._error_samples([{"code": "c1", "location": "L", "hint": raw}])
    assert sample["hint"] == "第一行 第二行 第三行 四行[31m红反向"
    [long] = runner._error_samples([{"code": "c1", "location": "L",
                                     "hint": "长" * (runner.MAX_VERIFIER_HINT_CHARS + 50)}])
    assert len(long["hint"]) == runner.MAX_VERIFIER_HINT_CHARS and long["hint"].endswith("…")
    [bare] = runner._error_samples([{"code": "c1", "location": "L"}])
    assert "hint" not in bare, "没有 hint 时不写键，旧形状逐字不变"
    [blank] = runner._error_samples([{"code": "c1", "location": "L", "hint": "\x00\x07"}])
    assert "hint" not in blank, "清洗后为空的 hint 不写键"


def test_hint_does_not_change_verdict_or_counts():
    def stdout(hint):
        error = {"code": "pointer_mismatch", "location": "/shots/0/x"}
        if hint is not None:
            error["hint"] = hint
        return json.dumps({"schema": "pack_verifier_result.v1", "valid": False, "errors": [error], "warnings": []})

    base = {"verifier_id": "check", "package_id": "story-content"}
    plain = runner._parsed_result(stdout(None), dict(base))
    hinted = runner._parsed_result(stdout("期望是 SH01"), dict(base))
    assert (plain.status, plain.valid, plain.error_counts, plain.warning_counts) == (
        hinted.status, hinted.valid, hinted.error_counts, hinted.warning_counts), "判定与计数只看 code"
    assert plain.error_samples[0].get("hint") is None and hinted.error_samples[0]["hint"] == "期望是 SH01"


def test_hint_is_redacted_with_the_same_host_path_replacements():
    request = types.SimpleNamespace(workspace_root="/w/ws", target=Path("/w/ws/out/d.json"), inputs=())
    result = PackVerificationResult("check", "story-content", "failed", error_samples=(
        {"code": "c1", "location": "L", "hint": "文件 /w/ws/out/d.json 里的字段错了"},
        {"code": "c2", "location": "L2", "hint": "期望是 SH01"},
    ))
    redacted = redaction.redact_error_samples(result, request, "/tmp/pack-verifier-abc")
    assert redacted.error_samples[0]["hint"] == "文件 out/d.json 里的字段错了"
    assert redacted.error_samples[1]["hint"] == "期望是 SH01"


def test_closeout_rework_text_carries_hint_end_to_end(env, monkeypatch):
    def run(request):
        bad = json.loads(request.target.read_text()).get("bad") is True
        target = request.target.relative_to(request.workspace_root).as_posix()
        samples = ({"code": "pointer_mismatch", "location": "/shots/0/reference_ids",
                    "hint": "所在对象是 SH01，这里写的是 reference_ids"},) if bad else ()
        return PackVerificationResult(request.verifier_id, request.installation.manifest.plugin_id,
                                      "failed" if bad else "passed",
                                      package_version=request.installation.manifest.version,
                                      member="scripts/check.py", target=target, valid=not bad,
                                      error_counts={"pointer_mismatch": 1} if bad else {}, error_samples=samples)

    monkeypatch.setattr(service, "run_pack_verifier", run)
    _write_bad(env, 0)
    text = closeout_rework_block(env.agent, env.params)
    assert "pointer_mismatch @ /shots/0/reference_ids（所在对象是 SH01，这里写的是 reference_ids）" in text
    last = [row for row in env.ledger.records() if row["kind"] == "result"][-1]
    assert last["fact"]["error_samples"][0]["hint"] == "所在对象是 SH01，这里写的是 reference_ids"
