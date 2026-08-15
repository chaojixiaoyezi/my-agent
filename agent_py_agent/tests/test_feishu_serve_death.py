"""审计 #15 修复真测:飞书回调服务器异常退出不再被 except:pass 静默吞,通道死亡可被感知。

原问题:_serve 的 serve_forever 抛异常被 except:pass 吞掉、_running 仍 True → 通道无声死亡而 running/状态检查
误判在线,用户消息长时间无响应却无人察觉。修复后:记录异常 + 置 _running=False,running 反映真实死亡可触发重启。
"""

from __future__ import annotations

import logging

from agent_py_agent.agent.adapter.feishu import FeishuAdapter


def _adapter() -> FeishuAdapter:
    return FeishuAdapter(
        config={"feishu_app_id": "a", "feishu_app_secret": "s", "feishu_verification_token": "t"},
        callback_port=8421,
    )


class _BoomServer:
    def serve_forever(self) -> None:
        raise RuntimeError("socket died")


def test_serve_exception_marks_channel_dead(caplog) -> None:
    adapter = _adapter()
    adapter._running = True
    adapter._server = _BoomServer()
    with caplog.at_level(logging.ERROR):
        adapter._serve()  # serve_forever 抛异常,被捕获
    assert adapter._running is False  # 通道死亡被感知(不再误报在线)
    assert any("异常退出" in r.getMessage() or "socket died" in r.getMessage() for r in caplog.records)


def test_running_reflects_death(caplog) -> None:
    adapter = _adapter()
    adapter._running = True
    adapter._server = _BoomServer()
    with caplog.at_level(logging.ERROR):
        adapter._serve()
    running = adapter.running() if callable(getattr(type(adapter), "running", None)) and not isinstance(
        getattr(type(adapter), "running", None), property
    ) else adapter.running
    assert running is False  # running 属性/方法反映真实死亡,健康探测/supervisor 可据此重启
