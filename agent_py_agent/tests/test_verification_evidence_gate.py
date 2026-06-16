from __future__ import annotations

"""防回归：uncontracted 路径下「任务要求跑测试却无测试执行证据」的一次性提醒门。

根因（native 回归实锤）：弱模型把可运行代码直接写进 output/ 后停手，uncontracted
closeout 只守「产物能不能打开」即判完成，**不验证用户白纸黑字要的「真实运行测试确保
全过」**。本门只在三要素同时成立时打回一次（任务要求跑测试 + 零测试执行证据 + 交了
代码产物），且幂等放行，绝不卡死；纯报告/文档/数据类任务没有代码产物，永不触发。
本测试锁死「该拦的拦、不该拦的（纯产出、已跑测试、无要求、二次）放行」四象限。
"""

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.delivery_closeout.verification_evidence_gate import (
    verification_evidence_rework,
)


def _request(prompt: str, archive_tool_calls: list[dict] | None = None, tool_context: list | None = None):
    return SimpleNamespace(
        params=SimpleNamespace(
            root_user_prompt=prompt,
            user_prompt=prompt,
            archive_tool_calls=list(archive_tool_calls or []),
            tool_context=tool_context if tool_context is not None else [],
        ),
    )


def _report(artifacts: list[dict], workspace_root: Path) -> dict:
    return {"artifacts": list(artifacts), "workspace_root": str(workspace_root), "report_ref": "r.json", "ok": True}


def _code_artifact(path: str = "/ws/output/lru.py") -> dict:
    return {"artifact_id": "lru.py", "kind": "py", "path": path, "ok": True}


def _doc_artifact(path: str = "/ws/output/report.md") -> dict:
    return {"artifact_id": "report.md", "kind": "md", "path": path, "ok": True}


def _test_run_record(command: str = "python3 -m pytest -q") -> dict:
    return {"tool": "run_command", "ok": True, "parameters": {"tool": "run_command", "command": command}}


# --- the case the regression is about: blocks once ----------------------------


def test_blocks_when_test_required_no_evidence_and_code_artifact(tmp_path):
    req = _request("复刻 LRU Cache，要求真实运行测试确保全过")
    report = _report([_code_artifact()], tmp_path)
    assert verification_evidence_rework(req, report) is True
    assert report["ok"] is False
    assert report["verification_evidence_gate"]["finding"] == "VERIFICATION_REQUIRED_BUT_NO_TEST_RUN_EVIDENCE"
    # a structured rework hint is injected so the (native) model learns why
    assert any("[verification-evidence-rework]" in str(item) for item in req.params.tool_context)
    assert any("真实运行测试" in str(item) for item in req.params.tool_context)


def test_english_run_tests_phrasing_also_triggers(tmp_path):
    req = _request("Clone an LRU cache. Run the tests and make sure all tests pass.")
    report = _report([_code_artifact()], tmp_path)
    assert verification_evidence_rework(req, report) is True


# --- idempotent: never deadlocks ---------------------------------------------


def test_second_pass_allows_after_hint_already_emitted(tmp_path):
    req = _request(
        "要求真实运行测试确保全过",
        tool_context=["[verification-evidence-rework]\n{...}\n请先真实运行测试。"],
    )
    report = _report([_code_artifact()], tmp_path)
    # already nudged once → allow (no deadlock even if the model cannot run tests)
    assert verification_evidence_rework(req, report) is False
    assert report["ok"] is True


# --- not the gate's business: must NOT block ----------------------------------


def test_pure_doc_task_not_blocked_even_if_test_keyword_present(tmp_path):
    # report/doc deliverable (no code artifact) → never triggers, protects normal delivery
    req = _request("写一份测试方案报告，确保覆盖所有用例")
    report = _report([_doc_artifact()], tmp_path)
    assert verification_evidence_rework(req, report) is False


def test_not_blocked_when_tests_were_actually_run(tmp_path):
    req = _request("要求真实运行测试确保全过", archive_tool_calls=[_test_run_record()])
    report = _report([_code_artifact()], tmp_path)
    assert verification_evidence_rework(req, report) is False


def test_not_blocked_when_task_does_not_ask_for_test_run(tmp_path):
    # bare "写个 LRU Cache" with no verification ask → out of scope, allow
    req = _request("写一个 LRU Cache 类放到 output 目录")
    report = _report([_code_artifact()], tmp_path)
    assert verification_evidence_rework(req, report) is False


def test_not_blocked_when_a_test_command_succeeded_among_calls(tmp_path):
    req = _request(
        "复刻 LRU Cache，跑测试确保全部通过",
        archive_tool_calls=[
            {"tool": "write_file", "ok": True, "parameters": {"tool": "write_file", "path": "lru.py"}},
            _test_run_record("python3 -m unittest discover"),
        ],
    )
    report = _report([_code_artifact()], tmp_path)
    assert verification_evidence_rework(req, report) is False


def test_failed_test_command_does_not_count_as_evidence(tmp_path):
    # a run_command that did NOT succeed (ok != True) is not evidence the tests ran green
    failed = {"tool": "run_command", "ok": False, "parameters": {"tool": "run_command", "command": "pytest -q"}}
    req = _request("要求真实运行测试确保全过", archive_tool_calls=[failed])
    report = _report([_code_artifact()], tmp_path)
    assert verification_evidence_rework(req, report) is True


def test_npm_and_go_test_commands_recognized_as_evidence(tmp_path):
    for cmd in ("npm test", "go test ./...", "cargo test", "yarn test"):
        req = _request("run the tests and ensure they all pass", archive_tool_calls=[_test_run_record(cmd)])
        report = _report([_code_artifact("/ws/output/index.js")], tmp_path)
        assert verification_evidence_rework(req, report) is False, cmd


def test_no_artifacts_no_block(tmp_path):
    req = _request("要求真实运行测试确保全过")
    report = _report([], tmp_path)
    assert verification_evidence_rework(req, report) is False
