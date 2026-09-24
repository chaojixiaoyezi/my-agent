"""媒体压缩策略片 B：输入模态声明、结构化视觉探针、B/A 决策与准入、随图摘要请求路径、typed 失败后同代次回落。"""
from __future__ import annotations

import base64
import json
import struct
import zlib
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.context_compactor import RuntimeCompactPolicy
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.errors import (
    ProviderConfigurationError,
    ProviderRecoverableError,
    ProviderRequestRejectedError,
)
from agent_py_agent.agent.backends.vision_capability import (
    VISION_INCONCLUSIVE,
    VISION_SUPPORTED,
    VISION_UNAVAILABLE,
    VISION_UNSUPPORTED,
    reset_vision_capability_cache,
    resolve_vision_capability,
)
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import compact_request_budget as budget_module
from agent_py_agent.agent.conversation.compact import (
    ConversationCompactOptions,
    prepare_conversation_context,
)
from agent_py_agent.agent.conversation.compact_checkpoint import committed_compact_checkpoint_chain
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_media_policy import (
    COMPACT_VISION_SUMMARY_FAILED,
    CompactMediaDecision,
    MediaArchiveFacts,
    resolve_compact_media_policy,
    resolve_vision_candidate,
    vision_summary_admission,
)
from agent_py_agent.agent.conversation.compact_projection import (
    ConversationCompactProjection,
    ConversationCompactSource,
)
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactProviderSurface,
)
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.compact_summary_view import (
    AppliedCompactContext,
    resolve_compact_summary_view,
)
from agent_py_agent.agent.conversation.native_history import (
    CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
    canonical_native_messages_envelope,
)
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    model_profiles_path,
    read_model_profiles,
    selected_model_config,
)
from agent_py_agent.agent.settings.model_provider_schema import (
    ModelProfileError,
    validate_input_modalities,
)
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_model_profiles import Host, add

_COLORS = {(255, 0, 0): "red", (0, 160, 0): "green", (0, 0, 255): "blue", (255, 220, 0): "yellow"}
SHA = "b" * 64
LOCAL_IMAGE = {"type": "image", "source": {"type": "local_file", "path": "/owner/attachments/" + SHA, "sha256": SHA,
                                            "media_type": "image/png", "size_bytes": 321, "name": "chart.png"}}


@pytest.fixture(autouse=True)
def _fresh_cache():
    reset_vision_capability_cache()
    yield
    reset_vision_capability_cache()


# 函数用途: 从探针 PNG 的 base64 解回首像素颜色，让假后端"真的看图"而不是从提示词猜答案。
def _decode_color(data: str) -> str:
    png = base64.b64decode(data)
    start = png.index(b"IDAT")
    length = struct.unpack(">I", png[start - 4:start])[0]
    raw = zlib.decompress(png[start + 4:start + 4 + length])
    return _COLORS[tuple(raw[1:4])]


# 类用途: 假后端，只记录请求、按图回答；answer 控制答对/答错/不调用，raise_exc 控制供应商异常。
class _Backend:
    def __init__(self, *, native=True, answer="match", raise_exc=None, model="vision-test"):
        self.native, self.answer, self.raise_exc, self.model = native, answer, raise_exc, model
        self.api_base = "https://example.invalid/v1"
        self.calls = 0
        self.requests = []

    def _tool_endpoint(self):
        return self.api_base + "/messages"

    def probe_tool_capability(self):
        return ProviderToolCapability(provider="fake", endpoint=self._tool_endpoint(), model=self.model, stream=False,
                                      native_supported=self.native, evidence="test")

    def generate(self, prompt, *, tools=None, tool_choice=None, messages=None, **_kwargs):
        self.calls += 1
        self.requests.append({"prompt": prompt, "tools": tools, "messages": messages})
        if self.raise_exc is not None:
            raise self.raise_exc
        color = _decode_color(messages[0]["content"][1]["source"]["data"])
        if self.answer == "none":
            return ModelResponse(text="看起来是彩色的", backend="fake")
        reply = color if self.answer == "match" else ("blue" if color != "blue" else "red")
        return ModelResponse(text="", backend="fake",
                             tool_use_blocks=[{"id": "t1", "name": "my_agent_vision_probe", "input": {"color": reply}}])


def test_probe_supported_is_cached_per_endpoint_and_model():
    backend = _Backend()
    first = resolve_vision_capability(backend)
    assert first.status == VISION_SUPPORTED and first.fact_source == "probe_supported" and first.supported
    assert resolve_vision_capability(backend) == first and backend.calls == 1, "阳性只探一次"
    request = backend.requests[0]
    assert request["tools"][0]["name"] == "my_agent_vision_probe"
    assert request["messages"][0]["content"][1]["source"]["media_type"] == "image/png"
    other = _Backend(model="another-model")
    assert resolve_vision_capability(other).supported and other.calls == 1, "不同模型是不同键"


def test_typed_media_rejection_caches_unsupported_and_transient_errors_are_not_cached():
    rejected = _Backend(raise_exc=ProviderRequestRejectedError("image input not supported", status_code=400))
    result = resolve_vision_capability(rejected)
    assert result.status == VISION_UNSUPPORTED and result.fact_source == "probe_unsupported"
    assert resolve_vision_capability(rejected) == result and rejected.calls == 1
    flaky = _Backend(raise_exc=ProviderRecoverableError("network"), model="flaky-model")
    with pytest.raises(ProviderRecoverableError):
        resolve_vision_capability(flaky)
    flaky.raise_exc = None
    assert resolve_vision_capability(flaky).supported and flaky.calls == 2, "瞬时错误不缓存，恢复后重探"


@pytest.mark.parametrize("answer, evidence", [("wrong", "probe_wrong_color"), ("none", "probe_no_tool_call")])
def test_wrong_or_missing_tool_answer_is_inconclusive_and_not_cached(answer, evidence):
    backend = _Backend(answer=answer)
    result = resolve_vision_capability(backend)
    assert result.status == VISION_INCONCLUSIVE and result.fact_source == "probe_inconclusive" and result.evidence == evidence
    assert backend.calls == 2, "有界重试两次"
    resolve_vision_capability(backend)
    assert backend.calls == 4, "不确定结果不缓存"


def test_probe_requires_native_tool_support():
    backend = _Backend(native=False)
    result = resolve_vision_capability(backend)
    assert result.status == VISION_UNAVAILABLE and result.fact_source == "probe_unavailable" and backend.calls == 0
    resolve_vision_capability(backend)
    assert backend.calls == 0


def _agent(backend=None, **config):
    values = {"compact_media_policy": "auto", "model_input_modalities": []}
    values.update(config)
    return SimpleNamespace(config=SimpleNamespace(**values), backend=backend)


# 函数用途: 模拟候选构造里"范围内确有媒体块"后的完整解析：骨架决定 + 视觉事实。
def _decide(agent, **kwargs):
    return resolve_vision_candidate(agent, resolve_compact_media_policy(agent, **kwargs))


def test_skeleton_decision_never_probes_and_text_only_ranges_stay_probe_free():
    backend = _Backend()
    skeleton = resolve_compact_media_policy(_agent(backend))
    assert skeleton == CompactMediaDecision("archived_refs", "vision_fact_pending", vision_candidate=True)
    assert backend.calls == 0, "骨架决定不发请求；纯文字范围不会走到 resolve_vision_candidate"
    resolved = resolve_vision_candidate(_agent(backend), skeleton)
    assert resolved == CompactMediaDecision("vision_summary", "probe_supported") and backend.calls == 1
    assert resolve_vision_candidate(_agent(backend), resolved) is resolved, "已解析的决定不再重复解析"


def test_policy_prefers_declared_modalities_and_only_probes_when_unknown():
    backend = _Backend()
    assert _decide(_agent(backend, model_input_modalities=["image", "text"])) == CompactMediaDecision("vision_summary", "declared")
    assert _decide(_agent(backend, model_input_modalities=["text"])) == CompactMediaDecision("archived_refs", "declared")
    assert backend.calls == 0, "有声明就不探针"
    assert _decide(_agent(backend)) == CompactMediaDecision("vision_summary", "probe_supported")
    unsupported = _Backend(raise_exc=ProviderRequestRejectedError("no images", status_code=400), model="no-vision-model")
    assert _decide(_agent(unsupported)) == CompactMediaDecision("archived_refs", "probe_unsupported")
    flaky = _Backend(raise_exc=ProviderConfigurationError("quota"), model="quota-model")
    assert _decide(_agent(flaky)) == CompactMediaDecision("archived_refs", "probe_unavailable")
    assert _decide(_agent(None)) == CompactMediaDecision("archived_refs", "vision_fact_unavailable")


def test_forced_recovery_and_failed_generation_fall_back_before_any_probe():
    backend = _Backend()
    agent = _agent(backend, model_input_modalities=["image"])
    assert _decide(agent, forced=True) == CompactMediaDecision("archived_refs", "policy_forced", "forced_recovery")
    failed = SimpleNamespace(compact_generation=3, compact_vision_failed_generation=3)
    assert _decide(agent, thread=failed) == CompactMediaDecision("archived_refs", "policy_forced", COMPACT_VISION_SUMMARY_FAILED)
    advanced = SimpleNamespace(compact_generation=4, compact_vision_failed_generation=3)
    assert _decide(agent, thread=advanced).policy == "vision_summary", "代次推进后失效"
    assert _decide(_agent(backend, compact_media_policy="archived_refs"), forced=False) == \
        CompactMediaDecision("archived_refs", "policy_forced")
    assert backend.calls == 0


def test_vision_summary_admission_gates_video_bytes_and_budget():
    candidate = CompactMediaDecision("vision_summary", "declared")
    common = dict(media_max_bytes=1000, reserve_tokens=100, text_tokens=500, budget=800)
    assert vision_summary_admission(candidate, MediaArchiveFacts(1, (SHA,), 321, 0), **common) == candidate
    assert vision_summary_admission(candidate, MediaArchiveFacts(1, (SHA,), 321, 1), **common).reason == "video_present"
    assert vision_summary_admission(candidate, MediaArchiveFacts(1, (SHA,), 1001, 0), **common).reason == "media_bytes_exceeded"
    assert vision_summary_admission(candidate, MediaArchiveFacts(4, (SHA,), 321, 0), **common).reason == "summary_budget_exceeded"
    fallback = vision_summary_admission(candidate, MediaArchiveFacts(4, (SHA,), 321, 0), **common)
    assert fallback.policy == "archived_refs" and fallback.fact_source == "declared", "回落保留事实来源"
    plain = CompactMediaDecision("archived_refs", "declared")
    assert vision_summary_admission(plain, MediaArchiveFacts(1, (SHA,), 321, 1), **common) is plain


def test_input_modalities_are_validated_persisted_and_reach_runtime_config(tmp_path):
    assert validate_input_modalities("image, text ，image") == ["image", "text"]
    assert validate_input_modalities(["video"]) == ["video"] and validate_input_modalities("") == []
    for bad in ("Image", "long-form", ["ok", 2], {"x": 1}, ",".join(f"m{i}" for i in range(9))):
        with pytest.raises(ModelProfileError):
            validate_input_modalities(bad)
    host = Host(tmp_path)
    tagged, _ = add(host, input_modalities="image, text")
    plain, _ = add(host, model_name="MiniMax-M3")
    stored = read_model_profiles(model_profiles_path(host.home_paths))["profiles"]
    assert stored[tagged]["input_modalities"] == ["image", "text"] and "input_modalities" not in stored[plain]
    rows = {row["id"]: row for row in execute_model_profile_operation(host, "list", {})["profiles"]}
    assert rows[tagged]["input_modalities"] == ["image", "text"]
    execute_model_profile_operation(host, "set_default", {"profile_id": tagged})
    assert selected_model_config(host).model_input_modalities == ["image", "text"]
    execute_model_profile_operation(host, "set_default", {"profile_id": plain})
    assert selected_model_config(host).model_input_modalities == [], "未声明 → 空列表 → 运行时按未知探针"
    with pytest.raises(ModelProfileError):
        decision(host, input_modalities="image")


@pytest.fixture
def vision_case(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "now": 1.0})
    backend = _Backend()
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_compact_dir=tmp_path / "compact"), backend=backend,
        config=SimpleNamespace(compact_media_policy="auto", model_input_modalities=["image"],
                               input_media_max_bytes=10_000, input_media_token_reserve=50),
        conversation_store=store,
    )
    policy = RuntimeCompactPolicy(
        context_window_tokens=1_000, trigger_percent=90, trigger_tokens=900,
        recovery_target_percent=60, recovery_target_tokens=600,
        allow_persistent_apply=True, recent_tail_max_turns=0, recent_tail_tokens=0,
        failure_threshold=3, failure_cooldown_seconds=300.0,
    )
    calls = []
    behaviour = {"raise": None}

    def bounded(request, *, message_source=None, vision_summary=False, media_reserve_tokens=0, **_kwargs):
        calls.append({"vision_summary": vision_summary, "media_reserve_tokens": media_reserve_tokens,
                      "messages": list(message_source) if message_source is not None else None})
        if behaviour["raise"] is not None:
            raise behaviour["raise"]
        return SimpleNamespace(text="摘要正文", tool_use_blocks=[], truncated=False)

    monkeypatch.setattr(budget_module, "generate_bounded_compact_response", bounded)
    monkeypatch.setattr(budget_module, "compact_summary_budget", lambda _agent: 10_000)
    monkeypatch.setattr(budget_module, "compact_request_tokens", lambda _request, _source=None: 100)
    return SimpleNamespace(store=store, thread=thread, agent=agent, policy=policy, calls=calls, behaviour=behaviour)


def _append(case, role, content, turn_id, *, native=None):
    metadata = {"conversation_request_id": turn_id}
    if native is not None:
        metadata[CANONICAL_NATIVE_MESSAGES_METADATA_KEY] = canonical_native_messages_envelope(native)
    return case.store.messages.append({"thread_id": case.thread.thread_id, "role": role, "content": content,
                                       "metadata": metadata, "now": 10.0})


def _rows(case):
    native = [{"role": "user", "content": [{"type": "text", "text": "看附件"}, LOCAL_IMAGE]},
              {"role": "assistant", "content": [{"type": "text", "text": "图里是红色"}]}]
    return [
        _append(case, "user", "旧问题", "turn-text"), _append(case, "assistant", "旧回答", "turn-text"),
        _append(case, "user", "看附件", "turn-media"), _append(case, "assistant", "图里是红色", "turn-media", native=native),
        _append(case, "user", "下一个问题", "turn-later"), _append(case, "assistant", "下一个回答", "turn-later"),
    ]


def _compact(case, rows):
    thread = case.store.threads.require(case.thread.thread_id)
    view = resolve_compact_summary_view(case.agent, thread, THREAD_COMPACT_SCOPE)
    source = ConversationCompactSource(thread, tuple(rows), case.policy, {},
                                       AppliedCompactContext(thread.thread_id, THREAD_COMPACT_SCOPE, view))
    return prepare_conversation_context(
        case.agent, case.store, source.thread,
        options=ConversationCompactOptions(
            source=source, force=False, exclude_request_id="active-turn",
            provider_surface=ConversationCompactProviderSurface("稳定前缀", None, "摘要系统"),
            request_projector=lambda view: ConversationCompactProjection(
                projected_tokens=300 if view.is_candidate else 950,
                material={"candidate": view.is_candidate, "messages": view.messages}),
        ),
    )


def test_vision_summary_keeps_image_blocks_in_the_single_summary_request(vision_case):
    case = vision_case
    result = _compact(case, _rows(case))
    assert result.compacted
    (call,) = case.calls
    assert call["vision_summary"] is True and call["media_reserve_tokens"] == 50
    text = json.dumps(call["messages"], ensure_ascii=False)
    assert "local_file" in text and SHA in text and "附件引用" not in text, "B 路径保留图块给供应商适配层展开"
    checkpoint = committed_compact_checkpoint_chain(case.agent, result.thread)[-1]
    assert checkpoint["media_policy"] == "vision_summary" and checkpoint["media_fact_source"] == "declared"
    assert checkpoint["media_blocks_summarized"] == 1 and checkpoint["media_blocks_archived"] == 0
    assert checkpoint["media_refs"] == [SHA] and "media_policy_reason" not in checkpoint
    assert case.agent.backend.calls == 0, "已声明模态，不发探针"


def test_vision_summary_typed_failure_marks_generation_and_next_compaction_uses_archived_refs(vision_case):
    case = vision_case
    rows = _rows(case)
    case.behaviour["raise"] = ConversationCompactError("随图摘要请求失败：ProviderContextWindowError", code=COMPACT_VISION_SUMMARY_FAILED)
    with pytest.raises(ConversationCompactError) as failure:
        _compact(case, rows)
    assert failure.value.code == COMPACT_VISION_SUMMARY_FAILED
    thread = case.store.threads.require(case.thread.thread_id)
    assert thread.compact_vision_failed_generation == thread.compact_generation == 0
    assert thread.compact_consecutive_failures == 0 and thread.compact_failure_code == "", "typed 随图失败不进熔断"
    case.behaviour["raise"] = None
    result = _compact(case, rows)
    assert result.compacted
    second = case.calls[-1]
    assert second["vision_summary"] is False and "附件引用" in json.dumps(second["messages"], ensure_ascii=False)
    checkpoint = committed_compact_checkpoint_chain(case.agent, result.thread)[-1]
    assert checkpoint["media_policy"] == "archived_refs" and checkpoint["media_policy_reason"] == COMPACT_VISION_SUMMARY_FAILED
    assert checkpoint["media_blocks_archived"] == 1 and checkpoint["media_blocks_summarized"] == 0
    assert result.thread.compact_vision_failed_generation == -1, "压缩成功后重置"


def test_budget_or_bytes_gate_falls_back_to_archived_refs_with_reason(vision_case, monkeypatch):
    case = vision_case
    monkeypatch.setattr(budget_module, "compact_summary_budget", lambda _agent: 120)
    result = _compact(case, _rows(case))
    (call,) = case.calls
    assert call["vision_summary"] is False and "附件引用" in json.dumps(call["messages"], ensure_ascii=False)
    checkpoint = committed_compact_checkpoint_chain(case.agent, result.thread)[-1]
    assert checkpoint["media_policy"] == "archived_refs" and checkpoint["media_policy_reason"] == "summary_budget_exceeded"
    assert checkpoint["media_fact_source"] == "declared" and checkpoint["media_blocks_archived"] == 1
