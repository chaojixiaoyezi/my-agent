from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts.live_lab.constants import REAL_CASES, SUITES
from scripts.live_lab.main_agent_artifact_case import (
    _assert_artifact_readback_report,
    _assert_compact_resume_roundtrip_payload,
    _latest_artifact_readback_fact_id,
    _main_artifact_readback_prompt,
)
from scripts.live_lab.main_agent_complex_case import (
    _assert_large_log_report,
    _assert_tool_recovery_report,
    _ensure_main_agent_only,
    _main_large_log_prompt,
    _main_tool_failure_prompt,
)
from scripts.live_lab.session import LabSessionManager


def test_main_complex_case_is_registered_as_real_opt_in_suite():
    prompts = [
        _main_tool_failure_prompt(),
        _main_large_log_prompt(),
        _main_artifact_readback_prompt(),
    ]

    assert SUITES["main-complex"] == [
        "health",
        "main_tool_failure_recovery",
        "main_artifact_readback",
        "main_compact_resume_roundtrip",
        "main_large_log_audit",
    ]
    assert SUITES["main-artifact"] == ["health", "main_artifact_readback", "main_compact_resume_roundtrip"]
    assert {
        "main_tool_failure_recovery",
        "main_artifact_readback",
        "main_compact_resume_roundtrip",
        "main_large_log_audit",
    } <= REAL_CASES
    for prompt in prompts:
        assert "这次你自己完成，不要派小傻妞" in prompt
        assert "dispatch" not in prompt.lower()
        assert "runner" not in prompt.lower()
        assert "contract" not in prompt.lower()


def test_main_complex_config_disables_subagents_in_isolated_config(tmp_path):
    source = tmp_path / "agent_config.yaml"
    source.write_text("model_backend: echo\nenable_subagents: true\n", encoding="utf-8")
    args = SimpleNamespace(
        config=str(source),
        runs_dir=str(tmp_path / "runs"),
        run_id="main-complex-config",
        real_llm=False,
        count=1,
        timeout=180,
    )
    session = LabSessionManager(args)
    session.setup()
    lab = SimpleNamespace(config_path=session.config_path, args=args)

    _ensure_main_agent_only(lab)

    text = session.config_path.read_text(encoding="utf-8")
    assert "# main-agent-complex overrides" in text
    assert "enable_subagents: false" in text
    assert f'my_agent_home: "{session.fixture_root / ".my_agent" / "home"}"' in text
    assert source.read_text(encoding="utf-8") == "model_backend: echo\nenable_subagents: true\n"


def test_main_complex_artifact_gates_accept_complete_outputs(tmp_path):
    _write_tool_recovery_report(tmp_path)
    _write_artifact_readback_report(tmp_path)
    _assert_compact_resume_roundtrip_payload(_compact_apply_payload(), _compact_resume_payload())
    _write_large_log_report(tmp_path)


def test_main_artifact_runtime_fact_lookup_uses_owner_home(tmp_path):
    fact = (
        tmp_path
        / ".my_agent"
        / "home"
        / "owners"
        / "local"
        / "main"
        / "memory_archive"
        / "runtime_facts"
        / "run-current"
        / "task.json"
    )
    fact.parent.mkdir(parents=True)
    fact.write_text('{"goal": "TRACE-ARTIFACT-991"}', encoding="utf-8")

    assert _latest_artifact_readback_fact_id(tmp_path) == "run-current"


def _write_tool_recovery_report(tmp_path) -> None:
    recovery = tmp_path / "lab_outputs" / "tool-recovery" / "report.md"
    recovery.parent.mkdir(parents=True)
    recovery.write_text(
        "先读取 notes/does-not-exist.md 失败。随后改读 notes/small_task.md 和 README.md，"
        "确认任务素材存在，并给出下一步建议。" * 2,
        encoding="utf-8",
    )
    _assert_tool_recovery_report(recovery)


def _write_artifact_readback_report(tmp_path) -> None:
    report = tmp_path / "lab_outputs" / "artifact-readback" / "report.md"
    report.parent.mkdir(parents=True)
    report.write_text(
        "报告证明读取了 ALPHA-ANCHOR、OMEGA-ANCHOR 和 TRACE-ARTIFACT-991。\n"
        "ALPHA-ANCHOR 说明北区门店库存偏低，风险是新品展示不足。\n"
        "OMEGA-ANCHOR 说明预约系统周末排队延迟，风险是客户到店体验下降。\n"
        "TRACE-ARTIFACT-991 说明售后回访里有面料色差反馈，风险是同批次质量问题扩大。\n"
        "下一步建议分别核对库存、排查预约峰值、抽检对应批次。",
        encoding="utf-8",
    )
    _assert_artifact_readback_report(report)


def _write_large_log_report(tmp_path) -> None:
    log_report = tmp_path / "lab_outputs" / "large-log-audit" / "report.md"
    log_report.parent.mkdir(parents=True)
    log_report.write_text(
        "\n".join(
            [
                "# 大日志审计",
                "",
                "发现 PAYMENT_TIMEOUT，trace-9f42 指向付款链路在支付提供商侧等待超过阈值。",
                "还发现 CART_STUCK，说明流程状态更新连续重试，可能导致用户结算前看到旧价格或旧数量。",
                "影响：付款超时会让流程确认变慢，流程状态异常会让用户重复点击或放弃购买。",
                "建议：按 trace id 查上游依赖、队列延迟、支付提供商响应时间和流程状态写入锁。",
                "下一步：先抽样 trace-9f42 的完整请求链，再对 CART_STUCK 的重试窗口做时间线复盘。",
            ]
        ),
        encoding="utf-8",
    )
    _assert_large_log_report(log_report)


def _compact_apply_payload() -> dict:
    source = "/tmp/runtime_facts/task.json"
    return {
        "ok": True,
        "post_compact_self_check": {"ok": True},
        "work_state_snapshot": {
            "missing_fields": [],
            "artifact_refs": [{"path": "/tmp/tool-output.json", "tool": "read_file"}],
            "acceptance": {"source_status": "recorded", "items": ["报告包含三处证据"], "source_paths": [source]},
            "constraints": {"source_status": "recorded", "items": ["不得猜测"], "source_paths": [source]},
            "latest_tests": {"status": "recorded", "items": ["Live Lab passed"], "source_paths": [source]},
        },
    }


def _compact_resume_payload() -> dict:
    return {
        "ok": True,
        "recommended_read_paths": ["/tmp/compact_context.md"],
        "action_guard": {"status": "allow_automated_continue", "allowed_to_continue": True},
        "handoff": {"missing_fields": []},
    }
