# LLM: 前缀优化不得绕过主线旧历史释放或完整请求预算；只用临时home和离线原函数，不调用模型、服务或真实资料。
# 模块用途: 锁住解绑前容量下界、校准跳点、工具来源原样恢复和摘要精确门仍生效。
import weakref
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import compact_request_recovery as recovery
from agent_py_agent.agent.agent_core import compact_transcript_prefix as prefixes
from agent_py_agent.agent.backends.errors import ProviderTransientError
from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn
from agent_py_agent.agent.conversation import compact
from agent_py_agent.agent.conversation import compact_request_budget as budgets
from agent_py_agent.agent.conversation.auxiliary_model_call import AuxiliaryModelCallRequest
from agent_py_agent.agent.conversation.compact_calibration import CompactRequestCalibration
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_media_policy import (
    CompactMediaDecision,
    media_archive_facts,
)
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactModelSurface,
    ConversationCompactProviderSurface,
    conversation_compact_provider_prompt,
)
from agent_py_agent.agent.conversation.models import MessageLogEntry
from agent_py_agent.agent.conversation.native_history import (
    CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
    canonical_native_messages_envelope,
    provider_history_messages_from_rows,
)
from agent_py_agent.agent.settings import AgentConfig


# LLM: helper自身声明隔离路径，不依赖pytest外默认home；只建立配置对象，不创建Agent或访问任何owner存储。
# 函数用途: 给纯预算和接线测试一个明确临时home的宿主。
@pytest.fixture
def isolated_agent(tmp_path, monkeypatch):
    home = tmp_path / "my-agent-home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    return SimpleNamespace(config=AgentConfig(my_agent_home=str(home)))


# LLM: 使用真实历史Adapter与typed前缀；正文很小，测试不持有外部历史或制造另一份覆盖权威。
# 函数用途: 构造可重放的单行前缀供容量和生命周期断言使用。
def _prefix(*, joint_end=0):
    rows = (MessageLogEntry("m1", "thread-local", "user", "原文"),)
    return prefixes.TranscriptInputPrefix(
        provider_history_messages_from_rows(rows), 0, rows, ("m1",), joint_end
    )


@pytest.mark.parametrize(
    "scope,raw,observed,provider,budget,exceeded",
    [
        (None, 99, 0, 0, 100, False),
        (None, 100, 0, 0, 100, False),
        (None, 101, 0, 0, 100, True),
        ("current_run", 9_999, 10_000, 100, 100, False),
        ("current_run", 10_000, 10_000, 100, 100, False),
        ("current_run", 10_001, 10_000, 100, 100, True),
        ("current_run", 9_999, 10_000, 100, 99, True),
        ("current_run", 9_999, 10_000, 100, 200, False),
        ("current_run", 9_999, 10_000, 9_000, 1_000, True),
        ("owner_ratio", 9_999, 10_000, 100, 4_999, True),
        ("owner_ratio", 9_999, 10_000, 100, 5_000, False),
        ("durable_thread", 9_999, 10_000, 100, 4_999, True),
        ("durable_thread", 9_999, 10_000, 100, 5_000, False),
        ("durable_thread", 10_001, 10_000, 100, 5_001, False),
        ("current_run", 101, 0, 100, 100, True),
        ("current_run", 101, 10_000, 0, 100, True),
    ],
)
def test_lower_bound_uses_original_calibration_across_observed_boundary(
    isolated_agent,
    monkeypatch,
    scope,
    raw,
    observed,
    provider,
    budget,
    exceeded,
):
    calibration = (
        CompactRequestCalibration(observed, provider, calibration_scope=scope) if scope else None
    )
    monkeypatch.setattr(budgets, "compact_request_tokens", lambda *_: raw)
    monkeypatch.setattr(budgets, "compact_cache_surface_budget", lambda *_: budget)
    assert (
        prefixes.transcript_prefix_exceeds_capacity(
            isolated_agent,
            ConversationCompactProviderSurface("stable", None, "system"),
            _prefix(),
            calibration,
        )
        is exceeded
    )


def test_admissible_lower_bound_does_not_replace_complete_request_gate(isolated_agent, monkeypatch):
    surface, prefix = ConversationCompactProviderSurface("stable", (), "system"), _prefix()
    monkeypatch.setattr(budgets, "compact_cache_surface_budget", lambda *_: 200)
    monkeypatch.setattr(
        budgets,
        "generate_auxiliary_model_response",
        lambda *_: pytest.fail("纯容量门不得发模型请求"),
    )
    assert not prefixes.transcript_prefix_exceeds_capacity(isolated_agent, surface, prefix, None)
    source = prefix(prefix.rows)
    request = AuxiliaryModelCallRequest(
        isolated_agent,
        conversation_compact_provider_prompt(surface, "新增摘要指令" * 4_000),
        tools=[],
        system_instruction=surface.system_instruction,
    )
    with pytest.raises(ConversationCompactError) as failure:
        compact._require_transcript_prefix_capacity(request, source, None)
    assert failure.value.code == "COMPACT_PREFIX_TOO_LARGE"


def test_capacity_lower_bound_excludes_uncoverable_runtime_facts(isolated_agent, monkeypatch):
    prefix = _prefix()
    protected_tail = AnthropicMessageAdapter().to_provider_messages(
        [
            RuntimeFactsTurn(text="当前受保护运行事实" * 2_000, source="conversation.runtime"),
        ]
    )
    prefix = prefixes.TranscriptInputPrefix(
        prefix.messages + tuple(protected_tail),
        0,
        prefix.rows,
        prefix.source_ids,
    )
    surface = ConversationCompactProviderSurface("stable", None, "system")
    monkeypatch.setattr(budgets, "compact_cache_surface_budget", lambda *_: 200)
    request = AuxiliaryModelCallRequest(
        isolated_agent,
        conversation_compact_provider_prompt(surface, ""),
        system_instruction="system",
    )
    from agent_py_agent.agent.conversation.compact_message_source import CompactMessageSource

    assert (
        budgets.compact_request_tokens(request, CompactMessageSource(lambda: iter(prefix.messages)))
        > 200
    )
    assert not prefixes.transcript_prefix_exceeds_capacity(isolated_agent, surface, prefix, None)
    assert list(prefix(prefix.rows)) == list(prefix.messages[:1])


def test_declared_unsupported_media_keeps_original_media_branch(isolated_agent, monkeypatch):
    from agent_py_agent.tests.test_media_compact_preflight import IMAGE

    native = [{"role": "user", "content": [IMAGE]}]
    row = MessageLogEntry(
        "m1",
        "thread-local",
        "user",
        "看图",
        metadata={
            CANONICAL_NATIVE_MESSAGES_METADATA_KEY: canonical_native_messages_envelope(native),
        },
    )
    rows = (row,)
    prefix = prefixes.TranscriptInputPrefix(
        provider_history_messages_from_rows(rows), 0, rows, ("m1",)
    )
    isolated_agent.config.model_input_modalities = ["text"]
    monkeypatch.setattr(
        budgets, "compact_request_tokens", lambda *_: pytest.fail("不能用容量原因掩盖明确媒体拒绝")
    )
    decision = prefixes.declared_transcript_prefix_media_decision(isolated_agent, prefix)
    assert decision.reason == "COMPACT_PREFIX_MEDIA_UNSUPPORTED"
    assert decision.policy == "archived_refs" and decision.fact_source == "declared"


@pytest.mark.parametrize(
    "candidate,declared,policy,adopted",
    [
        ("image", ["text", "image"], "archived_refs", False),
        ("video", ["text", "image"], "archived_refs", True),
        ("both", ["text", "image"], "archived_refs", True),
        ("none", ["text", "image"], "archived_refs", False),
        ("video", ["text", "image"], "off", False),
        ("video", [], "archived_refs", False),
        ("video", ["text", "image", "video"], "archived_refs", False),
    ],
)
def test_carried_media_decision_is_rechecked_for_exact_candidate(
    isolated_agent, candidate, declared, policy, adopted
):
    from agent_py_agent.tests.test_media_compact_preflight import IMAGE

    image = dict(IMAGE)
    video = {
        "type": "video",
        "source": {**IMAGE["source"], "media_type": "video/mp4", "sha256": "e" * 64},
    }
    rows = tuple(
        MessageLogEntry(
            f"m{index}",
            "thread-local",
            "user",
            "媒体",
            metadata={
                CANONICAL_NATIVE_MESSAGES_METADATA_KEY: canonical_native_messages_envelope(
                    [
                        {"role": "user", "content": [block]},
                    ]
                ),
            },
        )
        for index, block in enumerate((image, video))
    )
    prefix = prefixes.TranscriptInputPrefix(
        provider_history_messages_from_rows(rows), 0, rows, ("m0", "m1")
    )
    isolated_agent.config.model_input_modalities = ["text", "image"]
    carried = prefixes.declared_transcript_prefix_media_decision(isolated_agent, prefix)
    assert carried.reason == "COMPACT_PREFIX_MEDIA_UNSUPPORTED"
    surface = ConversationCompactProviderSurface("stable", None, "system", media_decision=carried)
    isolated_agent.config.model_input_modalities = declared
    selected = {"image": rows[:1], "video": rows[1:], "both": rows, "none": ()}[candidate]
    original = CompactMediaDecision(policy, "vision_fact_pending", vision_candidate=True)
    result = compact._candidate_prefix_media_decision(
        isolated_agent, surface, selected, original, media_archive_facts(selected)
    )
    assert result is (carried if adopted else original)


@pytest.mark.parametrize(
    "error",
    [
        InterruptedError("取消"),
        ProviderTransientError("离线网络故障"),
        ConversationCompactError("来源变化", code="COMPACT_SOURCE_CHANGED"),
        ValueError("媒体事实无效"),
    ],
)
def test_capacity_probe_does_not_convert_other_failures(isolated_agent, monkeypatch, error):
    def fail(*_args):
        raise error

    monkeypatch.setattr(budgets, "compact_request_tokens", fail)
    with pytest.raises(type(error)) as failure:
        prefixes.transcript_prefix_exceeds_capacity(
            isolated_agent,
            ConversationCompactProviderSurface("stable", None, ""),
            _prefix(),
            None,
        )
    assert failure.value is error


@pytest.mark.parametrize("entry", ["recovery", "preflight"])
@pytest.mark.parametrize("exceeded", [False, True])
def test_unusable_prefix_is_released_before_surface_publication(
    isolated_agent, monkeypatch, entry, exceeded
):
    original = ConversationCompactProviderSurface("stable", None, "system")
    refs = []

    def capture(*_args):
        value = _prefix()
        refs.append(weakref.ref(value))
        return value

    monkeypatch.setattr(prefixes, "transcript_prefix_exceeds_capacity", lambda *_: exceeded)
    if entry == "recovery":
        monkeypatch.setattr(recovery, "_summary_surface", lambda *_: original)
        monkeypatch.setattr(prefixes, "capture_transcript_prefix", capture)
        tool_source = object()
        host = SimpleNamespace(
            agent=isolated_agent,
            render_params=object(),
            source=SimpleNamespace(messages=("row",)),
            calibration=None,
        )
        result, selected = recovery._transcript_summary_surface(host, object(), tool_source)
        assert selected is (tool_source if exceeded else None), "拒用优化必须恢复完整原工具来源"
    else:
        monkeypatch.setattr(prefixes, "capture_preflight_prefix", capture)
        monkeypatch.setattr(
            compact,
            "prepare_conversation_compact_provider_surface",
            lambda *_args, **_kwargs: original,
        )
        request = SimpleNamespace(
            provider_surface=None,
            model_surface=ConversationCompactModelSurface(),
            thread=SimpleNamespace(thread_id="thread-local", loaded_tool_names=()),
            agent=isolated_agent,
            run_id="r",
            calibration=None,
        )
        result = compact._resolved_provider_surface(request)
    assert (result is original) is exceeded
    assert (result.transcript_prefix is None) is exceeded
    assert (refs[0]() is None) is exceeded, "原调用栈不能额外强留已拒用前缀"


@pytest.mark.parametrize("host", ["gateway", "child", "background"])
def test_unsupported_media_does_not_keep_unusable_full_history(tmp_path, monkeypatch, host):
    from agent_py_agent.agent.conversation import compact_media_policy
    from agent_py_agent.agent.conversation.compact_checkpoint import (
        committed_compact_checkpoint_chain,
    )
    from agent_py_agent.agent.conversation.input_media import (
        import_input_media,
        input_media_blocks,
        input_media_root,
    )
    from agent_py_agent.tests import test_host_summary_phase_lifetime as lifetime
    from agent_py_agent.tests.test_media_compact_preflight import PNG

    original_host, original_seed = lifetime._HOSTS[host], lifetime._seed_history
    image = []

    def media_host(path):
        agent, thread_id, run = original_host(path)
        agent.config.model_input_modalities = ["text"]
        agent.config.compact_media_policy = "auto"
        png = path / "lifetime.png"
        png.write_bytes(PNG)
        image.extend(input_media_blocks([import_input_media(png, input_media_root(agent))]))
        return agent, thread_id, run

    def seed(store, thread_id):
        ids = original_seed(store, thread_id)
        native = [
            {"role": "user", "content": image},
            {"role": "assistant", "content": "历史图已记录"},
        ]
        row = store.messages.append(
            {
                "thread_id": thread_id,
                "role": "assistant",
                "content": "历史图已记录",
                "metadata": {
                    "conversation_request_id": "prior-media",
                    CANONICAL_NATIVE_MESSAGES_METADATA_KEY: canonical_native_messages_envelope(
                        native
                    ),
                },
            }
        )
        return ids + [row.message_id]

    monkeypatch.setitem(lifetime._HOSTS, host, media_host)
    monkeypatch.setattr(lifetime, "_seed_history", seed)
    monkeypatch.setattr(
        compact_media_policy,
        "vision_capability_fact",
        lambda *_: pytest.fail("原声明决定生效前不得另发视觉探针"),
    )
    agent, thread_id, ids, ok, facts = lifetime._run_host(tmp_path, monkeypatch, host)
    assert facts["released"] is True
    body_chars = lifetime.COUNT * lifetime.BODY_SIZE
    limit = body_chars * 3 // (8 if host == "background" else 4)
    assert facts["entry_bytes"] < limit, (host, facts)
    thread = agent.conversation_store.threads.require(thread_id)
    chain = committed_compact_checkpoint_chain(agent, thread)
    assert ok and len(chain) == 1 and chain[0]["source_message_ids"] == ids
    assert chain[0]["media_policy_reason"] == "COMPACT_PREFIX_MEDIA_UNSUPPORTED"
    assert chain[0]["media_blocks_archived"] == 1 and chain[0]["media_refs"]
    assert facts["business"] == 1 and facts["task_in_payload"]
