"""Tier-1 的纯图、预算和公开索引不变量；不调用模型或执行资产。"""
from copy import deepcopy

import pytest

from agent_py_agent.agent.capability.method_tree import build_method_tree, render_method_catalog
from agent_py_agent.agent.capability.router import (
    _SKILL_METADATA_HEADER,
    _SKILL_USAGE_INSTRUCTIONS,
    CapabilityCard,
    CapabilityRouter,
)


# LLM: 样本仅声明资产元数据，不产生存储或执行副作用。
# 函数用途: 生成可独立修改的目录描述符。
def descriptor(kind, identifier, *, summary="", refs=None, metadata=None, enabled=True, source="owner"):
    return {"kind": kind, "id": identifier, "name": identifier, "summary": summary,
            "refs": refs or {}, "metadata": metadata or {}, "enabled": enabled, "source_scope": source}


# LLM: 使用审计明确的名称、ID和中文说明长度；规模数据不是生产常量。
# 函数用途: 建立重用户、有绑定和超规格三组预算输入。
def inventory(skills=20, workflows=50, scripts=100, *, bindings=True, monitors=5, distinct_categories=False):
    rows = [descriptor("skill", f"skill-name-{i:03d}", summary="方法说明来源核对与事件关联步骤验收" * 4,
                       metadata={"stable_id": f"owner:skill-name-{i:03d}", "source": "owner"}) for i in range(skills)]
    rows.extend(descriptor("workflow", f"workflow-id-{i:04d}", summary="工作流说明来源核对和状态不变量检查" * 3,
                           refs={"scripts": [f"fixed-script-{j:03d}" for j in range(i * 6, i * 6 + 6)]}
                           if bindings and i < 10 else {}) for i in range(workflows))
    rows.extend(descriptor("script", f"fixed-script-{i:03d}", metadata={"category": "indicator"}) for i in range(scripts))
    if bindings:
        for i in range(min(10, skills, workflows)):
            rows[i]["refs"] = {"workflows": [f"workflow-id-{i:04d}"]}
    rows.extend(descriptor("monitor", f"monitor-{i}", refs={"workflows": [f"workflow-id-{i:04d}"]},
                           metadata={"alias": f"job{i:03d}"}) for i in range(monitors))
    if distinct_categories:
        # L4按类目计数，只有类目本身也超出预算时才应进入L5；资产数不是档位阈值。
        for row in rows:
            if row["kind"] in {"workflow", "script"}:
                row["metadata"]["category"] = row["id"]
    return rows


# LLM: 统一使用路由器原始技能使用说明计入整个块的费用。
# 函数用途: 从描述符生成可核对的完整目录和阶梯测量值。
def catalog(rows):
    return render_method_catalog(rows, skill_usage_instructions=_SKILL_USAGE_INSTRUCTIONS,
                                 skill_header=_SKILL_METADATA_HEADER, bytes_per_token=4)


# LLM: 在用列表只影响易变区，不允许改变公开技能的字节或包含范围。
# 函数用途: 覆盖公开模式 prompt 和 in-use 独立性，包含未推荐方法。
def test_public_catalog_ignores_prompt_and_in_use_including_inclusion():
    cards = [CapabilityCard("skill:a", "skill", "Alpha", "alpha source", source="owner",
                            metadata={"stable_id": "owner:alpha"}),
             CapabilityCard("skill:z", "skill", "Zulu", "job48 source", source="owner",
                            metadata={"stable_id": "owner:zulu"}),
             CapabilityCard("skill:off", "skill", "Not recommended", "source", source="owner",
                            metadata={"stable_id": "owner:off", "recommended": False})]
    router = CapabilityRouter(extra_cards=cards)
    outputs = [router.render_skill_metadata_index(query=q, in_use_method_ids=used)
               for q in ("plain", "192.0.2.1 alert", "job48")
               for used in ((), ("owner:zulu", "owner:off", "script:owner:missing"))]
    assert len(set(outputs)) == 1
    assert "owner:off" not in outputs[0] and "本会话在用" not in outputs[0]
    assert outputs[0].index("owner:alpha") < outputs[0].index("owner:zulu")


# LLM: 直连绑定子项只在父徽标计数，所有可见技能仍是明确入口。
# 函数用途: 核对绑定折叠、共享计数和不可见父不隐藏子项。
def test_bound_children_shared_parent_counts_and_invisible_parent():
    rows = [descriptor("skill", "s", refs={"workflows": ["w"]}, metadata={"stable_id": "owner:s"}),
            descriptor("workflow", "w", refs={"scripts": ["x"]}),
            descriptor("workflow", "other", refs={"scripts": ["x"]}), descriptor("script", "x"),
            descriptor("workflow", "hidden", refs={"scripts": ["free"]}, metadata={"hidden": True}),
            descriptor("script", "free")]
    tree = build_method_tree(rows)
    assert set(tree["roots"]) == {"skill:owner:s", "workflow:other", "script:free"}
    assert tree["children"]["workflow:w"] == ["script:x"]
    text = catalog(rows)["text"]
    assert "(skill_id: owner:s) ⤷1" in text and "[workflow] other" in text and "⤷1" in text
    assert "[workflow] w:" not in text and "hidden" not in text
    assert "free" in text and "fixed-script" not in text


# LLM: SCC 只需线性分解，不能枚举简单环或以输入顺序决定根。
# 函数用途: 锁定无外部父循环的最小键及外部父存在时的折叠行为。
def test_cycle_minimum_root_and_external_parent():
    rows = [descriptor("workflow", "b", refs={"workflows": ["a"]}),
            descriptor("workflow", "a", refs={"workflows": ["b"]})]
    assert build_method_tree(rows)["roots"] == ["workflow:a"]
    rows.append(descriptor("skill", "s", refs={"workflows": ["b"]}, metadata={"stable_id": "owner:s"}))
    assert build_method_tree(rows)["roots"] == ["skill:owner:s"]


# LLM: 徽标只读显式别名和引用；隐藏组合只将目标重定向到基础工作流。
# 函数用途: 验证行内别名上限、无行目标和隐藏组合监控。
def test_monitor_badges_hidden_composition_and_unattached_targets():
    rows = [descriptor("skill", "s", refs={"workflows": ["bound"]}, metadata={"stable_id": "owner:s"}),
            descriptor("workflow", "bound"), descriptor("workflow", "base"), descriptor("script", "alone"),
            descriptor("workflow", "monitor-m", metadata={"hidden": True, "composition": {"base_workflow_id": "base"}})]
    rows.extend(descriptor("monitor", f"m{i}", refs={"workflows": ["monitor-m"]},
                           metadata={"alias": f"job{i}"}) for i in range(4))
    rows.extend([descriptor("monitor", "child", refs=[{"kind": "workflow", "id": "bound"}], metadata={"alias": "child-job"}),
                 descriptor("monitor", "drawer", refs={"scripts": ["alone"]}, metadata={"alias": "drawer-job"})])
    result = catalog(rows)
    assert "⏰job0,job1(+2)" in result["text"]
    assert "[⏰] child-job→workflow:bound" in result["text"]
    assert "drawer-job→script:alone" in result["text"]
    assert "[workflow] monitor-m" not in result["text"]
    assert all(node["kind"] != "monitor" for node in build_method_tree(rows)["nodes"].values())


# LLM: 三组规模使用同一估算器；首个满足预算的级别才可采用。
# 函数用途: 证明重用户、最坏和超规格输入都在完整2500-token信封内。
@pytest.mark.parametrize("scale,allowed", [("gate", {"L1"}), ("worst", {"L2", "L3"}), ("beyond", {"L5"})])
def test_budget_ladder_first_fitting_level(scale, allowed):
    if scale == "gate":
        rows = inventory()
    elif scale == "worst":
        rows = inventory(30, 50, 100, bindings=False, monitors=0)
    else:
        rows = inventory(30, 300, 600, bindings=False, monitors=0, distinct_categories=True)
    result = catalog(rows)
    assert result["level"] in allowed
    assert result["tokens"] <= 2500
    assert result["tokens"] == (len(result["text"].encode()) + 3) // 4
    assert all(cost > 2500 for cost in result["level_tokens"][:int(result["level"][1:])])
    assert _SKILL_USAGE_INSTRUCTIONS in result["text"]
    assert "scripts/workflows(action=list)" in result["text"]


# LLM: 同一类目的库存即使超规格也不能绕过已适配的L4，避免按资产数量硬编码档位。
# 函数用途: 保留原失败输入，证明先适配原则与L5压力用例同时成立。
def test_beyond_scale_single_category_keeps_first_fitting_level():
    result = catalog(inventory(30, 300, 600, bindings=False, monitors=0))
    assert result["level"] == "L4"
    assert result["level_tokens"][4] <= 2500
    assert all(cost > 2500 for cost in result["level_tokens"][:4])
    assert result["tokens"] == (len(result["text"].encode()) + 3) // 4


# LLM: 所有可变调用信息和版本观察均不得进入固定目录。
# 函数用途: 验证排序、原输入不变和库存事件仅触发一次字节变化。
def test_inventory_only_order_and_event_changes():
    rows = inventory(2, 3, 4, monitors=0)
    before = deepcopy(rows)
    text = catalog(rows)["text"]
    assert catalog(list(reversed(rows)))["text"] == text and rows == before
    for row in rows:
        row.update(revision=991, version="v999", runtime_summary={"last_run_at": "tomorrow"})
        row["metadata"].update(known_pitfalls=["volatile-note"], trigger_matchers={"monitor_ids": ["later"]})
    assert catalog(rows)["text"] == text
    rows.append(descriptor("script", "added"))
    changed = catalog(rows)["text"]
    assert changed != text and changed == catalog(rows)["text"]


# LLM: 清理展示文本控制字符但不解释或执行说明中的指令。
# 函数用途: 阻止多行说明创建额外目录结构，并确定类目回退规则。
def test_summaries_single_line_and_category_slug():
    rows = [descriptor("skill", "s", summary="first\n### another\r\x00\u2028last", metadata={"stable_id": "owner:s"}),
            descriptor("script", "one", metadata={"category": "SOURCE_QUERY"}), descriptor("script", "two")]
    rows[2]["tags"] = ["event-correlation"]
    text = catalog(rows)["text"]
    assert "\x00" not in text and "\r" not in text and "\u2028" not in text
    assert not any(line.startswith("### another") for line in text.splitlines())
    assert "[script·source_query] 1: one" in text and "[script·event-correlation] 1: two" in text


# LLM: 极大技能集合允许既有省略语义，整块仍必须有完整用法和预算。
# 函数用途: 为超过150个技能和长标识符保留有界退出路径。
def test_many_skills_omit_whole_lines_and_bound_envelope():
    rows = inventory(400, 1, 1, bindings=False, monitors=0)
    result = catalog(rows)
    assert result["tokens"] <= 2500 and "未显示" in result["text"]
    assert _SKILL_USAGE_INSTRUCTIONS in result["text"]


# LLM: 路由入口必须消费同一冻结绑定，不能只有纯函数测试通过；样本仅使用隔离Store。
# 函数用途: 验证真实公开目录的技能折叠、方法标题和完整预算。
@pytest.mark.parametrize("name,reverse", [("review", False), ("中文方法", True)])
def test_router_uses_frozen_bindings_and_fixed_method_header(tmp_path, name, reverse):
    from types import MethodType

    from agent_py_agent.agent.assets import skill_asset_id
    from agent_py_agent.tests.test_v15_w2_l1_skill_search import _script, _surface, _workflow

    agent = _surface(tmp_path, name=name, refs={} if reverse else {"workflows": ["bound"]})
    metadata = {"member_of": [{"kind": "skill", "id": skill_asset_id(name)}]} if reverse else {}
    _workflow(agent, "bound", scripts=("child",), metadata=metadata)
    _script(agent, "child")

    # LLM: 生产路由只接受宿主绑定的快照提供者，不从任意回调或路径猜Owner。
    # 函数用途: 给测试Agent提供有明确宿主身份的快照方法。
    def snapshot_for(self):
        return self._current_skill_snapshot

    agent.current_skill_snapshot = MethodType(snapshot_for, agent)
    router = CapabilityRouter(skill_snapshot_provider=agent.current_skill_snapshot)
    text = router.render_skill_metadata_index()
    assert f"(skill_id: owner:{name}) ⤷1" in text
    assert "### 固定方法（工作流 / 脚本）" in text
    assert "[workflow] bound" not in text and "[script·" not in text
    assert (len(text.encode("utf-8")) + 3) // 4 <= 2500


# LLM: W8错误字段必须在同一生产解析口按缺失处理，不能让未哈希类型中断目录或L1。
# 函数用途: 复现并锁定monitor列表引用中错误kind类型的容错行为。
@pytest.mark.parametrize("bad_kind", [[], {}])
def test_monitor_wrong_type_ref_kind_is_ignored(bad_kind):
    rows = [descriptor("workflow", "w"),
            descriptor("monitor", "m", refs=[{"kind": bad_kind, "id": "w"}], metadata={"alias": "job"})]
    failure = None
    tree = None
    try:
        tree = build_method_tree(rows)
    except TypeError as exc:
        failure = str(exc)
    assert failure is None, failure
    assert tree["monitors"] == []
    assert tree["roots"] == ["workflow:w"]


# LLM: 同一W8容错合同也覆盖别名，错误类型不是可展示的别名。
# 函数用途: 防止把列表、字典或数字强制转成监控徽标。
@pytest.mark.parametrize("bad_alias", [["wrong"], {"wrong": 1}, 1])
def test_monitor_wrong_type_alias_is_ignored(bad_alias):
    rows = [descriptor("workflow", "w"),
            descriptor("monitor", "m", refs=[{"kind": "workflow", "id": "w"}], metadata={"alias": bad_alias})]
    assert build_method_tree(rows)["monitors"] == []


# LLM: 隐藏标记的意义不随无关固定资产出现而变化，公开技能回退也必须遵守同一可见性。
# 函数用途: 成对验证无资产回退和方法树路径都不泄露隐藏技能。
@pytest.mark.parametrize("with_asset", [False, True])
def test_public_hidden_skill_absent_with_or_without_fixed_assets(with_asset):
    cards = [CapabilityCard("skill:hidden", "skill", "hidden-method-name", "private summary", source="owner",
                            metadata={"stable_id": "owner:hidden", "hidden": True}),
             CapabilityCard("skill:visible", "skill", "visible", "public summary", source="owner",
                            metadata={"stable_id": "owner:visible"})]
    if with_asset:
        cards.append(CapabilityCard("script:owner:alone", "script", "alone", "source", source="owner",
                                    metadata={"stable_id": "script:owner:alone", "asset_id": "alone"}))
    text = CapabilityRouter(extra_cards=cards).render_skill_metadata_index()
    assert "owner:hidden" not in text and "hidden-method-name" not in text
    assert "owner:visible" in text


# LLM: 包的在用标记和说明只属于包区，不能通过共享预算使固定Skill目录退成单项兜底。
# 函数用途: 覆盖默认、常用token窗口与硬字符信封下的包/Skill预算隔离。
@pytest.mark.parametrize("window", [None, 128000, 1000000])
def test_package_in_use_does_not_collapse_or_change_public_skill_block(window):
    cards = [CapabilityCard(f"skill:{i}", "skill", f"method-{i:03}", "来源核对和状态不变量检查" * 100,
                            source="owner", metadata={"stable_id": f"owner:method-{i:03}"})
             for i in range(20)]
    package = CapabilityCard("capability:midturn-method", "capability_package", "midturn-method",
                             "把长篇材料整理成分镜", metadata={"stable_id": "capability:midturn-method",
                             "package_id": "midturn-method"})
    router = CapabilityRouter(extra_cards=[*cards, package])
    outputs = [router.render_skill_metadata_index(context_window_tokens=window, in_use_method_ids=used)
               for used in ((), ("capability:midturn-method",))]
    skill_blocks = [text.split("# Available Skills", 1)[1] for text in outputs]
    assert skill_blocks[0] == skill_blocks[1]
    assert skill_blocks[0].count("(skill_id:") > 1
    assert "midturn-method（本会话在用）" in outputs[1]
    assert all(len(text) <= 12000 for text in outputs)
    if window is not None:
        assert all((len(text.encode("utf-8")) + 3) // 4 <= window * 2 // 100 for text in outputs)
