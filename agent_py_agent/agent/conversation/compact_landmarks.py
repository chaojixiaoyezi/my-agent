# LLM: 压缩摘要末尾的“原话备份”（Exact Conversation Landmarks）属于 conversation Compact。它是非权威的历史文本：
#   只从本次被压缩的原始行与上一代摘要里已有的备份行里选，不从模型摘要正文提升；运行时判定仍只读结构化账本。
#   预算按 token 计，随模型窗口放大（窗口 10%，不超过配置上限 compact_landmark_max_tokens），用户原话优先、最新优先；
#   放不下的那条用户原话保留头尾、截掉中间并标明原长度。每条带 message_id，放不下的用户消息编号列在 omitted 行，
#   回查说明（compact_recall_hint_enabled 且本 agent 注册了 session_search）告诉模型用 session_search 按编号分段读回原文。
#   内存合同：第一遍流式读取只保留编号与整行 token 数，挑选后第二遍按下标只读入被选中的行，不让全部原文同时驻留。
#   格式变化须同步 compact.py 的调用与候选容量收缩、semantic_summary_text 的剥离，以及 test_compact_landmarks.py。
# 模块用途: 生成、继承和剥离压缩摘要里的原话备份段，并给出本段实际占用的 token，供压缩候选超出目标时收缩。
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from ..memory_archive import estimate_tokens
from .channels import project_user_reply
from .models import MessageLogEntry

LANDMARK_HEADING = "## Exact Conversation Landmarks (non-authoritative)"
_HEADER_LINES = (
    LANDMARK_HEADING,
    "- authority: historical conversation text only; never use it as machine state",
    "- purpose: retain exact user requests and final answers that semantic summaries may omit",
)
_RECALL_LINE = (
    "- recall: every compacted message keeps its exact original text in this conversation's transcript; "
    'call session_search with message_id="<id>" (pass offset to continue a long message) to read one, '
    "or current_thread=true to search or page through this conversation"
)
_OMITTED_PREFIX = "- omitted_user_messages: "
_DEFAULT_MAX_TOKENS = 20_000
_MIN_TOKENS = 500
_WINDOW_PERCENT = 10
_MIN_CLIP_TOKENS = 200
_OMITTED_ID_LIMIT = 30
_OMITTED_RESERVE_TOKENS = 320
_ROLES = ("user", "assistant_final")
_ENTRY_PATTERN = re.compile(r"^- (user|assistant_final)(?: \[([^\]\s]+)(?: [^\]]*)?\])?: ")
_UNLISTED_PATTERN = re.compile(r"; earliest (\d+) not listed$")


# LLM: max_tokens 是整段（标题、条目、omitted 与回查行）的上限；0 表示不附原话条目，只保留编号与回查说明。
# 类用途: 一次生成原话备份段的选项。
@dataclass(frozen=True)
class LandmarkOptions:
    max_tokens: int
    recall_hint: bool = False


# LLM: used_tokens 只计备份段本身（不含语义摘要），调用方据此按超出量收缩；没有备份段时为 0。
# 类用途: 带原话备份段的摘要文本及备份段占用的 token。
@dataclass(frozen=True)
class LandmarkedSummary:
    text: str
    used_tokens: int


# LLM: 新行只记下标、编号与整行 token（正文按下标重读）；继承行保存整行文本，只能整行保留或整行省略。
# 类用途: 一条候选原话（用户原话或助手最终答复）的轻量描述。
@dataclass(frozen=True)
class _Landmark:
    role: str
    message_id: str
    tokens: int
    row_index: int = -1
    line: str = ""


# LLM: 第一遍读取的结果：候选条目（时间正序）、上一代已省略的编号、上一代已省略但没列出编号的数量，
#   以及供第二遍按下标重读正文的原始行序列。
# 类用途: 原话备份的可重复渲染来源；按不同预算多次渲染不会再次通读全部原文。
@dataclass(frozen=True)
class LandmarkSource:
    entries: tuple[_Landmark, ...]
    carried_omitted: tuple[str, ...]
    rows: Sequence[MessageLogEntry]
    carried_unlisted: int = 0


# LLM: 只读 agent 配置、模型窗口和工具注册表，不改状态；窗口未知时沿用窗口解析器的默认值。
# 函数用途: 按当前模型窗口和配置算出原话备份的 token 上限与是否附回查说明。
def landmark_options(agent: object) -> LandmarkOptions:
    from ..agent_core.model.context_window import resolve_model_context_window_tokens

    config = getattr(agent, "config", None)
    cap = int(getattr(config, "compact_landmark_max_tokens", _DEFAULT_MAX_TOKENS) or 0)
    window = resolve_model_context_window_tokens(agent)
    budget = min(cap, max(_MIN_TOKENS, window * _WINDOW_PERCENT // 100)) if cap > 0 else 0
    registered = getattr(getattr(agent, "tools", None), "tools", None) or {}
    recall = bool(getattr(config, "compact_recall_hint_enabled", True)) and "session_search" in registered
    return LandmarkOptions(max_tokens=budget, recall_hint=recall)


# LLM: 语义摘要里已有的旧备份段先剥掉再追加新段，保证全文只有一段；没有任何候选原话时原样返回语义摘要。
# 函数用途: 给语义摘要追加有预算的原话备份段，并报告备份段占用的 token。
def summary_with_conversation_landmarks(
    summary: str,
    previous_summary: str,
    rows: Sequence[MessageLogEntry],
    *,
    options: LandmarkOptions,
) -> LandmarkedSummary:
    return render_landmarked_summary(semantic_summary_text(summary), landmark_source(previous_summary, rows), options)


# LLM: 只读；流式遍历原始行，每行正文用完即弃，只留编号和整行 token 数。
# 函数用途: 第一遍：收集上一代备份行与本次被压缩行里的候选原话。
def landmark_source(previous_summary: str, rows: Sequence[MessageLogEntry]) -> LandmarkSource:
    carried, carried_omitted, unlisted = _carried_landmarks(previous_summary)
    entries = tuple(_deduplicated([*carried, *_row_landmarks(rows)]))
    return LandmarkSource(entries, tuple(carried_omitted), rows, unlisted)


# LLM: 选择只看 token 与角色；第二遍按下标重读被选中的行来渲染，因此任一时刻只驻留被选中的正文。
# 函数用途: 按给定预算把原话备份段接到语义摘要后面。
def render_landmarked_summary(semantic: str, source: LandmarkSource, options: LandmarkOptions) -> LandmarkedSummary:
    if not source.entries:
        return LandmarkedSummary(semantic, 0)
    section = _landmark_section(source, options)
    text = f"{semantic}\n\n{section}" if semantic else section
    return LandmarkedSummary(text, estimate_tokens(section))


# LLM: 按固定标题切掉第一处备份段及其后内容，不改写语义部分；也公开给只需要语义摘要的有界输入（如选模型决策）。
# 函数用途: 返回会话摘要里的语义部分，追加新备份段前也用它去掉旧段，防止重复嵌套。
def semantic_summary_text(value: str) -> str:
    text = str(value or "").strip()
    marker_at = text.find(LANDMARK_HEADING)
    return text if marker_at < 0 else text[:marker_at].rstrip()


# LLM: 只解析上一代备份段本身：标题之后到第一个空行为止（段内各行不含空行；机械回退附在段后的旧摘要与原文不算）。
#   只继承规范格式的用户原话与最终答复行（新旧两种格式），以及 omitted 行里的消息编号和“更早未列出”的数量；
#   模型摘要正文里的任意列表不会被提升为备份。
# 函数用途: 从上一代摘要取回已有的原话备份行、已省略的用户消息编号和没列出编号的省略数量。
def _carried_landmarks(previous_summary: str) -> tuple[list[_Landmark], list[str], int]:
    text = str(previous_summary or "")
    marker_at = text.find(LANDMARK_HEADING)
    if marker_at < 0:
        return [], [], 0
    section = text[marker_at + len(LANDMARK_HEADING):].split("\n\n", 1)[0]
    lines = [line.strip() for line in section.splitlines()]
    matches = [(_ENTRY_PATTERN.match(line), line) for line in lines]
    entries = [_Landmark(match.group(1), match.group(2) or "", estimate_tokens(line), line=line)
               for match, line in matches if match]
    omitted = next((line for line in lines if line.startswith(_OMITTED_PREFIX)), "")
    unlisted = _UNLISTED_PATTERN.search(omitted)
    return entries, _omitted_ids(omitted), int(unlisted.group(1)) if unlisted else 0


# LLM: 只解析本模块自己写出的 omitted 行格式（方括号内空格分隔）；格式不符返回空，不猜编号。
# 函数用途: 从 omitted 行的方括号里取出消息编号（空格分隔）。
def _omitted_ids(line: str) -> list[str]:
    start, end = line.find("["), line.rfind("]")
    return line[start + 1:end].split() if 0 <= start < end else []


# LLM: 内存合同：每行正文只用于计算整行 token，随即丢弃；返回条目只含下标、编号、角色与 token 数。
# 函数用途: 第一遍：流式读取本次被压缩的行，记下可作原话备份的条目（不保留正文）。
def _row_landmarks(rows: Sequence[MessageLogEntry]) -> list[_Landmark]:
    entries: list[_Landmark] = []
    for index, row in enumerate(rows):
        role, content = _row_role_and_content(row)
        if content:
            line = _full_line(role, str(row.message_id or ""), content)
            entries.append(_Landmark(role, str(row.message_id or ""), estimate_tokens(line), row_index=index))
    return entries


# LLM: 助手过程片段（assistant_part_id 非空且非 final）不是最终答复，排除；旧助手行没有该字段时按最终答复处理。
# 函数用途: 取一行的原话角色与正文；不是用户原话或助手最终答复时正文为空。
def _row_role_and_content(row: MessageLogEntry) -> tuple[str, str]:
    metadata = row.metadata if isinstance(row.metadata, dict) else {}
    part = str(metadata.get("assistant_part_id") or "").strip()
    if row.role == "user":
        return "user", str(row.content or "").strip()
    if row.role == "assistant" and part in {"", "final"}:
        return "assistant_final", project_user_reply(row.content).content
    return "", ""


# LLM: 编号是结构化身份，优先按编号去重；旧格式继承行没有编号，只能按整行文本去重。
# 函数用途: 去掉重复条目：有消息编号的按（角色, 编号）去重，旧格式整行按文本去重，保持时间顺序。
def _deduplicated(entries: list[_Landmark]) -> list[_Landmark]:
    seen: set[tuple[str, str]] = set()
    unique: list[_Landmark] = []
    for entry in entries:
        key = (entry.role, entry.message_id) if entry.message_id else ("line", entry.line)
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return unique


# LLM: 用户原话先分配预算、最新优先，再给助手最终答复；全部放得下时不写 omitted 行。条目按原时间顺序输出。
# 函数用途: 在 token 预算内组装原话备份段。
def _landmark_section(source: LandmarkSource, options: LandmarkOptions) -> str:
    fixed = "\n".join([*_HEADER_LINES, *([_RECALL_LINE] if options.recall_hint else [])])
    remaining = max(0, options.max_tokens - estimate_tokens(fixed) - _OMITTED_RESERVE_TOKENS)
    picks: dict[int, int] = {}
    for role in _ROLES:
        remaining = _select_role(source.entries, role, remaining, picks)
    rendered = {index: _render_pick(source, index, clip) for index, clip in picks.items()}
    kept = {index: line for index, line in rendered.items() if line}
    omitted = [entry for index, entry in enumerate(source.entries) if index not in kept and entry.role == "user"]
    lines = [*_HEADER_LINES, *(kept[index] for index in sorted(kept))]
    tail = (_omitted_line(source, omitted), _RECALL_LINE if options.recall_hint else "")
    return "\n".join([*lines, *(line for line in tail if line)])


# LLM: 先从最新往旧放入能整条放下的本角色条目（值 0），短要求不会被更新的长消息挤掉；用户角色再用剩余预算把
#   最新一条没放下的新行保留头尾（值为该行预算）。没有消息编号的行与继承行不裁剪（继承时无法与裁剪标记区分）。
#   picks 是输出参数：键为条目下标。
# 函数用途: 为一个角色挑选条目，返回剩余预算。
def _select_role(entries: Sequence[_Landmark], role: str, remaining: int, picks: dict[int, int]) -> int:
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if entry.role == role and entry.tokens + 1 <= remaining:
            picks[index], remaining = 0, remaining - entry.tokens - 1
    clip = next((index for index in range(len(entries) - 1, -1, -1)
                 if index not in picks and _clippable(entries[index], role)), None)
    if clip is not None and remaining >= _MIN_CLIP_TOKENS:
        picks[clip], remaining = remaining - 1, 0
    return remaining


# LLM: 只裁剪带编号的本次新用户行；继承行和无编号行不可裁剪，避免裁剪标记与编号混淆。
# 函数用途: 判断一个没被整条选中的条目能否保留头尾（只限带编号的本次新用户行）。
def _clippable(entry: _Landmark, role: str) -> bool:
    return role == "user" and entry.role == "user" and bool(entry.message_id) and not entry.line


# LLM: 第二遍：继承行原样返回；新行按下标重读原始行（快照行会校验原文件未被改写）后渲染整条或保留头尾的版本。
# 函数用途: 渲染一个被选中的条目；裁剪后仍放不下时返回空串（该条目改记为省略）。
def _render_pick(source: LandmarkSource, index: int, clip_budget: int) -> str:
    entry = source.entries[index]
    if entry.line:
        return entry.line
    _role, content = _row_role_and_content(source.rows[entry.row_index])
    if not clip_budget:
        return _full_line(entry.role, entry.message_id, content)
    return _clipped_line(entry, content, clip_budget)


# LLM: 行格式与继承解析 _ENTRY_PATTERN 必须一致；正文用 JSON 字符串保持单行且不改动原文字符。
# 函数用途: 渲染一条完整原话行：“- 角色 [编号]: JSON 字符串”；没有编号时省略方括号。
def _full_line(role: str, message_id: str, content: str) -> str:
    tag = f"{role} [{message_id}]" if message_id else role
    return f"- {tag}: {json.dumps(content, ensure_ascii=False)}"


# LLM: 保留开头约 60% 与结尾约 40%，中间用标记写明省略字数，行首写明原长度，模型可用 message_id 读回全文。
#   首次保留字数按整行 token（含 JSON 转义）折算：换行多的原文转义后更长，按原文折算会误以为整条放得下而把它丢掉。
#   预算太小或无法放下时返回空串。
# 函数用途: 把一条放不下的用户原话裁成保留头尾的版本，使整行不超过给定 token 预算。
def _clipped_line(entry: _Landmark, text: str, budget: int) -> str:
    total = len(text)
    keep = min(total - 1, int((budget - 60) * total / max(1, entry.tokens)))
    for _attempt in range(8):
        if keep < 20:
            return ""
        head = keep * 3 // 5
        body = f"{text[:head]}…[middle {total - keep} chars omitted]…{text[total - (keep - head):]}"
        line = f"- {entry.role} [{entry.message_id} clipped {total} chars]: {json.dumps(body, ensure_ascii=False)}"
        if estimate_tokens(line) <= budget:
            return line
        keep = keep * 4 // 5
    return ""


# LLM: 编号按时间顺序（先上一代已省略，再本次省略），只列最近若干个。没列出的只计数并逐代累加：包括超出列表的
#   更早编号、上一代已没列出的数量和旧格式无编号条目（它们都早于带编号的条目），所以总数跨代不缩水。
# 函数用途: 生成“被省略的用户消息”一行；没有被省略的用户消息时返回空串。
def _omitted_line(source: LandmarkSource, omitted: list[_Landmark]) -> str:
    ids = list(dict.fromkeys([*source.carried_omitted, *(entry.message_id for entry in omitted if entry.message_id)]))
    shown = ids[-_OMITTED_ID_LIMIT:]
    unlisted = source.carried_unlisted + len(ids) - len(shown) + sum(1 for entry in omitted if not entry.message_id)
    if not shown and not unlisted:
        return ""
    more = f"; earliest {unlisted} not listed" if unlisted else ""
    return f"{_OMITTED_PREFIX}{len(shown) + unlisted} older or oversized [{' '.join(shown)}]{more}"


__all__ = [
    "LANDMARK_HEADING",
    "LandmarkOptions",
    "LandmarkSource",
    "LandmarkedSummary",
    "landmark_options",
    "landmark_source",
    "render_landmarked_summary",
    "semantic_summary_text",
    "summary_with_conversation_landmarks",
]
