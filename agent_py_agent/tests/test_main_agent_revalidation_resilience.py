from __future__ import annotations

from pathlib import Path


# LLM: Corrupt stored reports must become structured failed reports, not raw JSONDecodeError crashes.
# 函数用途: 覆盖普通任务和真实任务 revalidate 的坏 execution_report.json 结构化失败路径。
def test_main_agent_revalidation_handles_corrupt_report_json(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        revalidate_main_agent_real_task_execution,
    )
    from agent_py_agent.agent.contracts.main_agent_task_execution import (
        revalidate_main_agent_task_execution,
    )

    task_report = _write_corrupt_report(tmp_path, "main_agent_task_execution")
    real_report = _write_corrupt_report(tmp_path, "main_agent_real_task_execution")

    task_payload = revalidate_main_agent_task_execution(task_report, workspace=tmp_path).to_dict()
    real_payload = revalidate_main_agent_real_task_execution(real_report, workspace=tmp_path).to_dict()

    _assert_invalid_report_payload(task_payload)
    _assert_invalid_report_payload(real_payload)
    assert task_report.read_text(encoding="utf-8").lstrip().startswith("{")
    assert real_report.read_text(encoding="utf-8").lstrip().startswith("{")


def _write_corrupt_report(root: Path, dirname: str) -> Path:
    report_path = root / dirname / "execution_report.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("{bad json", encoding="utf-8")
    return report_path


def _assert_invalid_report_payload(payload: dict[str, object]) -> None:
    cases = payload["cases"]
    assert payload["ok"] is False
    assert payload["summary"]["failed"] == 1
    assert isinstance(cases, list)
    assert cases[0]["case_id"] == "__report__"
    assert cases[0]["issues"][0] == "REVALIDATION_REPORT_INVALID_JSON"
