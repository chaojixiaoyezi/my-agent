"""来源比例观测钉子(REFACTORING_BACKLOG"交付数据量 vs 来源调用量",实锤 R8b
接力轮隐蔽编造:24/24 xlsx 验收通过,但数据是静态列表复用+公式编造)。

钉死契约:
1. 纯观测零判定:报告里只有并排数字(网络成功调用/交付文件数/字节/声明总数),
   无 finding、无 allowed=False——比例是否可疑由把关者结合任务性质判断
   (零网络+多文件是本地分析任务的正常形态,机器判"数据类产物"必误伤)。
2. 检索侧零白名单:按 ToolSpec.category=="web" 结构化判定;失败调用不计。
3. 防御:registry 异常/文件缺失/无声明一律计 0,绝不打断验收。
4. 双路径投影:uncontracted closeout 报告必带 source_volume_observation。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.delivery_closeout.source_volume import (
    source_volume_observation,
    url_authenticity_observation,
    web_category_tool_names,
)

pytestmark = pytest.mark.integration


def _spec(name: str, category: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, category=category)


def test_r8b_shape_numbers_side_by_side() -> None:
    """R8b 形态:交付 24 个文件但网络成功调用极少——数字并排可见,无判定字段。"""
    archive = [
        {"tool": "web_fetch", "call_id": f"1-{i}", "ok": True} for i in range(3)
    ] + [
        {"tool": "web_fetch", "call_id": "9-1", "ok": False},  # 失败不计
        {"tool": "write_file", "call_id": "9-2", "ok": True},  # 非 web 不计
    ]
    obs = source_volume_observation(
        archive,
        [],
        web_tool_names=frozenset({"web_fetch", "web_search"}),
        declared_min_count_total=24,
    )
    assert obs["network_success_calls"] == 3
    assert obs["declared_min_count_total"] == 24
    assert obs["network_calls_scope"] == "web_category_tools"
    assert "allowed" not in obs and "finding" not in obs, "纯观测,绝不携带判定字段"


def test_delivered_volume_counts_real_files(tmp_path: Path) -> None:
    f1 = tmp_path / "WEEK01.xlsx"
    f1.write_bytes(b"PK" + b"x" * 998)
    f2 = tmp_path / "WEEK02.xlsx"
    f2.write_bytes(b"PK" + b"y" * 498)
    obs = source_volume_observation(
        [],
        [str(f1), str(f2), str(tmp_path / "missing.xlsx")],
        web_tool_names=frozenset(),
    )
    assert obs["delivered_files"] == 2
    assert obs["delivered_bytes"] == 1500
    assert obs["network_success_calls"] == 0


def test_web_category_tool_names_from_registry() -> None:
    agent = SimpleNamespace(
        tools=SimpleNamespace(
            specs=lambda: [
                _spec("web_fetch", "web"),
                _spec("web_search", "WEB"),
                _spec("read_file", "filesystem"),
                _spec("run_command", "shell"),
            ]
        )
    )
    names = web_category_tool_names(agent)
    assert names == frozenset({"web_fetch", "web_search"}), "按 category 判定,大小写归一,零白名单"
    assert web_category_tool_names(SimpleNamespace()) == frozenset(), "registry 异常计空不崩"


def test_uncontracted_closeout_report_carries_observation(tmp_path: Path) -> None:
    """端到端:uncontracted closeout 报告必带 source_volume_observation。"""
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
    from agent_py_agent.agent.agent_core.tool_loop.final_exit_contract import (
        FinalExitRequest,
        FinalExitState,
        final_exit_closeout_decision,
    )
    from agent_py_agent.agent.settings.config import AgentConfig

    task_root = tmp_path / "tasks" / "t-vol"
    output = task_root / "output"
    output.mkdir(parents=True)
    report_file = output / "报告.md"
    report_file.write_text("# 报告\n内容", encoding="utf-8")
    agent = SimpleNamespace(
        config=AgentConfig(),
        tools=SimpleNamespace(workspace_root=tmp_path, specs=lambda: [_spec("web_fetch", "web")]),
        root=tmp_path,
    )
    params = ToolLoopExecuteParams(
        user_prompt="测试任务", memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section="", tool_recommendations_section="", tool_context=[],
        effective_on_chunk=None, allowed_tools=None, granted_capabilities=None,
        write_boundary=None,
        task_attributes={"run_workspace": {
            "task_root": str(task_root), "output_dir": str(output), "work_dir": str(task_root / "work")}},
        request_id="req-vol", run_id="run-vol", task_id="run-vol",
        one_shot_tool_calls=set(), executed_tools=[],
        archive_tool_calls=[
            {"tool": "web_fetch", "call_id": "1-1", "ok": True},
            {"tool": "write_file", "call_id": "2-1", "ok": True,
             "parameters": {"path": str(report_file)}, "output": "written"},
        ],
    )

    final_exit_closeout_decision(
        FinalExitRequest(agent, params, SimpleNamespace(text="完成。", backend="echo"), FinalExitState())
    )

    report = json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    obs = report["source_volume_observation"]
    assert obs["network_success_calls"] == 1
    assert obs["delivered_files"] >= 1
    assert obs["delivered_bytes"] > 0


# ---- 子项3: URL 真实性软观测(防编造) ----


def test_url_authenticity_flags_cited_but_not_fetched(tmp_path: Path) -> None:
    """报告引用的 URL 不在本会话实际 fetch 集里 → 软标记为未验证。"""
    report = tmp_path / "report.md"
    report.write_text(
        "参考来源:\n- https://really-fetched.example/a 见正文\n"
        "- https://made-up-official.example/spec 官方文档\n",
        encoding="utf-8",
    )
    archive = [
        {"tool": "web_fetch", "ok": True, "parameters": {"url": "https://really-fetched.example/a"}},
        {"tool": "web_fetch", "ok": False, "parameters": {"url": "https://failed.example/x"}},  # 失败不算验证
    ]
    obs = url_authenticity_observation(
        archive, [str(report)], web_tool_names=frozenset({"web_fetch", "web_search"})
    )
    assert obs["verified_fetch_url_count"] == 1
    assert obs["cited_url_count"] == 2
    assert obs["unverified_url_count"] == 1
    assert obs["unverified_urls"] == ["https://made-up-official.example/spec"]
    assert "allowed" not in obs and "finding" not in obs, "软标记非硬拦,不携带判定字段"


def test_url_authenticity_normalizes_trailing_slash_and_fragment(tmp_path: Path) -> None:
    """fetch 'a/' vs 报告写 'a' 不算不一致;fragment 差异也归一,避免伪标记。"""
    report = tmp_path / "r.md"
    report.write_text("见 https://x.example/doc#section 和 https://x.example/path", encoding="utf-8")
    archive = [
        {"tool": "web_fetch", "ok": True, "parameters": {"url": "https://x.example/doc"}},
        {"tool": "web_fetch", "ok": True, "parameters": {"urls": ["https://x.example/path/"]}},
    ]
    obs = url_authenticity_observation(archive, [str(report)], web_tool_names=frozenset({"web_fetch"}))
    assert obs["unverified_url_count"] == 0, f"归一后不应误标: {obs['unverified_urls']}"


def test_url_authenticity_empty_when_no_urls_cited(tmp_path: Path) -> None:
    """产物不引用任何 URL → 零未验证,note 走正向文案。"""
    report = tmp_path / "r.md"
    report.write_text("# 本地分析报告\n纯本地任务,无外部引用。", encoding="utf-8")
    obs = url_authenticity_observation([], [str(report)], web_tool_names=frozenset({"web_fetch"}))
    assert obs["cited_url_count"] == 0
    assert obs["unverified_url_count"] == 0


def test_url_authenticity_extract_mode_urls_count_as_verified(tmp_path: Path) -> None:
    """extract 模式用 urls 数组 fetch,这些 URL 也算本会话验证过。"""
    report = tmp_path / "r.md"
    report.write_text("来源 https://a.example/1 https://b.example/2", encoding="utf-8")
    archive = [
        {"tool": "web_fetch", "ok": True,
         "parameters": {"urls": ["https://a.example/1", "https://b.example/2"], "mode": "extract"}},
    ]
    obs = url_authenticity_observation(archive, [str(report)], web_tool_names=frozenset({"web_fetch"}))
    assert obs["verified_fetch_url_count"] == 2
    assert obs["unverified_url_count"] == 0


def test_url_authenticity_carried_in_closeout_report(tmp_path: Path) -> None:
    """端到端:closeout 报告必带 url_authenticity_observation。"""
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
    from agent_py_agent.agent.agent_core.tool_loop.final_exit_contract import (
        FinalExitRequest,
        FinalExitState,
        final_exit_closeout_decision,
    )
    from agent_py_agent.agent.settings.config import AgentConfig

    task_root = tmp_path / "tasks" / "t-url"
    output = task_root / "output"
    output.mkdir(parents=True)
    report_file = output / "报告.md"
    report_file.write_text("参考 https://unfetched.example/x 官方说明。", encoding="utf-8")
    agent = SimpleNamespace(
        config=AgentConfig(),
        tools=SimpleNamespace(workspace_root=tmp_path, specs=lambda: [_spec("web_fetch", "web")]),
        root=tmp_path,
    )
    params = ToolLoopExecuteParams(
        user_prompt="测试", memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section="", tool_recommendations_section="", tool_context=[],
        effective_on_chunk=None, allowed_tools=None, granted_capabilities=None,
        write_boundary=None,
        task_attributes={"run_workspace": {
            "task_root": str(task_root), "output_dir": str(output), "work_dir": str(task_root / "work")}},
        request_id="req-url", run_id="run-url", task_id="run-url",
        one_shot_tool_calls=set(), executed_tools=[],
        archive_tool_calls=[
            {"tool": "write_file", "call_id": "1-1", "ok": True,
             "parameters": {"path": str(report_file)}, "output": "written"},
        ],
    )
    final_exit_closeout_decision(
        FinalExitRequest(agent, params, SimpleNamespace(text="完成。", backend="echo"), FinalExitState())
    )
    report = json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    url_obs = report["url_authenticity_observation"]
    assert url_obs["cited_url_count"] == 1
    assert url_obs["unverified_url_count"] == 1, "报告引用了从未 fetch 的 URL,应软标记"
