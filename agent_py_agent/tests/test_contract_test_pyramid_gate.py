from __future__ import annotations

from pathlib import Path


def test_contract_test_pyramid_gate_passes_current_repo() -> None:
    from scripts.check_contract_test_pyramid import check_contract_test_pyramid

    repo_root = Path(__file__).resolve().parents[2]

    report = check_contract_test_pyramid(repo_root)

    assert report.ok is True
    assert report.error_codes == ()
    assert "openclaw-main" in report.reference_projects_checked
    assert "hermes-agent-main" in report.reference_projects_checked
    assert "codex-main" in report.reference_projects_checked


def test_contract_test_pyramid_gate_rejects_production_task_specific_needles(tmp_path: Path) -> None:
    from scripts.check_contract_test_pyramid import check_contract_test_pyramid

    agent_root = tmp_path / "agent_py_agent" / "agent"
    agent_root.mkdir(parents=True)
    (agent_root / "bad_contract.py").write_text('TASK = "DeepSeek 论文翻译 PDF 专项合同"\n', encoding="utf-8")
    for rel in (
        "agent_py_agent/tests/contracts",
        "agent_py_agent/tests/fake_tools",
        "agent_py_agent/tests/fake_llm",
        "agent_py_agent/tests/replay",
        "agent_py_agent/tests/scenario_packs",
    ):
        path = tmp_path / rel
        path.mkdir(parents=True)
        (path / "case.json").write_text("{}", encoding="utf-8")
    docs = tmp_path / "docs" / "design"
    docs.mkdir(parents=True)
    (docs / "main-agent-contract-testing.md").write_text(
        "\n".join(
            (
                "AgentScope Claude Code Claw Code Codex Free Code Hermes LangChain LangGraph",
                "OpenAI Agents SDK OpenClaude OpenClaw OpenHuman",
                "my-agent-architecture-review my-agent-feature-card-message-runtime",
            )
        ),
        encoding="utf-8",
    )

    report = check_contract_test_pyramid(tmp_path)

    assert report.ok is False
    assert "PRODUCTION_TASK_SPECIFIC_CONTRACT" in report.error_codes
