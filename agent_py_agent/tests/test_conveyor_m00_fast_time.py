# LLM: Conveyor M00 / R3-G0：fast_time 必须与 datetime(…, timezone.utc).isoformat() 逐字符一致——微秒向下取整、
#   微秒为零时省略小数部分、后缀固定 +00:00；10^6 个随机纳秒时间戳（含大量零微秒样本）逐一比对，
#   utc_now_iso 落在前后两次 datetime.now(timezone.utc) 之间；同时给 G1 锁等待门禁提供压测用例工厂。
# 模块用途: 固定 agent/common/fast_time.py 的格式合同（C1.2），不读 git 历史。
from __future__ import annotations

import random
import re
from datetime import datetime, timedelta, timezone

from agent_py_agent.agent.common import fast_time

# 随机纳秒样本数（10^6）；若 3.12 上本文件超过 20 秒，按切片规定降到 2×10^5 并在交接块记录。
RANDOM_SAMPLE_COUNT = 1_000_000
# 固定随机种子，失败可复现。
RANDOM_SAMPLE_SEED = 1119
# 随机秒数上界：2100-01-01T00:00:00Z，覆盖网关运行期内全部 time.time_ns() 取值。
RANDOM_SECONDS_UPPER_BOUND = 4_102_444_800
# 每 4 个样本有 1 个只带亚微秒尾数（微秒为零），专测省略 .ffffff 的分支。
ZERO_MICROSECOND_EVERY = 4

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?\+00:00$")


# 函数用途: 用 datetime 精确算出同一纳秒时间戳的参考格式（timedelta 整数微秒运算无舍入误差）。
def _reference(ns: int) -> str:
    return (_EPOCH + timedelta(microseconds=ns // 1000)).isoformat()


def test_edge_values_match_datetime_isoformat():
    edges = [
        0, 1, 999, 1000, 1001, 999_999, 1_000_000, 999_999_999, 1_000_000_000,
        1_700_000_000_000_000_000, 1_700_000_000_000_000_999, 1_700_000_000_123_456_789,
        951_782_400_000_000_000,  # 2000-02-29 闰日
        (RANDOM_SECONDS_UPPER_BOUND - 1) * 1_000_000_000 + 999_999_999,
        253_402_300_799 * 1_000_000_000 + 999_999_999,  # 9999-12-31T23:59:59.999999
    ]
    for ns in edges:
        assert fast_time.utc_iso_from_ns(ns) == _reference(ns), ns


def test_random_nanoseconds_match_datetime_isoformat_including_zero_microseconds():
    rng = random.Random(RANDOM_SAMPLE_SEED)
    convert = fast_time.utc_iso_from_ns
    mismatches = []
    zero_micro = 0
    for index in range(RANDOM_SAMPLE_COUNT):
        seconds = rng.randrange(RANDOM_SECONDS_UPPER_BOUND)
        if index % ZERO_MICROSECOND_EVERY == 0:
            ns = seconds * 1_000_000_000 + rng.randrange(1000)
            zero_micro += 1
        else:
            ns = seconds * 1_000_000_000 + rng.randrange(1_000_000_000)
        ours, expected = convert(ns), _reference(ns)
        if ours != expected:
            mismatches.append((ns, ours, expected))
            if len(mismatches) >= 5:
                break
    assert not mismatches
    assert zero_micro >= RANDOM_SAMPLE_COUNT // ZERO_MICROSECOND_EVERY


def test_utc_now_iso_is_bracketed_by_datetime_now():
    for _ in range(1000):
        before = datetime.now(timezone.utc)
        text = fast_time.utc_now_iso()
        after = datetime.now(timezone.utc)
        assert _ISO_UTC.match(text), text
        # 两个时钟同源（CLOCK_REALTIME）；向下取整到微秒后必落在前后两次读数之间。
        assert before <= datetime.fromisoformat(text) <= after


# 函数用途: G1 锁等待门禁用例工厂：返回零参可调用，每次调用格式化一次当前 UTC 时间。
def g1_case_fast_time():
    return fast_time.utc_now_iso
