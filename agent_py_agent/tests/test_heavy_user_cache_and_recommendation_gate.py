"""P5.GATE1：真实重用户库存、原生多轮及强制压缩；仅供应商使用本地替身。"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams, RuntimeContextRequest
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.monitor_adapter import (
    list_monitor_descriptors,
    save_monitor_binding,
)
from agent_py_agent.agent.assets.workflow_store import WorkflowDefinition, WorkflowStepSpec
from agent_py_agent.agent.assets.workspace_assets import commit_asset_batch
from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse, ProviderToolCapability
from agent_py_agent.agent.capability.decision_candidates import method_descriptors_for_agent
from agent_py_agent.agent.capability.method_tree import build_method_tree, render_method_catalog
from agent_py_agent.agent.capability.router import _SKILL_METADATA_HEADER, _SKILL_USAGE_INSTRUCTIONS
from agent_py_agent.agent.conversation.authority import CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR
from agent_py_agent.agent.conversation.compact import (
    ConversationCompactOptions,
    prepare_conversation_context,
)
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactModelSurface,
    prepare_conversation_compact_provider_surface,
)
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.compact_summary_view import (
    AppliedCompactContext,
    resolve_compact_summary_view,
)
from agent_py_agent.agent.conversation.models import ConversationHistorySeed
from agent_py_agent.agent.conversation.native_history import (
    canonical_native_messages_envelope,
    provider_history_messages_from_rows,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.prompting_parts.cache_layout import prompt_cache_layout
from agent_py_agent.agent.settings import AgentConfig


# LLM: 只替代供应商生成与能力声明；目录、工具执行、线程写入和压缩均由原产品实现。
# 类用途: 捕获每一次真实生成请求，并按测试显式设置的队列返回原生工具调用。
class _CaptureProvider(EchoBackend):
    supports_system_instructions = True
    supports_provider_request_options = True
    model_name = "gate1-local-provider"
    max_tokens = 128
    context_window_tokens = 262144
    stream_enabled = False

    # LLM: 状态仅属于当前pytest场景，不读取凭据、不启动网络或共享缓存。
    # 函数用途: 初始化请求记录及本轮供应商响应队列。
    def __init__(self):
        self.calls = []
        self.actions = []
        self.phase = ""
        self.compacting = False

    # LLM: 固定的能力事实仅描述本地替身，不冒充真实供应商探测。
    # 函数用途: 使原工具循环沿native协议执行。
    def probe_tool_capability(self):
        return ProviderToolCapability(
            provider=self.name,
            endpoint="local://gate1",
            model=self.model_name,
            stream=False,
            native_supported=True,
            evidence="local_fixture",
            observed_at="2026-11-04T00:00:00Z",
        )

    # LLM: 测试窗口来自显式供应商配置，不依赖用户环境或真实模型目录。
    # 函数用途: 为原预算链声明固定的本地上下文容量。
    def provider_context_window_tokens(self):
        return self.context_window_tokens

    # LLM: 不修改prompt、tools或messages；完整保留结构化缓存边界和工具schema顺序。
    # 函数用途: 记录所有主模型与摘要请求，返回可由原native循环消费的合法响应。
    def generate(
        self,
        prompt,
        on_chunk=None,
        tools=None,
        tool_choice=None,
        messages=None,
        on_thinking_delta=None,
        request_options=None,
    ):
        layout = prompt_cache_layout(prompt)
        assert layout is not None and layout.stable_prefix, "供应商请求必须携带非空原生缓存布局"
        self.calls.append(
            {
                "phase": self.phase,
                "compact": self.compacting,
                "layout": layout,
                "tools": deepcopy(tools),
                "tool_choice": deepcopy(tool_choice),
                "messages": deepcopy(messages),
                "request_options": request_options,
            }
        )
        assert len(self.calls) <= 20, "本地供应商场景必须有界收敛"
        if self.compacting:
            text = (
                "[compact-live-handoff.v1]\ncurrent_progress: 已读取方法并核对来源\n"
                "user_constraints: 保留当前库存和证据引用\ncompleted: 前两轮\n"
                "failures: 无\nunresolved: 核对监控\nnext_step: 处理当前监控"
            )
            return ModelResponse(text=text, backend=self.name)
        if self.actions:
            name, arguments = self.actions.pop(0)
            identifier = f"gate1-call-{len(self.calls)}"
            return ModelResponse(
                text="",
                backend=self.name,
                stop_reason="tool_use",
                tool_use_blocks=[{"id": identifier, "name": name, "input": arguments}],
                assistant_content_blocks=[
                    {"type": "tool_use", "id": identifier, "name": name, "input": arguments}
                ],
            )
        return ModelResponse(text="GATE1完成", backend=self.name)


# LLM: 所有落盘只发生在pytest的私有fixture目录，不接触产品配置或真实Owner。
# 函数用途: 写入可由真实只读入口解析的JSON样本。
def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


# LLM: 库存通过真实Store、Skill发现和监控绑定建立；不替换任何被测目录/推荐行为。
# 函数用途: 构造三档重用户环境，固定10个绑定工作流、60个绑定脚本和5个监控。
def _heavy_case(tmp_path, counts):
    skill_count, workflow_count, script_count = counts
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = AgentConfig(
        model_backend="echo",
        my_agent_home=str(tmp_path / "home"),
        memory_path=str(tmp_path / "memory.jsonl"),
        enable_tools=True,
        enable_self_learning=False,
        prompt_files=[],
        model_context_window_tokens=262144,
        max_tokens=128,
        memory_compact_remote_enabled=False,
    )
    agent = SimpleAgent(config, workspace)
    provider = _CaptureProvider()
    agent.backend = provider
    home = agent.home_paths.owner_home_dir
    # 限定测试输入为当前Owner的技能，避免随包技能数变化污染三档规模。
    _write_json(
        agent.home_paths.owner_skill_policy_json, {"enabled": True, "enabled_sources": ["owner"]}
    )
    for number in range(skill_count):
        name = f"skill-name-{number:03d}"
        refs = {"workflows": [f"workflow-id-{number:04d}"]} if number < 10 else {}
        header = {
            "name": name,
            "description": "方法说明来源核对与事件关联步骤验收" * 4,
            "refs": refs,
        }
        path = home / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        front = "\n".join(
            f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in header.items()
        )
        path.write_text(
            f"---\n{front}\n---\n# 状态不变量检查方法\n保留原始证据引用。\n", encoding="utf-8"
        )
    # 门禁验证目录/推荐，不逐项验收CRUD；真实批量入口保留校验、落盘、历史和步骤引用，避免重复读回。
    assets = []
    for number in range(script_count):
        assets.append({
            "descriptor": AssetDescriptor(
                f"fixed-script-{number:03d}",
                "script",
                f"fixed-script-{number:03d}",
                summary="来源查询与证据核对",
                io_contract={
                    "inputs": ["indicator", "scope"],
                    "outputs": ["result", "evidence_refs"],
                },
                metadata={
                    "category": "indicator",
                    "trigger_matchers": {"indicator_types": ["ipv4"]} if number == 0 else {},
                },
            ),
            "code": 'raise AssertionError("fixture scripts must never execute")\n',
        })
    for number in range(workflow_count):
        children = range(number * 6, number * 6 + 6) if number < 10 else ()
        steps = tuple(
            WorkflowStepSpec(
                f"step-{index}",
                "来源查询",
                "deterministic_script",
                script_id=f"fixed-script-{index:03d}",
            )
            for index in children
        )
        assets.append(
            WorkflowDefinition(
                AssetDescriptor(
                    f"workflow-id-{number:04d}",
                    "workflow",
                    f"workflow-id-{number:04d}",
                    summary="工作流说明来源核对和状态不变量检查" * 3,
                ),
                steps,
                body_markdown="# 结构化事件关联\n只读取正式来源。\n",
            ).to_dict()
        )
    committed = commit_asset_batch(assets, owner_home=home)
    assert committed["committed"] is True
    assert len(committed["items"]) == script_count + workflow_count
    assert {(row["kind"], row["id"]) for row in committed["items"]} == {
        *(('script', f"fixed-script-{number:03d}") for number in range(script_count)),
        *(('workflow', f"workflow-id-{number:04d}") for number in range(workflow_count)),
    }
    for number in range(5):
        save_monitor_binding(
            home,
            f"monitor-{number}",
            name=f"监控-{number}",
            alias=f"job{48 + number}",
            workflow_ids=[f"workflow-id-{number:04d}"],
            schedule_spec={"kind": "every", "expression": "3600", "enabled": True},
        )
    _write_json(
        home / "data/asset_evolution/pitfalls/script/fixed-script-000.json",
        {
            "schema_version": "asset_pitfalls.v1",
            "kind": "script",
            "asset_id": "fixed-script-000",
            "entries": [{"text": "先验证scope边界"}, {"text": "保留原始证据引用"}],
        },
    )
    agent.skills_service.clear_cache()
    snapshot = agent.current_skill_snapshot()
    assert len(snapshot.enabled_entries()) == skill_count
    assert {entry.stable_id for entry in snapshot.enabled_entries()} == {
        f"owner:skill-name-{number:03d}" for number in range(skill_count)
    }
    descriptors = method_descriptors_for_agent(agent)
    assert {
        kind: sum(row["kind"] == kind for row in descriptors)
        for kind in ("skill", "workflow", "script", "monitor")
    } == {"skill": skill_count, "workflow": workflow_count, "script": script_count, "monitor": 5}
    tree = build_method_tree(descriptors)
    assert (
        sum(tree["nodes"][key]["kind"] == "workflow" for key in tree["roots"])
        == workflow_count - 10
    )
    assert sum(tree["nodes"][key]["kind"] == "script" for key in tree["roots"]) == script_count - 60
    assert len(list_monitor_descriptors(home)) == 5
    thread = agent.conversation_store.threads.get_or_create(
        {"channel": "test", "channel_conversation_id": "gate1"}
    )
    return agent, provider, thread, descriptors


# LLM: 使用真实canonical行与摘要视图建立每轮参数，不手工复制或补造工具加载状态。
# 函数用途: 执行一轮真实工具循环，并持久化其正式native回放结果。
def _run_turn(agent, provider, thread, phase, prompt, *, actions=()):
    store = agent.conversation_store
    current = store.threads.require(thread.thread_id)
    view = resolve_compact_summary_view(agent, current, THREAD_COMPACT_SCOPE)
    prior = provider_history_messages_from_rows(store.messages.recent(thread.thread_id, limit=0))
    request_id = f"gate1-{phase}"
    store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": prompt,
            "metadata": {"conversation_request_id": request_id},
        }
    )
    provider.phase, provider.actions = phase, list(actions)
    before = len(provider.calls)
    result = agent.run(
        prompt,
        params=RunParams(
            save=False,
            context_scope="conversation",
            request_id=request_id,
            run_id=request_id,
            task_attributes={
                CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
                "conversation_thread_id": thread.thread_id,
            },
            conversation_history_seed=ConversationHistorySeed(
                compact_summary=view.summary,
                compact_generation=view.generation,
                canonical_messages=prior,
            ),
            compact_context=AppliedCompactContext(thread.thread_id, THREAD_COMPACT_SCOPE, view),
        ),
    )
    assert result.response == "GATE1完成", result.response
    assert not provider.actions, "供应商队列中的原生工具调用必须真实执行完"
    store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "assistant",
            "content": result.response,
            "metadata": {
                "conversation_request_id": request_id,
                "canonical_native_messages": canonical_native_messages_envelope(
                    result.canonical_native_messages
                ),
            },
        }
    )
    calls = provider.calls[before:]
    assert calls and all(call["phase"] == phase and not call["compact"] for call in calls)
    return calls


# LLM: 只比较供应商已经收到的冻结输入；不从集合相等推断工具数组顺序相等。
# 函数用途: 核对I1、I2、I3和I4的稳定字节及完整工具schema。
def _assert_stable_requests(calls, reference):
    expected = reference["layout"].stable_prefix.encode("utf-8")
    expected_names = [row["name"] for row in reference["tools"]]
    assert expected and {"scripts", "workflows"} <= set(expected_names)
    expected_hash = hashlib.sha256(expected).hexdigest()
    for call in calls:
        actual = call["layout"].stable_prefix.encode("utf-8")
        assert hashlib.sha256(actual).hexdigest() == expected_hash, "稳定前缀hash必须逐请求相同"
        assert actual == expected, "稳定前缀必须逐字节相同"
        assert [row["name"] for row in call["tools"]] == expected_names, (
            "工具名称序列不能重排或卸载"
        )
        assert call["tools"] == reference["tools"], "工具schema不能发生隐式变化"


# LLM: 测试观察的是实际provider消息或其typed布局，不自行渲染一个替代推荐段。
# 函数用途: 找出当前请求中最新的Tier-2段，保留历史段用于压缩去重检查。
def _tier2_sections(call):
    sections = [
        text
        for source, text in call["layout"].volatile_sections
        if source == "prompt.tool_recommendations" and text
    ]
    for message in call["messages"] or []:
        content = message.get("content")
        texts = (
            [content]
            if isinstance(content, str)
            else [
                part.get("text", "")
                for part in content or []
                if isinstance(part, dict) and part.get("type") == "text"
            ]
        )
        for text in texts:
            start = text.find("## 本轮推荐的固定方法")
            if start >= 0:
                sections.append(text[start:])
    return sections


def _full_recommendation_blocks(messages):
    blocks = []
    for message in messages or []:
        content = message.get("content")
        texts = (
            [content]
            if isinstance(content, str)
            else [
                part.get("text", "")
                for part in content or []
                if isinstance(part, dict) and part.get("type") == "text"
            ]
        )
        for text in texts:
            if "## 本轮推荐的固定方法" in text:
                blocks.append(text)
    return blocks


# LLM: 必须核对确切父子、IO字段、别名及两条侧文件经验；标题存在不等于I6通过。
# 函数用途: 对真实Tier-2完整JSON条目执行I6断言。
def _assert_job48_tier2(section):
    rows = [json.loads(line[2:]) for line in section.splitlines() if line.startswith("- {")]
    by_key = {(row["kind"], row["id"]): row for row in rows}
    assert {("workflow", "workflow-id-0000"), ("script", "fixed-script-000")} <= by_key.keys()
    parent, child = by_key[("workflow", "workflow-id-0000")], by_key[("script", "fixed-script-000")]
    assert "script:fixed-script-000" in parent["bound_methods"]
    assert child["inputs"] == ["indicator", "scope"]
    assert child["outputs"] == ["result", "evidence_refs"]
    assert child["known_pitfalls"] == ["保留原始证据引用", "先验证scope边界"]
    assert parent["monitor_aliases"] == child["monitor_aliases"] == ["job48"]
    assert (
        "monitor_alias:job48" in parent["matched_by"]
        and "monitor_alias:job48" in child["matched_by"]
    )
    assert "⏰ job48" in section
    assert len(section.encode("utf-8")) <= 800 * 4


# LLM: 字节估算与中文占比取实际输出，不把审计的理论数字抄成实测，不调用真实tokenizer。
# 函数用途: 计算完整Tier-1块的预算、所选档位和CJK字节比例。
def _measure_catalog(agent, descriptors, stable_prefix):
    rendered = render_method_catalog(
        descriptors,
        skill_usage_instructions=_SKILL_USAGE_INSTRUCTIONS,
        skill_header=_SKILL_METADATA_HEADER,
    )
    actual = agent.capability_router.render_skill_metadata_index(selected_skill_ids=None)
    assert actual == rendered["text"]
    assert actual in stable_prefix, "被测目录必须真的进入供应商稳定前缀"
    assert "## 本轮推荐的固定方法" not in stable_prefix
    assert _SKILL_USAGE_INSTRUCTIONS in actual and "### 固定方法用法" in actual
    raw = actual.encode("utf-8")
    tokens = (len(raw) + 3) // 4
    assert tokens == rendered["tokens"] <= 2500
    chosen = int(rendered["level"][1:])
    assert all(cost > 2500 for cost in rendered["level_tokens"][:chosen])
    cjk_bytes = sum(
        len(char.encode("utf-8"))
        for char in actual
        if "\u3400" <= char <= "\u4dbf"
        or "\u4e00" <= char <= "\u9fff"
        or "\uf900" <= char <= "\ufaff"
    )
    return {
        "tier1_tokens": tokens,
        "tier1_level": rendered["level"],
        "tier1_bytes": len(raw),
        "cjk_bytes": cjk_bytes,
        "cjk_byte_share": cjk_bytes / len(raw),
        "stable_prefix_sha256": hashlib.sha256(stable_prefix.encode("utf-8")).hexdigest(),
    }


# LLM: 每档都走真实三轮、正式skill_search、CAS压缩与工具发现；不patch被测产品函数。
# 函数用途: 将P5.GATE1的I1至I6锁成可反证的端到端本地集成门禁。
@pytest.mark.parametrize(
    "profile,counts",
    [
        ("low", (20, 30, 60)),
        ("high", (30, 50, 100)),
        ("audit", (20, 50, 100)),
    ],
)
def test_heavy_user_cache_recommendation_and_compaction_gate(tmp_path, profile, counts):
    agent, provider, thread, descriptors = _heavy_case(tmp_path, counts)
    plain = "请核对当前处理步骤"
    alert = "关联地址 192.0.2.48 的来源告警"
    monitor = "请核对 job48 的事件与证据"
    first = _run_turn(agent, provider, thread, "plain", plain)
    assert len(first) == 1
    measurements = _measure_catalog(agent, descriptors, first[0]["layout"].stable_prefix)
    second = _run_turn(
        agent,
        provider,
        thread,
        "alert",
        alert,
        actions=(("skill_search", {"action": "get", "skill_id": "owner:skill-name-000"}),),
    )
    assert len(second) == 2, "第二轮必须包含真实skill_search往返"
    assert "状态不变量检查方法" in json.dumps(second[-1]["messages"], ensure_ascii=False)
    stored = agent.conversation_store.threads.require(thread.thread_id)
    assert len(stored.conversation_methods) == 1
    assert "owner:skill-name-000" in json.dumps(stored.conversation_methods, ensure_ascii=False)
    assert list(stored.loaded_tool_names) == ["scripts", "workflows"]
    assert stored.metadata["pinned_tool_names"] == ["scripts", "workflows"]
    latest = _tier2_sections(second[-1])
    assert latest and "fixed-script-000" in latest[-1]

    # 第二轮后的压缩继续消费该轮的当前展示，不从旧历史里随便捞一个同名段。
    attrs = {
        CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
        "conversation_thread_id": thread.thread_id,
    }
    surface = ConversationCompactModelSurface(
        thread_id=thread.thread_id,
        context_scope="conversation",
        loaded_tool_names=tuple(stored.loaded_tool_names),
        presentation_context=RuntimeContextRequest(
            user_prompt=alert,
            inject=[],
            resume_context=False,
            context_scope="conversation",
            task_attributes=attrs,
        ),
    )
    prepared = prepare_conversation_compact_provider_surface(agent, surface, run_id="gate1-compact")
    sections = [
        text
        for source, text in prepared.volatile_sections
        if source == "prompt.tool_recommendations"
    ]
    assert len(sections) == 1 and "fixed-script-000" in sections[0]
    tier2_pos = sections[0].find("## 本轮推荐的固定方法")
    assert tier2_pos >= 0
    tier2_section = sections[0][tier2_pos:]
    assert tier2_section == latest[-1], "压缩面必须保留第二轮的当前推荐"
    live_full_blocks = _full_recommendation_blocks(second[-1]["messages"])
    assert live_full_blocks and live_full_blocks[-1] == sections[0]
    assert live_full_blocks[-1].encode("utf-8") == sections[0].encode("utf-8")
    assert "## 本轮推荐的固定方法" not in prepared.stable_prompt_prefix
    provider.phase, provider.compacting = "compact", True
    before = len(provider.calls)
    try:
        compacted = prepare_conversation_context(
            agent,
            agent.conversation_store,
            stored,
            options=ConversationCompactOptions(force=True, provider_surface=prepared),
        )
    finally:
        provider.compacting = False
    compact_calls = provider.calls[before:]
    assert (
        compacted.compacted and compacted.thread.compact_generation == stored.compact_generation + 1
    )
    assert len(compact_calls) == 1 and compact_calls[0]["compact"]
    assert _tier2_sections(compact_calls[0])[-1] == tier2_section
    compact_full_blocks = _full_recommendation_blocks(compact_calls[0]["messages"])
    assert compact_full_blocks and compact_full_blocks[-1] == sections[0]
    assert compact_full_blocks[-1].encode("utf-8") == sections[0].encode("utf-8")
    # 摘要输入仍含待压缩的旧消息；在压缩后的当前展示面核对唯一、最新的推荐。
    post_compact_surface = prepare_conversation_compact_provider_surface(
        agent, surface, run_id="gate1-post-compact"
    )
    assert [
        text
        for source, text in post_compact_surface.volatile_sections
        if source == "prompt.tool_recommendations"
    ] == [sections[0]], "压缩后的当前展示必须保留唯一、最新的Tier-2"
    persisted = agent.conversation_store.threads.require(thread.thread_id)
    assert list(persisted.loaded_tool_names) == ["scripts", "workflows"]
    assert persisted.metadata["pinned_tool_names"] == ["scripts", "workflows"]

    third = _run_turn(agent, provider, thread, "job48", monitor)
    assert len(third) == 1
    _assert_stable_requests([*first, *second, *compact_calls, *third], first[0])
    tail = _tier2_sections(third[-1])
    assert tail
    _assert_job48_tier2(tail[-1])
    assert "job48" not in first[0]["layout"].canonical_user_turn
    assert alert != monitor != plain

    # 前3轮wire完全同序；本轮预载的skill_search保持粘性，搜索只向wire末尾追加package_build。
    names_before = [row["name"] for row in first[0]["tools"]]
    assert "package_build" not in names_before
    discovery = _run_turn(
        agent,
        provider,
        thread,
        "discover",
        "继续核对可用工具",
        actions=(("tool_search", {"names": ["package_build"]}),),
    )
    assert len(discovery) == 2
    _assert_stable_requests(discovery[:1], first[0])
    assert [row["name"] for row in discovery[-1]["tools"]] == [*names_before, "package_build"]
    assert discovery[-1]["tools"][:-1] == first[0]["tools"]
    assert tuple(agent.conversation_store.threads.require(thread.thread_id).loaded_tool_names) == (
        "scripts",
        "workflows",
        "skill_search",
        "package_build",
    )
    after_discovery = _run_turn(agent, provider, thread, "persist-discovery", plain)
    assert len(after_discovery) == 1
    assert after_discovery[0]["tools"] == discovery[-1]["tools"]
    assert after_discovery[0]["layout"].stable_prefix == first[0]["layout"].stable_prefix

    # 反证只改已捕获的副本，不patch生产代码：断言必须识别单字节漂移和工具重排。
    from dataclasses import replace

    changed_prefix = {
        **third[-1],
        "layout": replace(
            third[-1]["layout"], stable_prefix=third[-1]["layout"].stable_prefix + "x"
        ),
    }
    reordered = {**third[-1], "tools": list(reversed(third[-1]["tools"]))}
    for counterexample in (changed_prefix, reordered):
        with pytest.raises(AssertionError):
            _assert_stable_requests([counterexample], first[0])
    for invalid_tail in (
        tail[-1].replace("⏰ job48", "missing-alias"),
        tail[-1].replace("保留原始证据引用", "missing-pitfall"),
    ):
        with pytest.raises(AssertionError):
            _assert_job48_tier2(invalid_tail)
    print(
        "GATE1_MEASUREMENTS "
        + json.dumps(
            {
                "profile": profile,
                "counts": counts,
                **measurements,
                "provider_calls": len(provider.calls),
                "compactions": len(compact_calls),
                "falsification_rejections": 4,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
