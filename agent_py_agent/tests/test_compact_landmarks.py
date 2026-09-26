"""压缩摘要末尾的原话备份：按 token 预算随窗口放大、带消息编号、放不下的保留头尾、省略的列编号，并在候选超目标时收缩。

背景（2026-09-26）：旧备份固定 6000 字、每条截开头 1200 字、按最新优先，5 条长需求里最早一条整条丢失且没有任何回查线索。
对照 Codex（最新优先 2 万 token、放不下的那条保留头尾）与 Hermes/OpenClaw（压缩后可按编号检索原文），本测试锁定新合同，
并锁定两条不变量：挑选时不让全部原文同时驻留内存；备份不能把压缩候选挤出目标或容量线。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation import ConversationStore, compact_request_budget
from agent_py_agent.agent.conversation.compact import (
    ConversationCompactOptions,
    _CandidateLimits,
    _CompactSummaryCall,
    _conversation_summary_instruction,
    _finish_summary,
    _fit_landmarks_to_target,
    _MeasuredSummary,
    _SummaryFinish,
    load_conversation_compact_source,
    prepare_conversation_context,
)
from agent_py_agent.agent.conversation.compact_checkpoint import committed_compact_checkpoint_chain
from agent_py_agent.agent.conversation.compact_landmarks import (
    LANDMARK_HEADING,
    LandmarkOptions,
    landmark_options,
    semantic_summary_text,
    summary_with_conversation_landmarks,
)
from agent_py_agent.agent.conversation.compact_projection import ConversationCompactProjection
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactProviderSurface,
)
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.models import MessageLogEntry
from agent_py_agent.agent.memory_archive import estimate_tokens


# 函数用途: 造一条用户消息行。
def _user(message_id: str, content: str) -> MessageLogEntry:
    return MessageLogEntry(message_id=message_id, thread_id="t", role="user", content=content)


# 函数用途: 取备份段里的某类行。
def _lines(text: str, prefix: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(prefix)]


def test_budget_scales_with_the_window_and_respects_the_config_cap():
    # 函数用途: 造一个只有窗口、配置和工具注册表的 agent。
    def agent(window: int, **config) -> SimpleNamespace:
        values = {"model_context_window_tokens": window, "model_context_window_explicit": True, **config}
        return SimpleNamespace(config=SimpleNamespace(**values), backend=None,
                               tools=SimpleNamespace(tools={"session_search": object()}))

    assert landmark_options(agent(262_144)) == LandmarkOptions(max_tokens=20_000, recall_hint=True)
    assert landmark_options(agent(128_000)).max_tokens == 12_800
    assert landmark_options(agent(2_000)).max_tokens == 500
    assert landmark_options(agent(262_144, compact_landmark_max_tokens=0)).max_tokens == 0
    assert landmark_options(agent(262_144, compact_recall_hint_enabled=False)).recall_hint is False
    # 没注册 session_search 的 agent 不提示回查，避免指向一个用不了的工具。
    bare = agent(262_144)
    bare.tools = SimpleNamespace(tools={})
    assert landmark_options(bare).recall_hint is False


def test_every_line_carries_its_message_id_and_the_recall_hint_is_optional():
    rows = [_user("msg-a", "第一条要求：金额保留两位小数。"),
            MessageLogEntry(message_id="msg-b", thread_id="t", role="assistant", content="好的。",
                            metadata={"assistant_part_id": "final"})]
    plain = summary_with_conversation_landmarks("摘要", "", rows, options=LandmarkOptions(max_tokens=2_000)).text
    assert '- user [msg-a]: "第一条要求：金额保留两位小数。"' in plain
    assert '- assistant_final [msg-b]: "好的。"' in plain
    assert "- recall:" not in plain and "omitted_user_messages" not in plain
    hinted = summary_with_conversation_landmarks("摘要", "", rows, options=LandmarkOptions(2_000, recall_hint=True)).text
    assert _lines(hinted, "- recall:") and "session_search" in hinted and 'message_id="<id>"' in hinted


def test_short_older_requests_are_kept_before_a_long_newest_one_is_clipped_head_and_tail():
    long = "开头规则：金额保留两位小数。" + "数据行。" * 3000 + "结尾规则：按城市拼音排序。"
    rows = [_user("u-old", "较早的要求：人名只写姓氏加某。"), _user("u-new", long)]
    result = summary_with_conversation_landmarks("", "", rows, options=LandmarkOptions(max_tokens=1_500))
    assert result.used_tokens <= 1_500
    # 短要求整条保留；最新的长消息用剩余预算保留开头和结尾，写明原长度与省略字数。
    assert '- user [u-old]: "较早的要求：人名只写姓氏加某。"' in result.text
    clipped = _lines(result.text, "- user [u-new clipped ")[0]
    assert clipped.startswith(f"- user [u-new clipped {len(long)} chars]: ")
    body = json.loads(clipped.split(": ", 1)[1])
    assert body.startswith("开头规则：金额保留两位小数。") and body.endswith("结尾规则：按城市拼音排序。")
    assert "chars omitted]…" in body and "omitted_user_messages" not in result.text


def test_omitted_requests_are_listed_by_id_newest_last_and_the_list_is_bounded():
    rows = [_user(f"u{index:02d}", f"要求-{index:02d}：" + "细节" * 200) for index in range(50)]
    result = summary_with_conversation_landmarks("", "", rows, options=LandmarkOptions(max_tokens=1_200))
    omitted = _lines(result.text, "- omitted_user_messages: ")[0]
    ids = omitted.split("[", 1)[1].split("]", 1)[0].split()
    kept = [line for line in _lines(result.text, "- user [") if "clipped" not in line]
    assert kept and "u49" in kept[-1] and "u00" not in result.text.split("- omitted_user_messages")[0]
    # 只列最近 30 个被省略的编号（时间顺序），并写明更早还有多少没列出。
    assert len(ids) == 30 and ids == sorted(ids) and omitted.endswith("not listed")
    assert omitted.startswith(f"- omitted_user_messages: {50 - len(_lines(result.text, '- user ['))} older")


def test_previous_generation_lines_and_omitted_ids_are_carried_in_both_formats():
    previous = "\n".join([
        "旧摘要", "", LANDMARK_HEADING, "- authority: x", "- purpose: y",
        '- user: "旧格式要求：日期写成 YYYY-MM-DD。"',
        '- user [msg-kept]: "新格式要求：零库存标红。"',
        "- omitted_user_messages: 2 older or oversized [msg-gone-1 msg-gone-2]",
    ])
    result = summary_with_conversation_landmarks("新摘要", previous, [_user("msg-new", "本轮要求：按城市排序。")],
                                                 options=LandmarkOptions(max_tokens=2_000)).text
    assert result.count(LANDMARK_HEADING) == 1 and semantic_summary_text(result) == "新摘要"
    assert '- user: "旧格式要求：日期写成 YYYY-MM-DD。"' in result
    assert '- user [msg-kept]: "新格式要求：零库存标红。"' in result and '- user [msg-new]: "本轮要求：按城市排序。"' in result
    assert "[msg-gone-1 msg-gone-2]" in result
    # 预算为 0 时不留原话，但编号全部转入省略行（旧格式无编号只计数），模型仍能按编号读回。
    empty = summary_with_conversation_landmarks("新摘要", previous, [_user("msg-new", "本轮要求。")],
                                                options=LandmarkOptions(max_tokens=0, recall_hint=True)).text
    assert not _lines(empty, "- user") and "- recall:" in empty
    assert _lines(empty, "- omitted_user_messages: ") == [
        "- omitted_user_messages: 5 older or oversized [msg-gone-1 msg-gone-2 msg-kept msg-new]; earliest 1 not listed"]


def test_omitted_count_accumulates_across_generations_and_only_the_section_itself_is_inherited():
    listed = [f"m{index:02d}" for index in range(10, 40)]
    previous = "\n".join([
        "旧摘要", "", LANDMARK_HEADING, "- authority: x", "- purpose: y", '- user [msg-kept]: "保留的要求。"',
        f"- omitted_user_messages: 40 older or oversized [{' '.join(listed)}]; earliest 10 not listed",
        "", "user: 机械回退附在备份段之后的原文", '- user: "模型摘要正文里的列表"',
        "- omitted_user_messages: 3 older or oversized [x1 x2 x3]",
    ])
    rows = [_user("msg-new", "本轮要求。")]
    kept = summary_with_conversation_landmarks("新摘要", previous, rows, options=LandmarkOptions(max_tokens=2_000)).text
    # 段后内容（机械回退附带的旧摘要、原文与其中的列表）不会被当成备份继承。
    assert "模型摘要正文里的列表" not in kept and "x1" not in kept and _lines(kept, '- user [msg-kept]: "保留的要求。"')
    assert _lines(kept, "- omitted_user_messages: ") == [
        f"- omitted_user_messages: 40 older or oversized [{' '.join(listed)}]; earliest 10 not listed"]
    # 预算为 0 时两条原话也转入省略：列表只留最近 30 个，没列出的数量逐代累加，总数不缩水。
    empty = summary_with_conversation_landmarks("新摘要", previous, rows, options=LandmarkOptions(max_tokens=0)).text
    shown = [*listed[2:], "msg-kept", "msg-new"]
    assert _lines(empty, "- omitted_user_messages: ") == [
        f"- omitted_user_messages: 42 older or oversized [{' '.join(shown)}]; earliest 12 not listed"]


def test_a_newline_heavy_request_just_over_budget_is_clipped_not_dropped():
    text = "HEAD-" + "a\n" * 3_000 + "-TAIL"
    result = summary_with_conversation_landmarks("", "", [_user("u-rows", text)], options=LandmarkOptions(max_tokens=2_900))
    # 整行（含换行转义）放不下、按原文估算却像放得下：按整行折算保留头尾，而不是整条丢掉。
    clipped = _lines(result.text, "- user [u-rows clipped ")
    assert clipped and result.used_tokens <= 2_900 and "omitted_user_messages" not in result.text
    body = json.loads(clipped[0].split(": ", 1)[1])
    assert body.startswith("HEAD-a\na") and body.endswith("a\n-TAIL")


def test_mechanical_fallback_keeps_its_transcript_when_the_previous_summary_has_landmarks():
    previous = "\n".join(["旧摘要：用户在整理三份需求。", "", LANDMARK_HEADING, "- authority: x", "- purpose: y",
                          '- user [msg-old]: "旧要求：金额保留两位小数。"'])
    agent = SimpleNamespace(config=SimpleNamespace(model_context_window_tokens=100_000, model_context_window_explicit=True),
                            backend=None, tools=SimpleNamespace(tools={}))
    rows = [_user("msg-new", "新要求：日期写成 YYYY-MM-DD。")]
    summary = _finish_summary(_SummaryFinish(agent, previous, {}, rows, rows, "", "", _CompactSummaryCall()), "")
    # 模型没给正文时走机械续接包：旧摘要只取语义部分，剥离旧备份时不会把续接包里的原文段一起切掉；全文只有一段备份。
    assert summary.startswith("[conversation-compact-mechanical-fallback]") and summary.count(LANDMARK_HEADING) == 1
    assert "- chronological_transcript:" in summary and "旧摘要：用户在整理三份需求。" in summary
    assert '- user [msg-old]: "旧要求：金额保留两位小数。"' in summary
    assert '- user [msg-new]: "新要求：日期写成 YYYY-MM-DD。"' in summary


class _CountingRows(list):
    """只记录按下标读取的次数；迭代照常流式返回（模拟磁盘快照行）。"""

    def __init__(self, rows):
        super().__init__(rows)
        self.indexed: list[int] = []

    def __getitem__(self, key):
        self.indexed.append(key)
        return super().__getitem__(key)


def test_only_selected_rows_are_read_back_for_rendering():
    rows = _CountingRows([_user(f"u{index}", f"要求-{index}：" + "细节" * 300) for index in range(40)])
    result = summary_with_conversation_landmarks("", "", rows, options=LandmarkOptions(max_tokens=2_000))
    rendered = _lines(result.text, "- user [")
    # 第一遍只流式迭代；第二遍只按下标重读被选中的行，不会把 40 条正文同时读进来。
    assert rendered and len(rows.indexed) == len(rendered) < len(rows)


def test_summary_instruction_asks_for_every_requirement_and_pending_requests():
    instruction = _conversation_summary_instruction({}, custom_instructions="", cache_safe=True)
    assert "'User requirements' section" in instruction and "'Pending user requests' section" in instruction
    assert "large pasted" in instruction


# 类用途: 记录收缩请求的原话备份重建替身。
class _Rebuild:
    def __init__(self, used: int, minimum: int = 0):
        self.used_tokens = used
        self.minimum = minimum
        self.budgets: list[int] = []

    def minimum_tokens(self) -> int:
        return self.minimum

    def render(self, budget: int) -> str:
        self.budgets.append(budget)
        return f"smaller-{budget}"


def test_fit_only_remeasures_when_shrinking_can_change_the_decision():
    request = _CandidateLimits(target=1_000, ceiling=1_500)
    remeasured: list[str] = []

    # 函数用途: 重新计量替身：按收缩后的备份预算线性给出新大小。
    def remeasure(summary: str) -> _MeasuredSummary:
        remeasured.append(summary)
        return _MeasuredSummary(summary, None, 900 + int(summary.rsplit("-", 1)[1]) // 10)

    under = _MeasuredSummary("s", None, 950)
    assert _fit_landmarks_to_target(request, under, [_Rebuild(300)], remeasure) is under
    # 超目标但去掉备份能达标：按超出量缩小备份后重算一次并采用更小的。
    rebuild = _Rebuild(300)
    fitted = _fit_landmarks_to_target(request, _MeasuredSummary("s", None, 1_200), [rebuild], remeasure)
    assert rebuild.budgets == [100] and fitted.summary == "smaller-100"
    # 会被拒但去掉备份能回到上限以下：同样收缩重算。
    rejected = _Rebuild(900)
    assert _fit_landmarks_to_target(request, _MeasuredSummary("s", None, 1_600), [rejected], remeasure).summary == "smaller-300"
    # 去掉整段备份也救不回来（超限来自别的内容）、超目标但缩到最小也达不到目标（最小段含标题与编号）、或没有备份段：都不重算。
    for tokens, used, minimum in ((2_000, 100, 0), (1_400, 100, 0), (1_200, 300, 150), (1_200, 0, 0)):
        count = len(remeasured)
        rebuild = _Rebuild(used, minimum)
        assert _fit_landmarks_to_target(request, _MeasuredSummary("s", None, tokens), [rebuild], remeasure).tokens == tokens
        assert len(remeasured) == count
    assert _fit_landmarks_to_target(request, _MeasuredSummary("s", None, 1_200), [], remeasure).tokens == 1_200


def test_real_compaction_shrinks_landmarks_to_the_target_with_one_extra_measurement(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversation")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "now": 1})
    for index in range(6):
        store.messages.append({"thread_id": thread.thread_id, "role": "user",
                               "content": f"第{index}份要求：" + "规则细节。" * 300})
        store.messages.append({"thread_id": thread.thread_id, "role": "assistant", "content": f"收到第{index}份。"})
    agent = SimpleNamespace(
        conversation_store=store, home_paths=SimpleNamespace(owner_compact_dir=tmp_path / "compact"),
        backend=SimpleNamespace(name="fake", model_name="fake", max_tokens=128),
        prompts=SimpleNamespace(build=lambda prompt, *_args, **_kwargs: prompt),
        config=SimpleNamespace(model_context_window_tokens=100_000, model_context_window_explicit=True),
    )
    monkeypatch.setattr(compact_request_budget, "generate_auxiliary_model_response",
                        lambda _request: ModelResponse(text="用户给了六份要求，尚未整理。", backend="fake"))
    source = load_conversation_compact_source(agent, store, thread, scope=THREAD_COMPACT_SCOPE)
    target = source.policy.recovery_target_tokens
    measured: list[tuple[str, int]] = []

    # 函数用途: 宿主投影替身：候选大小 = 目标附近的固定底座 + 摘要（含备份段）的 token。
    def project(view):
        tokens = target - 300 + estimate_tokens(view.summary) if view.is_candidate else source.policy.trigger_tokens + 1
        if view.is_candidate:
            measured.append((view.summary, tokens))
        return ConversationCompactProjection(tokens, object())

    result = prepare_conversation_context(agent, store, thread, options=ConversationCompactOptions(
        source=source, force=True, exclude_request_id="current-request", request_projector=project,
        provider_surface=ConversationCompactProviderSurface("稳定前缀", None, "摘要系统"),
    ))
    assert result.compacted and len(measured) == 2
    (full_summary, full_tokens), (small_summary, small_tokens) = measured
    assert full_tokens > target >= small_tokens
    committed = committed_compact_checkpoint_chain(agent, result.thread)[0]["summary"]
    assert committed == small_summary and len(_lines(small_summary, "- user [")) < len(_lines(full_summary, "- user ["))
    assert _lines(small_summary, "- omitted_user_messages: ")
