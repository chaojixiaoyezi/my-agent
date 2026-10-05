#!/usr/bin/env python3
# LLM: 只读运维报告：汇总 owner home 里 model_usage 账本的缓存命中与用量（按天×用途）。
#   账本发现复用 reproject_model_usage 的布局表（唯一来源，不写死路径）；只读结构化字段，
#   不读会话正文、不写任何文件；坏行跳过计数、不抛异常中断。口径与 store_usage 的
#   provider/estimated 分区同源：命中率 = Σcache_read ÷ Σinput，cache_write 不算命中，
#   估算口径不与供应商口径混加（estimated.call_count 是缺报调用数，单列）。
# 模块用途: 从 model_usage JSONL 汇总供应商/估算用量与缓存命中率，输出中文表格或 JSON。
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


def _ensure_repo_root_on_path() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    return repo_root


_REPO_ROOT = _ensure_repo_root_on_path()

from scripts.reproject_model_usage import discover_ledger_paths  # noqa: E402

# 供应商口径的统计字段；名字与 store_usage.ModelUsageStore.summary 的 provider 分区一致。
_PROVIDER_FIELDS = ("call_count", "input_tokens", "cache_read_input_tokens", "output_tokens")
# 估算口径：estimated.call_count 是"供应商未回报、只有本地估算"的调用数（缺报调用数）；
# estimated.unfinished_call_count 是其中未完成（超时/失败）的子集，单列避免与供应商口径混加。
_ESTIMATED_CALLS = "call_count"
_ESTIMATED_UNFINISHED = "unfinished_call_count"
# 压缩类来源按记录 source 分组时单独成行（如 conversation_compact），其它来源原样显示。
COMPACT_SOURCE = "conversation_compact"


# LLM: 一个维度（天×用途×[来源]×[模型]）的累加器；provider 与 estimated 分字段存放，永不混加。
# 类用途: 保存单个分组的调用数、token 与缓存命中统计。
@dataclass
class _Bucket:
    call_count: int = 0
    input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    estimated_calls: int = 0
    estimated_unfinished_calls: int = 0

    # 函数用途: 把另一份同维度计数加进来。
    def add(self, other: _Bucket) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))

    # 函数用途: 缓存命中率 = Σcache_read ÷ Σinput；没有输入时返回 None（显示为 —）。
    @property
    def hit_rate(self) -> float | None:
        if self.input_tokens <= 0:
            return None
        return self.cache_read_input_tokens / self.input_tokens

    # 函数用途: 未命中输入 token = 输入 - 缓存读取（不为负）。
    @property
    def missed_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cache_read_input_tokens)


# LLM: 可选维度收成一个不可变载体，保持构建器与渲染函数参数不增长（code-size 参数守卫）。
# 类用途: 标记表格是否按来源、按模型再分组。
@dataclass(frozen=True)
class _Dimensions:
    by_source: bool = False
    by_model: bool = False

    # 函数用途: 返回当前维度下分组键的列名（表头用）。
    def column_names(self) -> tuple[str, ...]:
        columns = ["日期", "用途"]
        if self.by_source:
            columns.append("来源")
        if self.by_model:
            columns.append("模型")
        return tuple(columns)


# LLM: 用量字段只接受非负整数；坏值按 0（与 store_usage._usage_int 同口径），不抛异常。
# 函数用途: 把用量字段归一为非负整数。
def _int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# LLM: 天分组用本地时区（运维视角按当地日期看趋势）；缺时间戳的行标 "(无时间)" 不参与时间窗。
# 函数用途: 把 created_at 时间戳换算成 YYYY-MM-DD；坏值返回 "(无时间)"。
def _day_of(value: object) -> str:
    try:
        return datetime.fromtimestamp(float(value or 0.0)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return "(无时间)"


# LLM: 模型标签只读结构化 models 名单；多模型一行时按名拼接，不按 backend 猜。
# 函数用途: 取一条记录的模型标签（多个用 | 连接；缺失给占位）。
def _model_label(model_calls: dict[str, Any]) -> str:
    models = model_calls.get("models")
    if isinstance(models, (list, tuple)):
        names = [str(item) for item in models if str(item)]
        if names:
            return "|".join(names)
    return "(未记模型)"


# LLM: 桶读取独立成函数：schema 与非对象条目跳过；保持主入口只有平级分支（嵌套预算）。
# 函数用途: 把 purpose_breakdown 里的桶读成 (用途, 用量摘要) 列表。
def _partition_rows(breakdown: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for purpose, row in breakdown.items():
        if purpose == "schema" or not isinstance(row, dict):
            continue
        rows.append((str(purpose), row))
    return rows


# LLM: 分区口径：有 purpose_breakdown 就用互斥分区（各桶不重复）；没有才回退根 usage_breakdown，
#   标 "(未分区)"。绝不同时统计根与分区，避免重复计数。
# 函数用途: 列出 (用途, 用量摘要) 对；没有任何用量结构时返回空。
def _purpose_rows(model_calls: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    breakdown = model_calls.get("purpose_breakdown")
    if isinstance(breakdown, dict) and breakdown.get("schema"):
        rows = _partition_rows(breakdown)
        if rows:
            return rows
    if isinstance(model_calls.get("usage_breakdown"), dict):
        return [("(未分区)", model_calls)]
    return []


# LLM: 一条用量摘要里 provider/estimated 分开读；全零桶不产出贡献，避免给空桶造行。
# 函数用途: 从用量摘要抽出 _Bucket；没有可用用量结构时返回 None。
def _bucket_from_usage(usage: dict[str, Any]) -> _Bucket | None:
    breakdown = usage.get("usage_breakdown")
    if not isinstance(breakdown, dict):
        return None
    provider = breakdown.get("provider")
    estimated = breakdown.get("estimated")
    provider = provider if isinstance(provider, dict) else {}
    estimated = estimated if isinstance(estimated, dict) else {}
    bucket = _Bucket(
        call_count=_int(provider.get("call_count")),
        input_tokens=_int(provider.get("input_tokens")),
        cache_read_input_tokens=_int(provider.get("cache_read_input_tokens")),
        output_tokens=_int(provider.get("output_tokens")),
        estimated_calls=_int(estimated.get(_ESTIMATED_CALLS)),
        estimated_unfinished_calls=_int(estimated.get(_ESTIMATED_UNFINISHED)),
    )
    if not any(getattr(bucket, name) for name in bucket.__dataclass_fields__):
        return None
    return bucket


# LLM: 一行记录换算成 (日期, 用途, 来源, 模型, 计数) 贡献列表；只读结构化字段，坏行返回空。
# 函数用途: 把一条账本记录拆成可累加的用量贡献。
def _record_contributions(record: dict[str, Any]) -> list[tuple[tuple[str, str, str, str], _Bucket]]:
    model_calls = record.get("model_calls")
    if not isinstance(model_calls, dict):
        return []
    day = _day_of(record.get("created_at"))
    source = str(record.get("source") or "").strip() or "(无来源)"
    model = _model_label(model_calls)
    contributions: list[tuple[tuple[str, str, str, str], _Bucket]] = []
    for purpose, usage in _purpose_rows(model_calls):
        bucket = _bucket_from_usage(usage)
        if bucket is not None:
            contributions.append(((day, purpose, source, model), bucket))
    return contributions


# LLM: 单行解析容错：非 JSON、非对象、缺 event_id 都算坏行（返回 None），由调用方计数跳过。
# 函数用途: 解析一行 JSONL；坏行返回 None。
def _parse_line(line: str) -> dict[str, Any] | None:
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(row, dict) or not str(row.get("event_id") or "").strip():
        return None
    return row


# LLM: 只读打开账本文件；单文件读不了或单行坏掉都跳过计数，不中断其它文件（任务硬约束）。
#   单文件逻辑独立成函数，保持收集入口只有一层循环（嵌套预算）。
# 函数用途: 读一个账本文件，把合法行追加进 records，并把问题计数进 stats。
def _collect_file(path: Path, records: list[dict[str, Any]], stats: dict[str, int]) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        stats["unreadable_files"] += 1
        return
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        row = _parse_line(stripped)
        if row is None:
            stats["bad_lines"] += 1
            continue
        records.append(row)


# 函数用途: 收集全部账本行与扫描统计（文件数、坏行数、读不了的文件数）。
def _collect_records(paths: list[Path]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    records: list[dict[str, Any]] = []
    stats = {"files": 0, "bad_lines": 0, "unreadable_files": 0}
    for path in paths:
        stats["files"] += 1
        _collect_file(path, records, stats)
    return records, stats


# LLM: 时间窗按 YYYY-MM-DD 字典序比较（等价时间序）；"(无时间)" 只在不设窗时保留。
# 函数用途: 判断一个日期是否落在 [since, until] 窗口内。
def _in_window(day: str, since: str, until: str) -> bool:
    return (not since or day >= since) and (not until or day <= until)


# LLM: 分组构建器把过滤与累加收在一处，避免把六个配置项传遍每个函数（参数守卫）。
# 类用途: 按天×用途（可选来源/模型）累加用量分组，并记录跳过统计。
class _ReportBuilder:
    # 函数用途: 保存时间窗与维度配置；不读取任何文件。
    def __init__(self, *, since: str = "", until: str = "", dimensions: _Dimensions | None = None) -> None:
        self.since = since
        self.until = until
        self.dimensions = dimensions or _Dimensions()
        self.groups: dict[tuple[str, ...], _Bucket] = {}
        self.stats = {"rows": 0, "skipped_window": 0, "skipped_no_usage": 0}

    # 函数用途: 把一条记录的贡献累进对应分组；窗口外与无用量都计数跳过。
    def add_record(self, record: dict[str, Any]) -> None:
        self.stats["rows"] += 1
        contributions = _record_contributions(record)
        if not contributions:
            self.stats["skipped_no_usage"] += 1
            return
        for values, bucket in contributions:
            if not _in_window(values[0], self.since, self.until):
                self.stats["skipped_window"] += 1
                continue
            key = self._group_key(values)
            self.groups.setdefault(key, _Bucket()).add(bucket)

    # 函数用途: 按维度配置裁剪分组键（日期、用途必留；来源、模型按开关）。
    def _group_key(self, values: tuple[str, str, str, str]) -> tuple[str, ...]:
        day, purpose, source, model = values
        key = [day, purpose]
        if self.dimensions.by_source:
            key.append(source)
        if self.dimensions.by_model:
            key.append(model)
        return tuple(key)

    # 函数用途: 返回按分组键排序的 (键, 计数) 列表，供渲染。
    def sorted_rows(self) -> list[tuple[tuple[str, ...], _Bucket]]:
        return [(key, self.groups[key]) for key in sorted(self.groups)]

    # 函数用途: 汇总全部分组的合计（命中率按总量重算，不做平均）。
    def totals(self) -> _Bucket:
        total = _Bucket()
        for bucket in self.groups.values():
            total.add(bucket)
        return total


# ===== 渲染与命令行 =====

# LLM: 中文/全角按 2 列宽计算，保证中文表头在终端里对齐；空值统一用 —，不打印 "None"。
# 函数用途: 计算字符串的终端显示宽度。
def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


# 函数用途: 按显示宽度补齐（默认左对齐，表头也用同一规则）。
def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


# 函数用途: 命中率显示一位小数百分比；没有输入时显示 —（不显示 0%）。
def _rate_text(rate: float | None) -> str:
    return "—" if rate is None else f"{rate * 100:.1f}%"


# 函数用途: 整数按千分位显示。
def _count_text(value: int) -> str:
    return f"{value:,}"


_VALUE_HEADERS = ("调用数", "输入tokens", "缓存读取", "命中率", "输出tokens", "缺报调用")


# 函数用途: 一个分组的六个数值列单元格（表头顺序固定）。
def _value_cells(bucket: _Bucket) -> list[str]:
    return [
        _count_text(bucket.call_count),
        _count_text(bucket.input_tokens),
        _count_text(bucket.cache_read_input_tokens),
        _rate_text(bucket.hit_rate),
        _count_text(bucket.output_tokens),
        _count_text(bucket.estimated_calls),
    ]


# LLM: 表格列宽由全部行（含表头）按显示宽度取最大值，单元格与表头共用同一宽度表。
# 函数用途: 把二维单元格渲染成对齐文本。
def _format_table(rows: list[list[str]]) -> str:
    column_count = len(rows[0])
    widths = [0] * column_count
    for row in rows:
        for index in range(column_count):
            widths[index] = max(widths[index], _display_width(row[index]))
    lines: list[str] = []
    for row in rows:
        lines.append("  ".join(_pad(cell, widths[index]) for index, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


# LLM: 表尾附合计行（命中率按总量重算，不做分组平均）；合计行的维度列留空避免误导。
# 函数用途: 渲染按天×用途（可选来源/模型）的主表格。
def _render_rows_table(
    rows: list[tuple[tuple[str, ...], _Bucket]],
    dimensions: _Dimensions,
    totals: _Bucket,
) -> str:
    headers = [*dimensions.column_names(), *_VALUE_HEADERS]
    table = [headers]
    for key, bucket in rows:
        table.append([*key, *_value_cells(bucket)])
    dimension_count = len(dimensions.column_names())
    table.append(["合计", *([""] * (dimension_count - 1)), *_value_cells(totals)])
    return _format_table(table)


# LLM: compare 把"天"聚合成前/后两组（基准日之前 = 日期 < pivot；之后 = 日期 >= pivot），
#   其余维度（用途、可选来源/模型）保留；两列各自重算命中率与未命中，不跨期平均。
# 函数用途: 按基准日把分组拆成 (之前, 之后) 两个映射。
def _split_before_after(
    groups: dict[tuple[str, ...], _Bucket],
    pivot: str,
) -> tuple[dict[tuple[str, ...], _Bucket], dict[tuple[str, ...], _Bucket]]:
    before: dict[tuple[str, ...], _Bucket] = {}
    after: dict[tuple[str, ...], _Bucket] = {}
    for key, bucket in groups.items():
        target = before if key[0] < pivot else after
        target.setdefault(key[1:], _Bucket()).add(bucket)
    return before, after


# 函数用途: 渲染"之前 / 之后"对比表（调用数、输入、命中率、未命中 token）。
def _render_compare(
    groups: dict[tuple[str, ...], _Bucket],
    dimensions: _Dimensions,
    pivot: str,
) -> str:
    before, after = _split_before_after(groups, pivot)
    labels = dimensions.column_names()[1:]
    headers = [
        *labels,
        "之前调用数", "之前输入", "之前命中率", "之前未命中",
        "之后调用数", "之后输入", "之后命中率", "之后未命中",
    ]
    table = [headers]
    for key in sorted({*before, *after}):
        left = before.get(key, _Bucket())
        right = after.get(key, _Bucket())
        table.append([
            *key,
            _count_text(left.call_count), _count_text(left.input_tokens),
            _rate_text(left.hit_rate), _count_text(left.missed_input_tokens),
            _count_text(right.call_count), _count_text(right.input_tokens),
            _rate_text(right.hit_rate), _count_text(right.missed_input_tokens),
        ])
    return _format_table(table)


# LLM: JSON 数字保持整数原值（不套千分位/百分比），机器消费端自己格式化。
# 函数用途: 把一个分组计数转成 JSON 字典。
def _bucket_dict(bucket: _Bucket) -> dict[str, Any]:
    return {
        "call_count": bucket.call_count,
        "input_tokens": bucket.input_tokens,
        "cache_read_input_tokens": bucket.cache_read_input_tokens,
        "hit_rate": bucket.hit_rate,
        "missed_input_tokens": bucket.missed_input_tokens,
        "output_tokens": bucket.output_tokens,
        "estimated_calls": bucket.estimated_calls,
        "estimated_unfinished_calls": bucket.estimated_unfinished_calls,
    }


# 函数用途: 把分组行转成 JSON 行列表（维度列名按当前配置）。
def _rows_payload(
    rows: list[tuple[tuple[str, ...], _Bucket]],
    dimensions: _Dimensions,
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for key, bucket in rows:
        entry = dict(zip(dimensions.column_names(), key))
        entry.update(_bucket_dict(bucket))
        payload.append(entry)
    return payload


# 函数用途: 把对比分组转成 JSON 列表（每行含 before/after 两个计数块）。
def _compare_payload(
    groups: dict[tuple[str, ...], _Bucket],
    dimensions: _Dimensions,
    pivot: str,
) -> list[dict[str, Any]]:
    before, after = _split_before_after(groups, pivot)
    labels = dimensions.column_names()[1:]
    payload: list[dict[str, Any]] = []
    for key in sorted({*before, *after}):
        entry = dict(zip(labels, key))
        entry["before"] = _bucket_dict(before.get(key, _Bucket()))
        entry["after"] = _bucket_dict(after.get(key, _Bucket()))
        payload.append(entry)
    return payload


# LLM: 参数只有输入与展示选项；默认只读、不写文件、不改 owner home。--deep 仅在常规布局
#   全落空时使用（真实 owner home 下整树递归很慢）。
# 函数用途: 解析命令行参数。
def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="汇总 owner home 里 model_usage 账本的缓存命中与用量（只读，不写文件）。",
    )
    parser.add_argument("--owner-home", required=True, help="owner home 路径（只读）")
    parser.add_argument("--since", default="", help="起始日期 YYYY-MM-DD（含当天）")
    parser.add_argument("--until", default="", help="结束日期 YYYY-MM-DD（含当天）")
    parser.add_argument("--compare", default="", help="对比基准日 YYYY-MM-DD：输出之前/之后两列")
    parser.add_argument("--by-source", action="store_true", help="再按来源（source）分组")
    parser.add_argument("--by-model", action="store_true", help="再按模型分组")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--deep", action="store_true", help="常规布局找不到账本时整树递归（慢）")
    return parser.parse_args(argv)


# LLM: 只读入口：解析参数 → 发现账本（复用 reproject 布局）→ 收集行 → 分组 → 渲染。
#   发现/读取的问题只打警告并计数，绝不中断；只 print，不写任何文件。
# 函数用途: 命令行主入口；返回进程退出码（0 正常，2 输入不可用）。
def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    owner_home = Path(args.owner_home).expanduser()
    if not owner_home.is_dir():
        print(f"owner home 不存在: {owner_home}", file=sys.stderr)
        return 2
    paths, discovery_errors = discover_ledger_paths([str(owner_home)], deep=args.deep)
    for message in discovery_errors:
        print(f"[警告] {message}", file=sys.stderr)
    records, scan = _collect_records(paths)
    dimensions = _Dimensions(by_source=bool(args.by_source), by_model=bool(args.by_model))
    builder = _ReportBuilder(
        since=str(args.since or ""),
        until=str(args.until or ""),
        dimensions=dimensions,
    )
    for record in records:
        builder.add_record(record)
    scan.update(builder.stats)
    if args.json:
        payload: dict[str, Any] = {
            "schema": "cache_hit_report.v1",
            "owner_home": str(owner_home),
            "window": {"since": str(args.since or ""), "until": str(args.until or "")},
            "scan": scan,
            "totals": _bucket_dict(builder.totals()),
            "rows": _rows_payload(builder.sorted_rows(), dimensions),
        }
        if args.compare:
            payload["compare"] = {
                "pivot": str(args.compare),
                "groups": _compare_payload(builder.groups, dimensions, str(args.compare)),
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(
        f"owner home: {owner_home}（文件 {scan['files']}，行 {scan['rows']}，"
        f"坏行 {scan['bad_lines']}，窗口外 {scan['skipped_window']}，无用量 {scan['skipped_no_usage']}）"
    )
    if args.compare:
        print(f"对比基准日: {args.compare}（之前 = 日期 < 基准；之后 = 日期 >= 基准）")
        print(_render_compare(builder.groups, dimensions, str(args.compare)))
        return 0
    print(_render_rows_table(builder.sorted_rows(), dimensions, builder.totals()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
