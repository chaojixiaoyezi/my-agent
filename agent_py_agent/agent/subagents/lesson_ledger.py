# LLM: 子代理 lesson 账本合同的唯一权威：写入口只有 record_lesson 工具，读入口只有 runner 结果收口。
#   四个字段规范成单行并各自有界，id 取四字段内容 hash，同 run 相同参数只记一次；每 run 条数与账本字节有上限，
#   超限返回结构化结论、不静默丢弃；run/attempt/task 身份只来自宿主 runner 上下文与任务记录，模型不能自报。
#   读回逐行复核版本、字段、id 与 run 归属，不合规行只计数不采用；宿主绝不从模型自然语言回复里提取经验。
#   改字段、上限或模板时同步 record_lesson_tool.py 的 Schema、runner_result_service 的合并与 memory_candidates 的候选。
# 模块用途: 定义子代理“可复用经验”账本 lessons.jsonl 的字段、固定渲染模板、追加与读回规则。
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import locked_json_path, read_jsonl_objects_report

LESSON_LEDGER_VERSION = 1
LESSON_LEDGER_SOURCE = "record_lesson_tool"
# 字段名与字符上限：工具 Schema、写入校验和读回复核共用这一份；四项之和保证渲染正文不超过候选 2000 字上限。
LESSON_FIELD_LIMITS: tuple[tuple[str, int], ...] = (
    ("title", 120),
    ("when_to_use", 500),
    ("procedure", 1000),
    ("applies_to", 200),
)
MAX_LESSONS_PER_RUN = 5
MAX_LESSON_LEDGER_BYTES = 16 * 1024

APPEND_RECORDED = "recorded"
APPEND_ALREADY_RECORDED = "already_recorded"
APPEND_LIMIT_REACHED = "limit_reached"
APPEND_BYTES_EXCEEDED = "bytes_exceeded"

LEDGER_ABSENT = "absent"
LEDGER_OK = "ok"
LEDGER_OVERSIZED = "oversized"
LEDGER_UNREADABLE = "unreadable"


# LLM: 账本损坏或被换成符号链接时按 fail-closed 拒绝追加；它是 OSError 子类，调用方可区分“确定没写”与写入中途失败。
# 类用途: 表示 lessons.jsonl 当前状态不可信、本次没有追加任何内容。
class LessonLedgerCorruptError(OSError):
    pass


# LLM: 四个字段已是规范单行文本；它是 lesson id、渲染正文与候选适用场景的唯一输入。
# 类用途: 保存一条经验的标题、适用时机、做法和适用范围。
@dataclass(frozen=True)
class LessonFields:
    title: str
    when_to_use: str
    procedure: str
    applies_to: str


# LLM: error_code 只用已登记的工具参数码；reason 是 missing/not_string/too_long 三种结构化原因之一。
# 类用途: 描述一次字段校验失败的字段名、原因和上限，供工具返回结构化拒绝。
@dataclass(frozen=True)
class LessonFieldError:
    error_code: str
    field: str
    reason: str
    limit: int


# LLM: 三个身份字段只能由宿主从 runner 上下文与任务记录填入，不接受模型参数。
# 类用途: 标明一条经验来自哪个 run、哪次尝试和哪个根任务。
@dataclass(frozen=True)
class LessonIdentity:
    run_id: str
    attempt_id: str
    task_id: str


# LLM: 读回后的已校验条目；ledger_ref 与 lesson_id 组成候选里的结构化账本引用。
# 类用途: 表示账本里一条通过复核的经验及其来源身份。
@dataclass(frozen=True)
class LessonLedgerEntry:
    lesson_id: str
    fields: LessonFields
    run_id: str
    attempt_id: str
    task_id: str
    created_at: float
    ledger_ref: str

    # LLM: 渲染只经固定模板，保证 output.json 的 lessons 与候选正文逐字一致。
    # 函数用途: 返回这条经验按固定模板渲染的正文。
    @property
    def text(self) -> str:
        return render_lesson_text(self.fields)


# LLM: status 取 APPEND_* 四值之一；只有 recorded 真正写了一行，其余三种都保证账本未变。
# 类用途: 返回一次追加的结构化结论、条目 id、当前条数和账本字节数。
@dataclass(frozen=True)
class LessonAppendResult:
    status: str
    lesson_id: str
    lesson_count: int
    ledger_bytes: int


# LLM: status 取 LEDGER_* 四值之一；rejected 统计不合规、重复或超出上限而未采用的行，结果收口据此写工作日志。
# 类用途: 汇总一次账本读回的已采用条目与被拒条数。
@dataclass(frozen=True)
class LessonLedgerReport:
    ledger_ref: str = ""
    status: str = LEDGER_ABSENT
    entries: tuple[LessonLedgerEntry, ...] = ()
    rejected: int = 0


# LLM: 只做确定性的空白与不可打印字符归一，不改写措辞、不按语义截断；上限按原始长度判定，与 Schema maxLength 一致。
# 函数用途: 校验并规范四个字段，失败时返回第一个出错字段的结构化原因。
def normalize_lesson_fields(values: Mapping[str, object]) -> LessonFields | LessonFieldError:
    normalized: dict[str, str] = {}
    for name, limit in LESSON_FIELD_LIMITS:
        text, error = _normalized_field(values.get(name), name, limit)
        if error is not None:
            return error
        normalized[name] = text
    return LessonFields(**normalized)


# LLM: 缺失/空白为必填错误，非字符串或超长为参数错误；返回的文本已压成单行。
# 函数用途: 校验并规范单个字段。
def _normalized_field(raw: object, name: str, limit: int) -> tuple[str, LessonFieldError | None]:
    if raw is None:
        return "", LessonFieldError("TOOL_PARAMETER_REQUIRED", name, "missing", limit)
    if not isinstance(raw, str):
        return "", LessonFieldError("TOOL_INVALID_ARGUMENTS", name, "not_string", limit)
    if len(raw) > limit:
        return "", LessonFieldError("TOOL_INVALID_ARGUMENTS", name, "too_long", limit)
    text = _single_line(raw)
    if not text:
        return "", LessonFieldError("TOOL_PARAMETER_REQUIRED", name, "missing", limit)
    return text, None


# LLM: 换行、制表和其它不可打印字符一律变空格再压缩，保证渲染模板行数固定、候选正文不超过行数上限。
# 函数用途: 把文本压成去首尾空白的单行。
def _single_line(value: str) -> str:
    printable = "".join(char if char.isprintable() else " " for char in value)
    return " ".join(printable.split())


# LLM: id 只由规范后的四字段决定，不含 run/时间，所以同 run 重复调用天然幂等；改算法会让旧账本读回时全部被拒。
# 函数用途: 计算一条经验的稳定编号。
def lesson_id_for(fields: LessonFields) -> str:
    material = json.dumps(
        [fields.title, fields.when_to_use, fields.procedure, fields.applies_to],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "lesson-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


# LLM: 固定模板、不调用模型；output.json 的 lessons、候选正文和 S1 草稿都以这段文本为准，改动会改变候选身份。
# 函数用途: 把四个字段渲染成四行经验正文。
def render_lesson_text(fields: LessonFields) -> str:
    return (
        f"{fields.title}\n"
        f"适用时机：{fields.when_to_use}\n"
        f"做法：{fields.procedure}\n"
        f"适用范围：{fields.applies_to}"
    )


# LLM: 账本行字段名是持久化协议；身份只取 LessonIdentity，created_at 由调用方传入宿主时间。
# 函数用途: 组装一行待追加的账本记录。
def lesson_ledger_record(
    fields: LessonFields,
    identity: LessonIdentity,
    *,
    created_at: float,
) -> dict[str, object]:
    return {
        "version": LESSON_LEDGER_VERSION,
        "id": lesson_id_for(fields),
        "title": fields.title,
        "when_to_use": fields.when_to_use,
        "procedure": fields.procedure,
        "applies_to": fields.applies_to,
        "task_id": identity.task_id,
        "run_id": identity.run_id,
        "attempt_id": identity.attempt_id,
        "created_at": float(created_at),
        "source": LESSON_LEDGER_SOURCE,
    }


# LLM: 读-判-写在同一把 locked_json_path 锁内完成：先查同 id（幂等优先于上限），再查条数与字节上限，最后以
#   O_NOFOLLOW 追加一行。账本损坏或是符号链接时抛 LessonLedgerCorruptError；副作用：可能创建目录、锁文件与账本文件。
# 函数用途: 把一条经验追加进 run 账本，返回结构化的记录/已记录/超限结论。
def append_lesson_record(path: Path, record: dict[str, object]) -> LessonAppendResult:
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    lesson_id = str(record.get("id") or "")
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked_json_path(path):
        rows, size = _rows_for_append(path)
        status = _append_decision(rows, lesson_id, size + len(line))
        if status == APPEND_RECORDED:
            _append_line_no_follow(path, line)
            return LessonAppendResult(status, lesson_id, len(rows) + 1, size + len(line))
    return LessonAppendResult(status, lesson_id, len(rows), size)


# LLM: 与 record_finding 同一 fail-closed 口径：任何损坏行都拒绝追加，避免在坏账本后面继续累积。
# 函数用途: 在锁内读出现有行和当前字节数。
def _rows_for_append(path: Path) -> tuple[list[dict[str, object]], int]:
    if path.is_symlink():
        raise LessonLedgerCorruptError("lessons.jsonl 是符号链接，已按 fail-closed 拒绝追加")
    if not path.exists():
        return [], 0
    report = read_jsonl_objects_report(path, context="record_lesson.append")
    if report.load_errors:
        raise LessonLedgerCorruptError("lessons.jsonl 存在损坏行，已按 fail-closed 拒绝追加")
    return report.records, path.stat().st_size


# LLM: 顺序固定：同 id 已在账本→已记录；条数满→拒绝；追加后超字节上限→拒绝；否则可写。
# 函数用途: 决定这次追加的结构化结论。
def _append_decision(rows: Sequence[Mapping[str, object]], lesson_id: str, size_after: int) -> str:
    if any(str(row.get("id") or "") == lesson_id for row in rows):
        return APPEND_ALREADY_RECORDED
    if len(rows) >= MAX_LESSONS_PER_RUN:
        return APPEND_LIMIT_REACHED
    if size_after > MAX_LESSON_LEDGER_BYTES:
        return APPEND_BYTES_EXCEEDED
    return APPEND_RECORDED


# LLM: O_NOFOLLOW 防止子代理把账本换成指向工作区外的链接后借本工具越界写；不支持该标志的平台退化为普通追加。
# 函数用途: 以追加方式写入一行，不跟随符号链接。
def _append_line_no_follow(path: Path, line: bytes) -> None:
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o666)
    with os.fdopen(descriptor, "ab") as handle:
        handle.write(line)


# LLM: 只读宿主给出的 run 账本路径；缺失/空路径是 absent，符号链接或加锁/读失败是 unreadable，超字节上限整本不采用。
#   逐行按写入合同复核并去重，最多采用 MAX_LESSONS_PER_RUN 条；任何 OSError 都转成状态，不能打断结果收口。
#   副作用：账本存在时会创建同名锁文件。
# 函数用途: 读回本 run 已记录的经验，供结果收口合并与生成候选。
def read_lesson_ledger(ledger_ref: str, *, run_id: str) -> LessonLedgerReport:
    path_text = str(ledger_ref or "").strip()
    if not path_text:
        return LessonLedgerReport()
    path = Path(path_text)
    if path.is_symlink():
        return LessonLedgerReport(path_text, LEDGER_UNREADABLE)
    if not path.is_file():
        return LessonLedgerReport(path_text, LEDGER_ABSENT)
    try:
        with locked_json_path(path):
            return _read_report_locked(path, path_text, str(run_id or "").strip())
    except OSError:
        return LessonLedgerReport(path_text, LEDGER_UNREADABLE)


# LLM: 在调用方持锁时执行；整文件读失败按 read_jsonl_objects_report 的 load_errors 计入 rejected。
# 函数用途: 按字节上限和逐行复核生成读回报告。
def _read_report_locked(path: Path, ledger_ref: str, run_id: str) -> LessonLedgerReport:
    try:
        size = path.stat().st_size
    except OSError:
        return LessonLedgerReport(ledger_ref, LEDGER_UNREADABLE)
    if size > MAX_LESSON_LEDGER_BYTES:
        return LessonLedgerReport(ledger_ref, LEDGER_OVERSIZED)
    report = read_jsonl_objects_report(path, context="subagent.lesson_ledger.read")
    entries, rejected = _valid_entries(report.records, ledger_ref, run_id)
    return LessonLedgerReport(ledger_ref, LEDGER_OK, tuple(entries), rejected + len(report.load_errors))


# LLM: 首个合法 id 生效，重复 id 与超出条数上限的合法行都计入 rejected。
# 函数用途: 从账本行里挑出可采用的经验条目。
def _valid_entries(
    rows: Sequence[Mapping[str, object]],
    ledger_ref: str,
    run_id: str,
) -> tuple[list[LessonLedgerEntry], int]:
    entries: list[LessonLedgerEntry] = []
    seen: set[str] = set()
    rejected = 0
    for row in rows:
        entry = _entry_from_row(row, ledger_ref, run_id)
        if entry is None or entry.lesson_id in seen or len(entries) >= MAX_LESSONS_PER_RUN:
            rejected += 1
            continue
        seen.add(entry.lesson_id)
        entries.append(entry)
    return entries, rejected


# LLM: 复核版本、run 归属、字段已是规范单行且不超上限、id 等于内容 hash；任何一项不符都不采用（返回 None）。
# 函数用途: 把一行账本记录转成已校验条目。
def _entry_from_row(row: Mapping[str, object], ledger_ref: str, run_id: str) -> LessonLedgerEntry | None:
    version = row.get("version")
    if isinstance(version, bool) or version != LESSON_LEDGER_VERSION or not run_id or row.get("run_id") != run_id:
        return None
    fields = normalize_lesson_fields(row)
    if not isinstance(fields, LessonFields) or not _stored_canonically(row, fields):
        return None
    lesson_id = lesson_id_for(fields)
    if row.get("id") != lesson_id:
        return None
    return LessonLedgerEntry(
        lesson_id=lesson_id,
        fields=fields,
        run_id=run_id,
        attempt_id=_text(row.get("attempt_id")),
        task_id=_text(row.get("task_id")),
        created_at=_timestamp(row.get("created_at")),
        ledger_ref=ledger_ref,
    )


# LLM: 账本只存规范后的字段；原样值与规范值不等说明不是本合同写入的行。
# 函数用途: 判断一行里的四个字段是否已是规范形式。
def _stored_canonically(row: Mapping[str, object], fields: LessonFields) -> bool:
    return all(row.get(name) == getattr(fields, name) for name, _limit in LESSON_FIELD_LIMITS)


# LLM: 身份辅助字段只接受字符串，其它类型读成空值，不获得来源权威。
# 函数用途: 把账本里的字符串字段安全取出。
def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


# LLM: 时间只作观察时间，坏值回退 0，由候选层改用任务时间。
# 函数用途: 把账本里的时间戳安全转成非负浮点数。
def _timestamp(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, float(value))


# LLM: 已有 lessons（结构化输出）在前、账本条目按账本顺序在后，按去首尾空白后的精确文本去重；
#   没有账本条目时原样返回已有列表，不改变旧行为。
# 函数用途: 把账本经验合并进结果 lessons 列表。
def merge_lesson_texts(existing: Sequence[str], entries: Sequence[LessonLedgerEntry]) -> list[str]:
    if not entries:
        return list(existing)
    texts = [*(str(item or "").strip() for item in existing), *(entry.text for entry in entries)]
    return list(dict.fromkeys(text for text in texts if text))


__all__ = [
    "APPEND_ALREADY_RECORDED",
    "APPEND_BYTES_EXCEEDED",
    "APPEND_LIMIT_REACHED",
    "APPEND_RECORDED",
    "LEDGER_ABSENT",
    "LEDGER_OK",
    "LEDGER_OVERSIZED",
    "LEDGER_UNREADABLE",
    "LESSON_FIELD_LIMITS",
    "LESSON_LEDGER_SOURCE",
    "LESSON_LEDGER_VERSION",
    "MAX_LESSONS_PER_RUN",
    "MAX_LESSON_LEDGER_BYTES",
    "LessonAppendResult",
    "LessonFieldError",
    "LessonFields",
    "LessonIdentity",
    "LessonLedgerCorruptError",
    "LessonLedgerEntry",
    "LessonLedgerReport",
    "append_lesson_record",
    "lesson_id_for",
    "lesson_ledger_record",
    "merge_lesson_texts",
    "normalize_lesson_fields",
    "read_lesson_ledger",
    "render_lesson_text",
]
