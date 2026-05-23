from __future__ import annotations

from pathlib import Path


def test_production_main_agent_has_no_case_runtime_contracts() -> None:
    root = Path(__file__).resolve().parents[1]
    forbidden_tokens = (
        "MainAgentTaskCase",
        "MainAgentTaskSuite",
        "MainAgentRealTaskCase",
        "MainAgentRealTaskSuite",
        "suite_cases",
        "real_task_suite",
        "run_real_tasks",
        "plan_main_agent_task_suite",
        "run_main_agent_task_execution",
    )

    assert _case_runtime_token_hits(root, forbidden_tokens) == []


def _case_runtime_token_hits(root: Path, forbidden_tokens: tuple[str, ...]) -> list[str]:
    return [
        f"{path.relative_to(root)}:{token}"
        for path in _production_python_files(root)
        for token in forbidden_tokens
        if token in path.read_text(encoding="utf-8")
    ]


def _production_python_files(root: Path) -> list[Path]:
    return [
        path
        for production_root in (root / "agent", root / "cli")
        for path in production_root.rglob("*.py")
    ]
