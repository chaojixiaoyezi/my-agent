from __future__ import annotations

"""LLM: JSONL storage primitives for memory hook snapshots and raw archive events.

新手说明:
这个文件只管把快照和归档事件写到固定目录。
它不决定什么时候压缩、不调用模型，也不读取配置；以后接入真实流程时，外层负责判断时机，这里负责可靠落盘。
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..io import append_jsonl
from .models import CompressionSnapshot, RawMemoryEvent


class MemoryArchiveError(RuntimeError):
    """LLM: raised when archive writes cannot be verified after append.

    新手说明:
    快照和 raw event 写完必须能读回来。
    如果写了却读不到，说明这次恢复锚点不可靠，要明确报错，不能假装"记住了"。
    """


def snapshot_path_for(root: str | Path, created_at: str | int | float | None = None) -> Path:
    """LLM: map a snapshot timestamp to `memory/hooks/YYYY-MM-DD.jsonl`.

    新手说明:
    压缩前 hook 快照每天一个文件。
    比如 2026-04-30 的快照都会放到 `memory/hooks/2026-04-30.jsonl`，以后按天排查会很直观。

    参数说明:
    `root` 是项目根目录或测试临时目录；`created_at` 是事件时间，可以是 ISO 字符串、
    Unix timestamp 或空值。空值会按今天日期落盘。

    返回说明:
    返回当天 hook JSONL 文件路径，不会主动创建文件。
    """

    return Path(root) / "memory" / "hooks" / f"{_date_key(created_at)}.jsonl"


def compression_snapshot_dir(root: str | Path) -> Path:
    """LLM: return the directory that stores authoritative compression snapshot JSON files.

    新手说明:
    `memory/hooks/*.jsonl` 适合按天浏览和搜索，
    但真正阻断压缩的权威快照需要一文件一对象，方便 readback 和恢复校验。
    所以这里单独固定到 `memory_archive/snapshots/`。
    """

    return Path(root) / "memory_archive" / "snapshots"


def compression_snapshot_file_for(root: str | Path, snapshot: CompressionSnapshot) -> Path:
    """LLM: map one compression snapshot to its authoritative JSON file path.

    新手说明:
    文件名优先带时间，再带 snapshot_id，方便人按日期扫，也能保证唯一性。
    """

    safe_id = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in snapshot.snapshot_id)
    return compression_snapshot_dir(root) / f"{_date_key(snapshot.created_at)}--{safe_id}.json"


def raw_event_path_for(root: str | Path, created_at: str | int | float | None = None) -> Path:
    """LLM: map a raw archive event timestamp to `memory/raw/YYYY-MM-DD.jsonl`.

    新手说明:
    冷归档事件也按天分文件，但和 hook 快照分开存。
    这样 daily memory、压缩快照、原始流水不会混在一起，排错时能先看对应功能目录。

    参数说明:
    `root` 是工作区根目录；`created_at` 决定文件名里的日期，格式兼容 ISO 字符串、
    Unix timestamp 和空值。

    返回说明:
    返回当天 raw archive JSONL 文件路径，不会主动创建文件。
    """

    return Path(root) / "memory" / "raw" / f"{_date_key(created_at)}.jsonl"


def filter_snapshot_for_level(payload: dict[str, Any], level: int) -> dict[str, Any]:
    """LLM: strip snapshot fields according to archive level before persistence.

    新手说明:
    archive level 越高，存的内容越少。
    level 0 保留全部字段；level 3 只保留恢复必需的标识符和摘要。
    这样不同等级的用户不会因为归档粒度不同而丢失恢复锚点，也不会浪费磁盘存没用的大字段。

    参数说明:
    `payload` 是 snapshot.to_dict() 之后的字典；`level` 是 0-3 的归档等级。

    返回说明:
    返回过滤后的新字典，不修改原始对象。
    """

    level = max(0, min(3, level))
    if level == 0:
        return dict(payload)
    result = dict(payload)
    if level >= 1:
        for tc in result.get("tool_calls", []):
            if isinstance(tc, dict) and "parameters_preview" in tc:
                tc["parameters_preview"] = ""
    if level >= 2:
        result["user_intents"] = result.get("user_intents", [])[:1]
        result["assistant_actions"] = result.get("assistant_actions", [])[:1]
        result["decisions"] = []
        result["open_questions"] = []
    if level >= 3:
        result["user_intents"] = []
        result["assistant_actions"] = []
        result["tool_calls"] = []
        result["content"] = ""
    return result


def filter_raw_event_for_level(payload: dict[str, Any], level: int) -> dict[str, Any]:
    """LLM: strip raw event fields according to archive level before persistence.

    新手说明:
    和 snapshot 过滤类似，raw event 也会按等级裁剪。
    level 3 只留标识和动作类型，不存内容预览和正文路径，省空间也减少敏感数据暴露。

    参数说明:
    `payload` 是 raw_event.to_dict() 之后的字典；`level` 是 0-3 的归档等级。

    返回说明:
    返回过滤后的新字典，不修改原始对象。
    """

    level = max(0, min(3, level))
    if level == 0:
        return dict(payload)
    result = dict(payload)
    if level >= 2:
        result["content_path"] = ""
    if level >= 3:
        result["content_path"] = ""
    return result


def append_snapshot(root: str | Path, snapshot: CompressionSnapshot) -> Path:
    """LLM: append a compression snapshot and verify it can be read back.

    新手说明:
    这是压缩前最关键的一步：先写快照，再确认磁盘里真的有这一条。
    如果 readback 找不到同一个 `snapshot_id`，函数会报错，避免系统嘴上说保存了、实际没落盘。

    参数说明:
    `root` 是工作区根目录；`snapshot` 是要保存的压缩前快照对象。

    返回说明:
    返回实际写入的 JSONL 文件路径。

    副作用说明:
    会创建 `memory/hooks/` 目录和当天 JSONL 文件，并追加一行 JSON。
    """

    path = snapshot_path_for(root, snapshot.created_at)
    payload = snapshot.to_dict()
    append_jsonl(path, payload, sort_keys=True)
    _verify_record_exists(path, key="snapshot_id", value=snapshot.snapshot_id, expected=payload)
    return path


def write_compression_snapshot_file(root: str | Path, snapshot: CompressionSnapshot) -> Path:
    """LLM: write one authoritative compression snapshot JSON file and verify readback.

    新手说明:
    这份 JSON 文件是压缩流程真正依赖的恢复锚点。
    它保留完整字段，不过滤 archive level，因为恢复时需要所有信息。
    如果写完读不回来，调用方必须把这次压缩当失败处理。
    """

    path = compression_snapshot_file_for(root, snapshot)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = snapshot.to_dict()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    _verify_json_file_payload(path, expected=payload)
    return path


def append_raw_event(root: str | Path, event: RawMemoryEvent) -> Path:
    """LLM: append one raw memory event to the daily cold archive JSONL.

    新手说明:
    这是一条普通冷归档流水。
    它可以记录用户消息、助手动作、工具调用、派工结果等；文件路径由 `created_at` 自动落到当天 raw 文件。

    参数说明:
    `root` 是工作区根目录；`event` 是要保存的 raw archive 事件。

    返回说明:
    返回实际写入的 JSONL 文件路径。

    副作用说明:
    会创建 `memory/raw/` 目录和当天 JSONL 文件，并追加一行 JSON；写完后会读回校验。
    """

    path = raw_event_path_for(root, event.created_at)
    payload = filter_raw_event_for_level(event.to_dict(), event.archive_level)
    append_jsonl(path, payload, sort_keys=True)
    _verify_record_exists(path, key="event_id", value=event.event_id, expected=payload)
    return path


def enforce_retention(root: str | Path, retention_days: Any, today: date | str | None = None) -> list[Path]:
    """LLM: delete hook snapshot files older than the configured retention window.

    新手说明:
    这里只清理 `memory/hooks/`，不碰 raw 冷归档。
    `retention_days=7` 表示保留今天和往前 6 天；`retention_days=0` 表示不删除。
    如果用户把配置写成 `abcd`、负数或奇怪内容，这里会安全跳过，不会崩，也不会误删。

    参数说明:
    `root` 是工作区根目录；`retention_days` 是要保留的天数；
    `today` 是测试或手工复现时指定的"今天"，真实运行通常不传。

    返回说明:
    返回被删除的 hook JSONL 文件路径列表；没有删除时返回空列表。

    副作用说明:
    只删除超过留存窗口的 `memory/hooks/*.jsonl` 文件。
    """

    days = _coerce_retention_days(retention_days)
    if days is None or days == 0:
        return []

    hook_dir = Path(root) / "memory" / "hooks"
    if not hook_dir.exists():
        return []

    today_date = _coerce_today(today) or date.today()
    cutoff = today_date - timedelta(days=days - 1)
    deleted: list[Path] = []

    for path in sorted(hook_dir.glob("*.jsonl")):
        file_date = _date_from_filename(path)
        if file_date is None or file_date >= cutoff:
            continue
        path.unlink()
        deleted.append(path)

    return deleted


def _verify_record_exists(path: Path, *, key: str, value: str, expected: dict[str, Any]) -> None:
    """LLM: read a JSONL file backwards and confirm the just-written record is present.

    新手说明:
    JSONL 是一行一个 JSON。刚写入的记录通常在文件最后，所以从后往前找更快。
    找到同一个 ID 后还要比较完整 payload，避免只写了一半或字段被改坏也算通过。

    参数说明:
    `path` 是要读回的 JSONL 文件；`key` 是唯一字段名，例如 `snapshot_id` 或 `event_id`；
    `value` 是唯一字段值；`expected` 是写入前的完整字典。

    返回说明:
    成功时不返回值；找不到或内容不一致时抛出 `MemoryArchiveError`。
    """

    normalized_expected = _normalized_json(expected)
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get(key) != value:
            continue
        if _normalized_json(payload) != normalized_expected:
            raise MemoryArchiveError(f"readback payload mismatch for {key}={value}")
        return
    raise MemoryArchiveError(f"readback failed for {key}={value} in {path}")


def _verify_json_file_payload(path: Path, *, expected: dict[str, Any]) -> None:
    """LLM: ensure an authoritative JSON snapshot file can be read back exactly.

    新手说明:
    JSON 文件不像 JSONL 那样要按行找记录，但同样要确认"写进去的"和"读出来的"完全一致。
    """

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryArchiveError(f"readback failed for snapshot file {path}: {exc}") from exc
    if _normalized_json(payload) != _normalized_json(expected):
        raise MemoryArchiveError(f"readback payload mismatch for snapshot file {path}")


def _normalized_json(payload: dict[str, Any]) -> str:
    """LLM: serialize JSON payloads into a canonical string for equality checks.

    新手说明:
    普通字典的字段顺序可能不同。这里把字段排序并去掉多余空格，让"内容相同"能稳定比较。

    参数说明:
    `payload` 是要比较的 JSON 字典。

    返回说明:
    返回稳定排序后的 JSON 字符串。
    """

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _date_key(created_at: str | int | float | None) -> str:
    """LLM: convert an optional timestamp-like value into a daily archive key.

    新手说明:
    归档文件名只需要日期，不需要小时分钟。这个 helper 把各种时间输入统一成 `YYYY-MM-DD`。

    参数说明:
    `created_at` 可以是 ISO 字符串、Unix timestamp、空值或非法值。

    返回说明:
    返回日期字符串；无法解析时返回今天日期。
    """

    parsed = _coerce_datetime(created_at)
    if parsed is None:
        return date.today().isoformat()
    return parsed.date().isoformat()


def _coerce_datetime(value: str | int | float | None) -> datetime | None:
    """LLM: safely coerce a caller timestamp into a timezone-aware datetime.

    新手说明:
    外层可能传 ISO 字符串、纯日期、Unix timestamp，也可能传坏值。
    这里尽量解析，解析不了就返回 `None`，让调用方使用安全默认值。

    参数说明:
    `value` 是待解析时间；布尔值虽然是 int 子类，但这里明确拒绝。

    返回说明:
    返回带时区的 `datetime`，或在非法输入时返回 `None`。
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.combine(date.fromisoformat(text[:10]), datetime.min.time())
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _coerce_retention_days(value: Any) -> int | None:
    """LLM: normalize retention-days config without throwing on bad user input.

    新手说明:
    留存天数写坏时，最安全的行为是不删文件。这里只接受非负整数或纯数字字符串。

    参数说明:
    `value` 是配置里的留存天数，可能来自 YAML、CLI 或测试。

    返回说明:
    返回非负整数；非法值返回 `None`，调用方会跳过删除。
    """

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        days = value
    elif isinstance(value, str):
        text = value.strip()
        if not text.isdecimal():
            return None
        days = int(text)
    else:
        return None

    if days < 0:
        return None
    return days


def _coerce_today(value: date | str | None) -> date | None:
    """LLM: normalize an optional test override for today's date.

    新手说明:
    留存测试需要固定"今天"是哪一天。真实运行不传这个值时，会使用系统日期。

    参数说明:
    `value` 可以是 `date`、`datetime`、ISO 日期字符串或空值。

    返回说明:
    返回 `date`；非法字符串返回 `None`。
    """

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _date_from_filename(path: Path) -> date | None:
    """LLM: parse `YYYY-MM-DD.jsonl` filenames for retention decisions.

    新手说明:
    retention 只按规范日期文件名删除。名字不是日期的 JSONL 会被跳过，避免误删手工文件。

    参数说明:
    `path` 是候选 hook 文件路径。

    返回说明:
    文件名能解析时返回日期，否则返回 `None`。
    """

    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return None
