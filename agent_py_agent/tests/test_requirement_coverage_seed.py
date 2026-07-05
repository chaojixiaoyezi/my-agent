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


# —— 内联枚举通道(§9.2 回炉):真实需求大量是"一句话里顿号并列",行首通道认不出 ——
INLINE_SITE_PROMPT = (
    "帮我从零做一个功能完整、能直接跑的【个人知识库/笔记 Web 应用】(前后端一套)。"
    "功能要全:用户注册登录、笔记增删改查、富文本编辑、Markdown 预览、标签与文件夹分类、"
    "全文搜索、笔记分享链接、回收站、导入导出、深色模式、后台数据统计面板。"
    "要多文件多模块、代码写完整。"
)
INLINE_PROJECTS_PROMPT = (
    "我家目录的 projects/ 里有 5 个真实开源项目(agentscope-main、claude-code-main、"
    "claw-code-main、langgraph-main、openai-agents-python-main)。帮我逐个深入分析,"
    "最后每个项目一份报告,放到 output/。"
)


def test_inline_dunhao_chain_site_features_all_extracted():
    # 真机失败用例③:11 个功能顿号并列在一句话里,行首通道 0 匹配 → coverage 从没触发。
    items = requirement_enumeration_items(INLINE_SITE_PROMPT)
    assert len(items) >= 10
    for feature in (
        "用户注册登录", "笔记增删改查", "富文本编辑", "Markdown 预览", "标签与文件夹分类",
        "全文搜索", "笔记分享链接", "回收站", "导入导出", "深色模式", "后台数据统计面板",
    ):
        assert feature in items
    # 首项剥掉"功能要全:"前缀、末项剥掉句末标点——项内不残留边界标点。
    assert all("。" not in item and ":" not in item and "," not in item for item in items)


def test_inline_dunhao_chain_ascii_project_names_extracted():
    # 真机失败用例④:5 个项目名顿号并列(括号内)→ 两用户都只做 1/5 就收工。
    # openai-agents-python-main 有 25 个半角字符:"短项 ≤24 字"必须按东亚宽度口径,别按字符数误杀。
    items = requirement_enumeration_items(INLINE_PROJECTS_PROMPT)
    for name in (
        "agentscope-main", "claude-code-main", "claw-code-main",
        "langgraph-main", "openai-agents-python-main",
    ):
        assert name in items


def test_inline_never_splits_commas_or_plain_prose():
    # ⚠️ 最大的坑:逗号在散文里无处不在,绝不当分隔符;纯散文无顿号不发明需求。
    assert requirement_enumeration_items("帮我建个功能齐全的个人网站,要好看,能跑,越快越好。") == []
    assert requirement_enumeration_items("Make an app, keep it fast, add tests, thanks.") == []
    assert requirement_enumeration_items("苹果、香蕉都行,随便买点。") == []  # 两项顿号并列是散文不是清单
    # 逗号只作边界:顿号串被逗号截开后各段不足 3 项就不算枚举。
    assert requirement_enumeration_items("要 A、B,还要 C、D。") == []


def test_inline_respects_fence_width_cap_and_line_channel_dedup():
    fenced = "```\n登录、注册、找回密码\n```\n"
    assert requirement_enumeration_items(fenced) == []  # 代码围栏里的顿号串不算需求
    long_pieces = "、".join(["这是一段超过二十四个汉字的冗长描述性子句用来验证宽度上限确实生效"] * 3)
    assert requirement_enumeration_items(long_pieces) == []  # 全是长片段 → 不是枚举
    # 行首列表行整行正文就是一项:身体里的顿号不再内联拆(防同一行双记账)。
    bullet = "- 支持登录、注册、找回密码\n- 深色模式\n- 全文搜索\n"
    items = requirement_enumeration_items(bullet)
    assert "支持登录、注册、找回密码" in items
    assert "登录" not in items and "注册" not in items
    # 两通道并集去重:同一项出现在行首清单和内联串里只记一次。
    mixed = "- 全文搜索\n- 深色模式\n- 回收站\n另外要:全文搜索、导入导出、标签分类。\n"
    mixed_items = requirement_enumeration_items(mixed)
    assert mixed_items.count("全文搜索") == 1
    assert "导入导出" in mixed_items and "标签分类" in mixed_items


def test_inline_site_prompt_seeds_ledger_end_to_end(tmp_path):
    # 端到端:内联枚举 → _seed 全套护栏(≥3 才种/寫账/注入告知)原样生效。
    params = _Params(run_id="run-inline", task_id="run-inline", root_user_prompt=INLINE_SITE_PROMPT)
    updated = run_params_with_requirement_coverage_seed(_agent(tmp_path), INLINE_SITE_PROMPT, params)
    assert updated.inject and "[requirement-coverage-seed]" in updated.inject[-1]
    targets = read_task_progress(tmp_path, "run-inline")["coverage"]["targets"]
    assert len(targets) >= 10
    assert all(target["id"].startswith("req-") for target in targets)


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


WAKE_INSTRUCTIONS = (
    "1. 先看 Active Wake Signal\n2. Recent Observations\n3. Agent Tree Snapshot 看清\n"
    "读取、整合、验证、收尾全是你自己动手。\n"
)


def test_background_wake_never_seeds_from_wake_instructions(tmp_path):
    # 真机实锤(干净轮 u-a2c1 问候线冒 13 条指令假需求):后台唤醒轮的 prompt 是机器拼的整合指令,
    # 不是需求。且 runtime_mixin 在种子挂钩前已把空 root_user_prompt 回填成本轮 user_prompt=指令,
    # 故"root_user_prompt 是否为空"分不出来——判据必须是 source。前台种、后台绝不种。
    # ① 回填后的真实形态:root_user_prompt 已是指令(=user_prompt),仍不种。
    filled = _Params(run_id="bg-main-1", task_id="task-greet", source="background_main_agent", root_user_prompt=WAKE_INSTRUCTIONS)
    assert run_params_with_requirement_coverage_seed(_agent(tmp_path), WAKE_INSTRUCTIONS, filled) is filled
    assert "coverage" not in read_task_progress(tmp_path, "task-greet")
    # ② 连真实需求恰好透传进后台轮也不种(需求 coverage 已在前台创建路种好,后台只读不重种)。
    real = _Params(run_id="bg-main-2", task_id="task-real", source="background_main_agent", root_user_prompt=BUILD_PROMPT)
    assert run_params_with_requirement_coverage_seed(_agent(tmp_path), BUILD_PROMPT, real) is real
    assert "coverage" not in read_task_progress(tmp_path, "task-real")
    # ③ 同样的建站需求走前台路(cli_run)照常种——证明拦的是 source 不是内容。
    fg = _Params(run_id="fg-1", task_id="fg-1", source="cli_run", root_user_prompt=BUILD_PROMPT)
    run_params_with_requirement_coverage_seed(_agent(tmp_path), BUILD_PROMPT, fg)
    assert read_task_progress(tmp_path, "fg-1")["coverage"]["targets"]


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
