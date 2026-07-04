"""需求枚举项自动派生 coverage 清单(A2 回炉)+ 打回载荷动手痕迹(A3 扩面)防回归。

真机实锤:A2 的打回挂在"模型自觉声明 coverage.targets"上,大体量建站任务两个用户
计数全 0、打回从没 fire。治本=清单从需求原文的枚举字面记号自动派生,不靠自觉;
打回载荷附 writes/commands 计数,代码/数据类"没真动手"有结构化事实可看。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.delivery_closeout.task_progress_gate import (
    _coverage_incomplete_findings,
    coverage_incomplete_rework,
    evaluate_task_progress_closeout_gate,
)
from agent_py_agent.agent.agent_core.requirement_coverage_seed import (
    requirement_enumeration_items,
    run_params_with_requirement_coverage_seed,
)
from agent_py_agent.agent.task_progress import read_task_progress, write_task_progress


@dataclass(frozen=True)
class _Params:
    run_id: str = "run-req"
    task_id: str = "run-req"
    source: str = "cli_run"
    context_scope: str = "default"
    root_user_prompt: str = ""
    inject: list | None = None


def _agent(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(tmp_path)), root=str(tmp_path))


BUILD_PROMPT = """给我建一个完整的个人网站,功能要齐全:
- 首页展示与自我介绍
- 博客列表和文章详情页
* 相册画廊(支持分类)
1. 联系表单(校验邮箱)
2) 深色模式切换
一、访客统计面板
① RSS 订阅
（3）站内搜索
```yaml
- 这行在代码围栏里: 不是需求项
```
- [ ] 移动端适配
- [x] SEO 基础标签
3.14 这行是小数不是序号
正文说明行不算列表。
- 首页展示与自我介绍
"""


def test_enumeration_parser_literal_markers_only():
    items = requirement_enumeration_items(BUILD_PROMPT)
    assert "首页展示与自我介绍" in items
    assert "博客列表和文章详情页" in items
    assert "相册画廊(支持分类)" in items
    assert "联系表单(校验邮箱)" in items
    assert "深色模式切换" in items
    assert "访客统计面板" in items
    assert "RSS 订阅" in items
    assert "站内搜索" in items
    assert "移动端适配" in items  # checkbox 前缀剥掉
    assert "SEO 基础标签" in items
    assert all("代码围栏" not in item for item in items)  # 围栏内不算
    assert all(not item.startswith("14") for item in items)  # 小数行不算
    assert items.count("首页展示与自我介绍") == 1  # 去重


def test_seed_writes_ledger_and_injects_note(tmp_path):
    params = _Params(root_user_prompt=BUILD_PROMPT)
    updated = run_params_with_requirement_coverage_seed(_agent(tmp_path), BUILD_PROMPT, params)
    assert updated.inject and "[requirement-coverage-seed]" in updated.inject[-1]
    progress = read_task_progress(tmp_path, "run-req")
    targets = progress["coverage"]["targets"]
    assert len(targets) >= 8
    assert all(target["id"].startswith("req-") for target in targets)
    assert all(target["status"] == "pending" for target in targets)
    assert progress["coverage"]["counts"]["targets_incomplete"] == len(targets)
    # 幂等:同账已有清单,第二次不重复立、不再注入。
    again = run_params_with_requirement_coverage_seed(_agent(tmp_path), BUILD_PROMPT, params)
    assert again is params


def test_seed_skips_short_lists_internal_scope_and_declared_coverage(tmp_path):
    few = "只有两条:\n- 甲\n- 乙\n"
    assert run_params_with_requirement_coverage_seed(_agent(tmp_path), few, _Params(root_user_prompt=few)) is not None
    assert "coverage" not in read_task_progress(tmp_path, "run-req")
    internal = _Params(root_user_prompt=BUILD_PROMPT, context_scope="task_local")
    assert (
        run_params_with_requirement_coverage_seed(_agent(tmp_path), BUILD_PROMPT, internal) is internal
    )
    # 模型已自立清单 → 种子不插手。
    write_task_progress(tmp_path, "run-own", {"coverage": {"targets": [{"id": "mine", "title": "自立项"}]}})
    own = _Params(run_id="run-own", root_user_prompt=BUILD_PROMPT)
    assert run_params_with_requirement_coverage_seed(_agent(tmp_path), BUILD_PROMPT, own) is own
    targets = read_task_progress(tmp_path, "run-own")["coverage"]["targets"]
    assert [target["id"] for target in targets] == ["mine"]


def test_background_wake_seeds_task_ledger(tmp_path):
    params = _Params(run_id="bg-main-1", task_id="task-main", source="background_main_agent", root_user_prompt=BUILD_PROMPT)
    run_params_with_requirement_coverage_seed(_agent(tmp_path), BUILD_PROMPT, params)
    assert read_task_progress(tmp_path, "task-main")["coverage"]["targets"]
    assert "coverage" not in read_task_progress(tmp_path, "bg-main-1")


def test_seeded_coverage_feeds_rework_with_action_trace(tmp_path):
    # 种出的清单 → 收口 coverage finding → 幂等打回一次,载荷带动手痕迹计数。
    run_params_with_requirement_coverage_seed(
        _agent(tmp_path), BUILD_PROMPT, _Params(root_user_prompt=BUILD_PROMPT)
    )
    progress = read_task_progress(tmp_path, "run-req")
    findings = _coverage_incomplete_findings(progress)
    assert findings and findings[0].evidence["targets_incomplete"] >= 8
    report = {
        "task_progress_closeout_gate": {
            "allowed": True,
            "findings": [
                {"code": findings[0].code, "severity": "soft", "evidence": dict(findings[0].evidence)}
            ],
        }
    }
    gate_params = SimpleNamespace(tool_context=[], executed_tools=["run_command", "web_fetch", "write_file"])
    assert coverage_incomplete_rework(gate_params, report) is True
    payload = json.loads(gate_params.tool_context[-1].split("\n", 1)[1])
    assert payload["action_trace"] == {"writes": 1, "commands": 1}
    assert payload["targets_incomplete"] >= 8
    # 二次同形态放行(幂等,不死锁)。
    assert coverage_incomplete_rework(gate_params, report) is False


def test_projection_exit_path_still_carries_coverage_advisory(tmp_path):
    # 真机实锤缺口:走 artifact-evidence projection 修补路收口时,coverage 清单 4 项全
    # open 却没人问(advisory 只挂 closed 路)——两条"进度已收"退出路都必须带 coverage 账。
    write_task_progress(
        tmp_path,
        "run-proj",
        {
            "items": [
                {
                    "id": "i1",
                    "title": "读了三份来源",
                    "status": "done",
                    "evidence": ["src/alpha_one.py", "src/beta_two.py", "src/gamma_three.py"],
                }
            ],
            "coverage": {"targets": [{"id": "req-01", "title": "甲", "status": "pending"}]},
        },
    )
    artifact = tmp_path / "report.md"
    artifact.write_text("总结:一个来源都没引用", encoding="utf-8")
    closeout = SimpleNamespace(
        agent=SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=str(tmp_path)), root=str(tmp_path)),
        params=SimpleNamespace(run_id="run-proj", task_id="run-proj", source="cli_run"),
    )
    decision = evaluate_task_progress_closeout_gate(
        closeout, {"artifacts": [{"ok": True, "path": str(artifact)}]}
    )
    codes = {finding.code for finding in decision.findings}
    assert "TASK_PROGRESS_EVIDENCE_NOT_IN_ARTIFACT" in codes  # 走的确实是 projection 路
    assert "TASK_PROGRESS_COVERAGE_INCOMPLETE" in codes  # coverage 账跟着上


@dataclass(frozen=True)
class _ParamsWithInject(_Params):
    inject: list = field(default_factory=lambda: ["已有注入"])


def test_seed_appends_to_existing_injections(tmp_path):
    params = _ParamsWithInject(run_id="run-inj", task_id="run-inj", root_user_prompt=BUILD_PROMPT)
    updated = run_params_with_requirement_coverage_seed(_agent(tmp_path), BUILD_PROMPT, params)
    assert updated.inject[0] == "已有注入"
    assert "[requirement-coverage-seed]" in updated.inject[1]
