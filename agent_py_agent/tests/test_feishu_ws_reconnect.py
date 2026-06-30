"""飞书长连接「进程内断线重连」单测(#2 借鉴 长期助手 每通道看门狗)。不打真网络/不真 sleep。"""

from __future__ import annotations

from unittest.mock import patch

from agent_py_agent.agent.adapter import feishu_ws


class _FakeClient:
    """假 lark 客户端:start() 每次自增计数;到第 stop_at 次模拟被 close()(_stopped=True)。"""

    def __init__(self, stop_at: int, raises: bool = True):
        self._stopped = False
        self.starts = 0
        self._stop_at = stop_at
        self._raises = raises

    def start(self):
        self.starts += 1
        if self.starts >= self._stop_at:
            self._stopped = True  # 模拟外部 close() 主动停
        if self._raises:
            raise RuntimeError("ws died")


def _run(client):
    dead = []
    with patch.object(feishu_ws.time, "sleep"), patch.object(feishu_ws.random, "uniform", return_value=0.0):
        feishu_ws._run_ws_blocking(client, lambda: dead.append(1))
    return dead


def test_reconnect_retries_until_stopped():
    # start 连续抛异常,到第3次时被标记主动停 → 共重连到3次、不永久放弃
    c = _FakeClient(stop_at=3)
    dead = _run(c)
    assert c.starts == 3        # 死2次都重连了(没有死一次就退出)
    assert dead == [1]          # 主动停后 on_dead 恰好一次


def test_no_start_when_already_stopped():
    c = _FakeClient(stop_at=1)
    c._stopped = True
    dead = _run(c)
    assert c.starts == 0        # 已停:压根不连
    assert dead == [1]


def test_clean_return_also_reconnects():
    # start() 正常返回(不抛)也当断线重连,直到主动停
    c = _FakeClient(stop_at=2, raises=False)
    dead = _run(c)
    assert c.starts == 2
    assert dead == [1]


def test_backoff_grows_between_retries():
    c = _FakeClient(stop_at=4)
    waits = []
    with patch.object(feishu_ws.time, "sleep", side_effect=lambda w: waits.append(w)), \
         patch.object(feishu_ws.random, "uniform", return_value=0.0):
        feishu_ws._run_ws_blocking(c, lambda: None)
    # 3 次重连等待应指数增长(1,2,4...)且各不减
    assert len(waits) == 3
    assert waits == sorted(waits)
    assert waits[0] < waits[-1]


def test_lark_processor_not_found_filter_drops_only_noise():
    import logging

    f = feishu_ws._LarkProcessorNotFoundFilter()

    def _rec(msg):
        return logging.LogRecord("Lark", logging.ERROR, "", 0, msg, None, None)

    assert f.filter(_rec("handle message failed ... err: processor not found, type: ...")) is False
    assert f.filter(_rec("connect failed: app_id or app_secret is null")) is True  # 真错误保留


def test_suppress_lark_noise_is_idempotent():
    import logging

    lark_logger = logging.getLogger("Lark")
    lark_logger.filters = [x for x in lark_logger.filters if not isinstance(x, feishu_ws._LarkProcessorNotFoundFilter)]
    feishu_ws._lark_noise_filtered = False
    feishu_ws._suppress_lark_processor_not_found()
    feishu_ws._suppress_lark_processor_not_found()  # 第二次应空操作
    cnt = sum(1 for x in lark_logger.filters if isinstance(x, feishu_ws._LarkProcessorNotFoundFilter))
    assert cnt == 1
