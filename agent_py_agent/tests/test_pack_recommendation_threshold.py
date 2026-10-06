"""learnpack 第 6 步：能力包推荐门槛（评测 4 个包的离线打分回归，不调模型）。

锁定：本轮能力包候选只推荐"包声明的名称或关键词整个出现在提问里"的包（纯 ASCII 的词两头不能紧挨英文字母）；只撞上描述、
适用场景里的常用词（"脚本""网文的"），或提问片段撞上关键词的一部分（"工作目录"的"工作"撞"相关工作"）都不推荐；领域请求照样
命中自己的包，"小说改编成短剧分镜"同时命中两个包；无关请求一个不推荐、提示字节为空；宿主一次选择选中的包不受门槛影响；
score_card 的分数和 search 排序不变（skill_search 主动搜照旧能按描述搜到）。代价（有意为之，记在 DESIGN_LEDGER）：关键词写成长短语的包，
用户只说了其中一部分时不进当轮候选，但主动搜照样搜得到。
4 个包的名称、说明、关键词、摘要取自 bench-2026-10-05 评测里 my-agent 学出来的包（短剧、长篇小说、数据分析、学术写作）。
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.capability.router import (
    CapabilityRouter,
    from_capability_package,
    has_strong_match,
    score_card,
)
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot
from agent_py_agent.tests.test_capability_package_discovery import package_fixture

# (包名, 摘要, 说明, 关键词)，与评测包的 declaration.json 一致。
_BENCH_PACKS = (
    ("short-drama-pro", "短剧文字交付：剧本、分场、分镜表、人物与道具连续性、审核清单",
     "适用于短剧的文字制作交付——剧本、分场、分镜表、人物与道具连续性和审核清单；只做文字资料，不生成图片、视频或配音。",
     ("短剧", "剧本", "分场", "分镜", "连续性", "审核清单", "制作交付")),
    ("longnovel-cn", "中文长篇网文的分阶段规划、章节写作与一致性回证",
     "适用于长篇连载：分阶段规划、逐章写作、状态回证与一致性审查；按 JSON 状态维护人物、伏笔、资源与时间线，"
     "确定性检查由包内脚本完成，语义一致性走方法清单。",
     ("小说", "长篇", "网文", "连载", "伏笔", "一致性", "写作")),
    ("data-analysis-pro", "表格数据的清洗、统计、可视化与结论报告",
     "适用于本地 CSV/Excel 表格数据的清洗记录、基础统计、静态图表与可回溯结论报告；不联网取数，不代跑外部生成服务。",
     ("数据分析", "数据清洗", "统计", "图表", "报告", "CSV")),
    ("academic-writing-pro", "只用给定材料做学术写作与修改：结构、摘要、相关工作与审稿回复的证据链",
     "适用于论文/报告的结构规划、摘要、相关工作组织、审稿意见回复与修订；只消费给定材料，逐条声明必须挂到可复核的证据记录，"
     "不联网检索、不编造引用。",
     ("学术写作", "论文", "摘要", "相关工作", "审稿回复", "证据链", "修订")),
)


def _packages():
    return [replace(package_fixture(name), summary=summary, description=description, keywords=keywords)
            for name, summary, description, keywords in _BENCH_PACKS]


def _router() -> CapabilityRouter:
    snapshot = SkillSnapshot((), (), "fixture", "local/main", "/unused", packages=tuple(_packages()))
    return CapabilityRouter(config=CapabilityConfig(capability_candidate_limit=5), skill_snapshot=snapshot)


def _recommended(query: str, selected=None) -> list[str]:
    text = _router().render_package_recommendations(query, limit=5, selected_skill_ids=selected)
    return [name for name, *_ in _BENCH_PACKS if f'"package_id": "{name}"' in text]


@pytest.mark.parametrize(("query", "expected"), [
    ("帮我把这个故事改成一集短剧，要分场和分镜表", ["short-drama-pro"]),
    ("帮我写长篇网文的前三章，注意伏笔", ["longnovel-cn"]),
    ("这部小说的人物前后不一致，帮我查一下", ["longnovel-cn"]),
    ("这份销售 CSV 帮我清洗一下再做统计图表", ["data-analysis-pro"]),
    ("帮我改论文摘要并写审稿回复", ["academic-writing-pro"]),
    ("把这部小说改编成短剧分镜", ["short-drama-pro", "longnovel-cn"]),
])
def test_domain_requests_still_hit_their_packs(query, expected):
    assert _recommended(query) == expected


@pytest.mark.parametrize(("query", "weak_pack"), [
    ("帮我写个自动化脚本", "longnovel-cn"),                   # "脚本"只在小说包的说明里
    ("这篇文的格式整理一下", "longnovel-cn"),                 # "文的"只撞上摘要里的"网文的"
    ("工作目录里有一份短剧剧本草稿", "academic-writing-pro"),  # "工作"只是关键词"相关工作"的一部分
])
def test_weak_hits_are_not_recommended(query, weak_pack):
    card = next(from_capability_package(item) for item in _packages() if item.package_id == weak_pack)
    assert score_card(query, card)[0] > 0, "打分照旧大于 0（skill_search 主动搜照样搜得到）"
    assert not has_strong_match(query, card) and weak_pack not in _recommended(query)


def test_unrelated_requests_recommend_nothing_and_keep_prompt_bytes_empty():
    for query in ("明天天气怎么样", "帮我发封邮件给客户", "电脑风扇很吵怎么办", "帮我写个自动化脚本"):
        assert _router().render_package_recommendations(query, limit=5) == "", query


def test_ascii_terms_need_letter_boundaries_and_names_count():
    card = from_capability_package(replace(package_fixture("stats-r"), keywords=("R", "csv", "", " ")))
    assert has_strong_match("用 R 语言画个图", card) and has_strong_match("inputs/sales_dirty.csv", card)
    assert not has_strong_match("写一份 report", card), "单个字母不能撞进英文单词里"
    assert not has_strong_match("明天天气怎么样", card), "空关键词不算命中"
    assert _recommended("用 longnovel-cn 那个包接着往下写") == ["longnovel-cn"], "提问里点了包名也算"


def test_long_phrase_keywords_trade_recall_for_precision():
    pack = replace(package_fixture("subtitle-pro"), summary="影视字幕翻译", description="影视字幕翻译与时间轴校对",
                   keywords=("影视字幕翻译", "时间轴校对"))
    snapshot = SkillSnapshot((), (), "fixture", "local/main", "/unused", packages=(pack,))
    router = CapabilityRouter(config=CapabilityConfig(capability_candidate_limit=5), skill_snapshot=snapshot)
    query = "帮我把这段字幕翻译成英文"
    assert router.render_package_recommendations(query, limit=5) == "", "长短语关键词只说了一部分，不进当轮候选"
    assert [hit.card.name for hit in router.search(query, limit=0, kinds={"capability_package"})] == ["subtitle-pro"]
    assert router.render_package_recommendations("这一集的影视字幕翻译交给你", limit=5) != ""


def test_host_selected_packs_bypass_the_threshold():
    card = next(from_capability_package(item) for item in _packages() if item.package_id == "longnovel-cn")
    assert _recommended("帮我写个自动化脚本", selected=(card.id,)) == ["longnovel-cn"]
    assert _recommended("帮我写个自动化脚本", selected=()) == []
