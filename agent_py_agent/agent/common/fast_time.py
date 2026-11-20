"""UTC ISO 时间串：与 datetime.now(timezone.utc).isoformat() 逐字符一致，不构造带时区 datetime。

带时区的 datetime 构造与格式化会在每次调用时查找 tzinfo 类型上的方法；python3.14t 关闭 GIL、
多线程时这类查找会落到全局类型锁上（R3 §4.5）。这里只用 time.time_ns()、整数运算与 time.gmtime。
微秒向下取整；微秒为零时省略小数部分；后缀固定为 +00:00。参与时间运算的带时区 datetime 不在此列。
"""

from __future__ import annotations

import time


# LLM: ns 为 Unix 纪元起的整数纳秒（同 time.time_ns()）；divmod 向下取整，与 datetime 的微秒截断一致。
# 函数用途: 把一个纳秒时间戳格式化为 isoformat 形式的 UTC 时间串。
def utc_iso_from_ns(ns: int) -> str:
    seconds, remainder = divmod(ns, 1_000_000_000)
    microseconds = remainder // 1000
    # struct_time 前 6 项依次为年、月、日、时、分、秒；切片取值不走逐字段属性查找。
    fields = time.gmtime(seconds)[:6]
    if microseconds:
        return "%04d-%02d-%02dT%02d:%02d:%02d.%06d+00:00" % (*fields, microseconds)
    return "%04d-%02d-%02dT%02d:%02d:%02d+00:00" % fields


# 函数用途: 返回当前 UTC 时间的 isoformat 串（等同 datetime.now(timezone.utc).isoformat()）。
def utc_now_iso() -> str:
    return utc_iso_from_ns(time.time_ns())


__all__ = ["utc_iso_from_ns", "utc_now_iso"]
