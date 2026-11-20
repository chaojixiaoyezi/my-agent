# LLM: consumer与metadata发现共用纯时间判据；不读取配置/文件、不创建任务或授执行权，原batch/lease及来源末门仍由调用方负责。
# 模块用途: 统一策展的本地日期、间隔与下次时间边界，避免UTC发现和实际运行时区分叉。
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


# LLM: 完整保留consumer原时钟语义，空时区为服务器local，不新增环境优先级或坏时区fallback。
# 函数用途: 将同一时刻换成策展实际使用的日期和小时。
def localized_now(value: datetime | None, timezone_name: str) -> datetime:
    current = value or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if not timezone_name:
        return current.astimezone()
    return current.astimezone(ZoneInfo(timezone_name))


# LLM: 原consumer已规定坏/naive耐久时间视为缺失；发现层不得以不同parse规则延长资格。
# 函数用途: 解析带时区的原成功或失败时间。
def parse_time(value: str) -> datetime | None:
    if not str(value or "").strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


# LLM: 仅判断原成功后时间窗，真实输入/batch条件仍在各调用方，不把mtime粗筛当作consumer已采集。
# 函数用途: 判断策展间隔是否已经过去。
def interval_elapsed(state, now: datetime, interval_seconds: int) -> bool:
    last_success = parse_time(state.last_success_at)
    return last_success is None or (now-last_success).total_seconds() >= interval_seconds


# LLM: 原consumer日终判据唯一实现，日期和小时必须来自同一本地clock，不能另读UTC日期。
# 函数用途: 判断本地今天是否已到收尾时间且尚未完成。
def daily_finalize_date(state, now: datetime, finalize_hour: int, *, window_seconds=0) -> str:
    """每次领取一个已到期本地日期，午夜后继续处理未完成的日终。"""
    today = now.date()
    eligible = today if now.hour >= finalize_hour else today - timedelta(days=1)
    active = next((item["date"] for item in getattr(state, "pending_local_dates", ())
                   if item["source_range"]["kind"] == "finalize" and item["date"] <= eligible.isoformat()), "")
    if active and active > state.last_daily_finalize_date:
        return active
    last = state.last_daily_finalize_date
    if last:
        try:
            oldest = date.fromisoformat(last) + timedelta(days=1)
        except (TypeError, ValueError, OverflowError):
            return ""
    else:
        boundary = datetime.combine(eligible, time(hour=finalize_hour), tzinfo=now.tzinfo)
        if eligible != today:
            latest = parse_time(getattr(state, "last_success_at", ""))
            if latest is not None:
                if latest.astimezone(now.tzinfo).date() > eligible:
                    return ""
            elif now.timestamp() - boundary.timestamp() > window_seconds:
                return ""
        oldest = eligible
    return oldest.isoformat() if oldest <= eligible else ""


def daily_finalize_due(state, now: datetime, finalize_hour: int, *, window_seconds=0) -> bool:
    return bool(daily_finalize_date(state, now, finalize_hour, window_seconds=window_seconds))


def unresolved_local_range(state, now):
    """只选择已经过了自身退避的精确来源范围。"""
    return next((item for item in getattr(state, "pending_local_dates", ()) if item["source_range"]["kind"] == "input"
                 and (not item["retry_at"] or parse_time(item["retry_at"]) <= now)), None)


def range_failure_backoff(state):
    return any(item["source_range"]["kind"] == "input" and item["failure_type"] == state.last_failure_code
               and item["failed_at"] == state.last_failure_at for item in getattr(state, "pending_local_dates", ()))


# LLM: 只提供缓存失效时间而非timer/执行许可；空时区由naive目标日期timestamp重算系统规则，不能沿用当前fixed-offset跨DST。
# 函数用途: 计算下一次本地小时或次日零点对应的绝对时刻。
def next_local_boundary(current: datetime, timezone_name: str, hour: int, *, next_day: bool = False) -> float:
    day = current.date()+timedelta(days=int(next_day))
    target = datetime.combine(day,time(hour=hour))
    if timezone_name:
        target = target.replace(tzinfo=ZoneInfo(timezone_name))
    if target.timestamp() <= current.timestamp():
        target += timedelta(days=1)
    return target.timestamp()


def candidate_scan_deadline(state, interval_seconds, *, source_present=False):
    """晋升续页复用原维护30–300秒冷却，不借提取pending触发模型或重做日报。"""
    cursor = getattr(state, "candidate_scan_cursor", {})
    if not cursor.get("active") and not (not cursor and source_present):
        return None
    last = parse_time(cursor.get("last_at", ""))
    cadence = max(30, min(300, interval_seconds))
    deadline = 0.0 if last is None else last.timestamp() + cadence
    failure = parse_time(state.last_failure_at)
    if state.last_failure_code and failure is not None:
        from .curator_models import curator_failure_retry_seconds
        deadline = max(
            deadline,
            failure.timestamp()
            + curator_failure_retry_seconds(
                state.last_failure_code,
                cadence,
                consecutive_failures=max(0, int(getattr(state, "consecutive_failures", 0) or 0)),
            ),
        )
    return deadline


def compact_thread_stamp(path):
    """线程 CAS 的原目录更改标记只用于唤醒，消费身份仍以检查点核验为准。"""
    try:
        return str(path.stat().st_mtime_ns)
    except OSError:
        return ""


def candidate_source_present(path):
    """只读规范候选叶的存在/大小提示；读错只触发原服务核验，不把未知当空。"""
    try:
        return path.stat(follow_symlinks=False).st_size > 0
    except FileNotFoundError:
        return False
    except OSError:
        return True
