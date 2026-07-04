from __future__ import annotations

from types import SimpleNamespace

# A2/A3(底座提升)防回归:治"双用户方差"——同任务一个覆盖 5/5、一个只覆盖 1/5 就自认
# 完成。模型自声明的 coverage 范围没对完账 → 幂等打回一次(双出口:继续覆盖或改声明);
# 覆盖闭环计数进出口进展签名(M→M+1=有进展→续航双闸放行再续)。零声明零影响。
from agent_py_agent.agent.agent_core.delivery_closeout.task_progress_gate import (
    coverage_incomplete_rework,
)
from agent_py_agent.agent.agent_core.tool_loop.final_exit_contract import (
    _progress_coverage_signature,
)
from agent_py_agent.agent.task_progress import write_task_progress


def _report_with_coverage_finding(targets_incomplete: int, checks_incomplete: int = 0) -> dict:
    return {
        "task_progress_closeout_gate": {
            "allowed": True,
            "findings": [
                {
                    "code": "TASK_PROGRESS_COVERAGE_INCOMPLETE",
                    "severity": "soft",
                    "evidence": {
                        "targets_incomplete": targets_incomplete,
                        "checks_incomplete": checks_incomplete,
                        "active_targets": [{"id": "p-2", "checks_open": ["分析"]}],
                    },
                }
            ],
        }
    }


def test_coverage_incomplete_reworks_once_then_allows():
    params = SimpleNamespace(tool_context=[])
    report = _report_with_coverage_finding(4)
    assert coverage_incomplete_rework(params, report) is True
    joined = "\n".join(str(item) for item in params.tool_context)
    assert "[coverage-incomplete-rework]" in joined
    assert "targets_incomplete" in joined
    # 幂等:同形态第二次放行(绝不死锁)。
    assert coverage_incomplete_rework(params, report) is False


def test_coverage_complete_or_undeclared_never_blocks():
    params = SimpleNamespace(tool_context=[])
    assert coverage_incomplete_rework(params, {}) is False
    assert coverage_incomplete_rework(params, _report_with_coverage_finding(0, 0)) is False
    assert params.tool_context == []


def test_coverage_rework_requires_tool_context():
    # 没有 tool_context(无处注入指令)不打回,不留隐形状态。
    assert coverage_incomplete_rework(SimpleNamespace(), _report_with_coverage_finding(3)) is False


def test_progress_coverage_signature_tracks_closure(tmp_path):
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(tmp_path)), root=str(tmp_path))
    params = SimpleNamespace(run_id="run-cov", task_id="run-cov", source="cli_run")
    assert _progress_coverage_signature(agent, params) == (-1, -1)
    write_task_progress(
        tmp_path,
        "run-cov",
        {
            "items": [{"id": "i", "title": "t", "status": "in_progress"}],
            "coverage": {
                "goal": "5 个项目全分析",
                "targets": [
                    {"id": "p1", "status": "done"},
                    {"id": "p2", "status": "pending"},
                ],
            },
        },
    )
    assert _progress_coverage_signature(agent, params) == (1, 0)
    write_task_progress(tmp_path, "run-cov", {"coverage": {"targets": [{"id": "p2", "status": "done"}]}})
    # 又闭环一个 → 签名变化(续航双闸据此识别"有真进展")。
    assert _progress_coverage_signature(agent, params) == (2, 0)
