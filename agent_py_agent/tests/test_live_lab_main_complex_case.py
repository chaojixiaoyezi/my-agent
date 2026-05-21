from __future__ import annotations

# LLM: Main-complex Live Lab tests stay separate from natural subagent canaries to keep both files small.
# 模块用途: 验证主代理复杂任务测试入口、隔离配置和真实产物 gate。
import json
from pathlib import Path
from types import SimpleNamespace

from scripts.live_lab.constants import REAL_CASES, SUITES
from scripts.live_lab.main_agent_artifact_case import (
    _assert_artifact_readback_report,
    _assert_compact_resume_roundtrip_payload,
    _main_artifact_readback_prompt,
)
from scripts.live_lab.main_agent_complex_case import (
    _assert_large_log_report,
    _assert_main_web_app_output,
    _assert_tool_recovery_report,
    _ensure_main_agent_only,
    _main_direct_web_app_prompt,
    _main_large_log_prompt,
    _main_tool_failure_prompt,
    case_main_direct_web_app,
)
from scripts.live_lab.session import LabSessionManager


# LLM: The main-complex suite should test the root agent with ordinary wording and no delegation jargon.
# 函数用途: 确认主代理复杂任务 suite 已注册，提示词像普通用户表达，并显式关闭小傻妞链路。
def test_main_complex_case_is_registered_as_real_opt_in_suite():
    prompts = [
        _main_direct_web_app_prompt(),
        _main_tool_failure_prompt(),
        _main_large_log_prompt(),
        _main_artifact_readback_prompt(),
    ]

    assert SUITES["main-complex"] == [
        "health",
        "main_direct_web_app",
        "main_tool_failure_recovery",
        "main_artifact_readback",
        "main_compact_resume_roundtrip",
        "main_large_log_audit",
    ]
    assert SUITES["main-artifact"] == ["health", "main_artifact_readback", "main_compact_resume_roundtrip"]
    assert {
        "main_direct_web_app",
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


# LLM: Main-complex cases append isolation overrides without mutating the source config.
# 函数用途: 确认主代理复杂测试会在临时配置里关闭小傻妞，并保持工具轮数不限制。
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


# LLM: The Web app case must put static_site_check inside the agent repair loop, not only after-run assertions.
# 函数用途: 验证主代理 Web 真实测试通过结构化 delivery_contract_file 传入通用网页验收合同。
def test_main_web_app_case_passes_static_site_contract_to_run(tmp_path):
    lab = _FakeMainWebAppLab(tmp_path)

    case_main_direct_web_app(lab)

    command = lab.commands[0]
    assert "--delivery-contract-file" in command
    contract_path = Path(command[command.index("--delivery-contract-file") + 1])
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    artifact = contract["artifacts"][0]
    assert artifact["preferred_path"] == "lab_outputs/main-web-app"
    assert artifact["validation_contract"]["validator"] == "static_site_check"
    assert artifact["validation_contract"]["required_files"] == ["index.html", "styles.css", "app.js", "README.md"]
    assert artifact["validation_contract"]["strict_dom_bindings"] is True


# LLM: Main-complex artifact gates should inspect concrete root-agent outputs.
# 函数用途: 确认主代理复杂测试的 Web、恢复报告和大日志报告验收都不相信口头回复。
def test_main_complex_artifact_gates_accept_complete_outputs(tmp_path):
    _write_complete_web_app(tmp_path)
    _write_tool_recovery_report(tmp_path)
    _write_artifact_readback_report(tmp_path)
    _assert_compact_resume_roundtrip_payload(_compact_apply_payload(), _compact_resume_payload())
    _write_large_log_report(tmp_path)


# LLM: _FakeMainWebAppLab records the command while creating a valid output fixture for the after-run gate.
# 类用途: 避免单测调用真实模型，只观察 Live Lab case 是否把结构化合同传给 CLI。
class _FakeMainWebAppLab:
    def __init__(self, root: Path) -> None:
        self.fixture_root = root
        self.responses_dir = root / "responses"
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = root / "agent_config.yaml"
        self.config_path.write_text("model_backend: echo\nenable_subagents: true\n", encoding="utf-8")
        self.args = SimpleNamespace(timeout=180)
        self.commands: list[list[str]] = []

    def section(self, _title: str) -> None:
        return None

    def record_prompt(self, _case_name: str, _prompt: str) -> Path:
        return self.fixture_root / "prompt.txt"

    def agent_command(self, *parts: str) -> list[str]:
        return ["agent_py_agent", *parts]

    def run_command(self, command: list[str], *, timeout: int) -> SimpleNamespace:
        self.commands.append(command)
        _write_complete_web_app(self.fixture_root)
        return SimpleNamespace(stdout="", stderr="", returncode=0, elapsed_seconds=0.0)

    def log(self, _message: str = "") -> None:
        return None


# LLM: _write_complete_web_app builds a compact valid multi-file site fixture.
# 函数用途: 写出测试用 Web app 示例，并立即走主代理产物验收 gate。
def _write_complete_web_app(tmp_path) -> None:
    web_root = tmp_path / "lab_outputs" / "main-web-app"
    web_root.mkdir(parents=True)
    (web_root / "index.html").write_text(
        "<!doctype html><html><head><link rel=\"stylesheet\" href=\"styles.css\"></head><body>"
        "<nav><a href=\"#showroom\">家具</a></nav><section id=\"showroom\">Furniture</section>"
        "<script src=\"app.js\"></script></body></html>",
        encoding="utf-8",
    )
    (web_root / "styles.css").write_text("body { color: #111; }\n", encoding="utf-8")
    (web_root / "app.js").write_text("document.addEventListener('click', function () {});\n", encoding="utf-8")
    (web_root / "README.md").write_text(
        "文件包含 index.html、styles.css 和 app.js。\n"
        "页面锚点包含 `#showroom`。\n",
        encoding="utf-8",
    )
    _assert_main_web_app_output(web_root)


# LLM: _write_tool_recovery_report builds a readable recovery report fixture.
# 函数用途: 写出测试用工具失败恢复报告，并立即走主代理恢复 gate。
def _write_tool_recovery_report(tmp_path) -> None:
    recovery = tmp_path / "lab_outputs" / "tool-recovery" / "report.md"
    recovery.parent.mkdir(parents=True)
    recovery.write_text(
        "先读取 notes/does-not-exist.md 失败。随后改读 notes/small_task.md 和 README.md，"
        "确认任务素材存在，并给出下一步建议。" * 2,
        encoding="utf-8",
    )
    _assert_tool_recovery_report(recovery)


# LLM: _write_artifact_readback_report builds a report that proves far-apart artifact sections were recovered.
# 函数用途: 写出测试用 artifact 读回报告，并立即走主代理 artifact 续接 gate。
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


# LLM: _write_large_log_report builds a realistic enough audit report fixture.
# 函数用途: 写出测试用大日志审计报告，并立即走关键证据 gate。
def _write_large_log_report(tmp_path) -> None:
    log_report = tmp_path / "lab_outputs" / "large-log-audit" / "report.md"
    log_report.parent.mkdir(parents=True)
    log_report.write_text(
        "\n".join(
            [
                "# 大日志审计",
                "",
                "发现 PAYMENT_TIMEOUT，trace-9f42 指向付款链路在支付提供商侧等待超过阈值。",
                "还发现 CART_STUCK，说明购物车更新连续重试，可能导致用户结算前看到旧价格或旧数量。",
                "影响：付款超时会让订单确认变慢，购物车异常会让用户重复点击或放弃购买。",
                "建议：按 trace id 查上游依赖、队列延迟、支付提供商响应时间和购物车写入锁。",
                "下一步：先抽样 trace-9f42 的完整请求链，再对 CART_STUCK 的重试窗口做时间线复盘。",
            ]
        ),
        encoding="utf-8",
    )
    _assert_large_log_report(log_report)


# LLM: _compact_apply_payload builds the minimum structured apply facts expected after manual fact completion.
# 函数用途: 构造 compact apply 测试 payload，验证 roundtrip gate 只读结构化字段。
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


# LLM: _compact_resume_payload builds the minimum auto-guard handoff expected from memory-resume.
# 函数用途: 构造 compact resume 测试 payload，确认 auto guard 放行和推荐路径存在。
def _compact_resume_payload() -> dict:
    return {
        "ok": True,
        "recommended_read_paths": ["/tmp/compact_context.md"],
        "action_guard": {"status": "allow_automated_continue", "allowed_to_continue": True},
        "handoff": {"missing_fields": []},
    }
