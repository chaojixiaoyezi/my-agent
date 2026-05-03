"""LLM: 通道管理器 — 统一管理所有外部通道适配器，负责注册、启停和消息路由。

给人看的解释：
ChannelManager 像一个"通道控制面板"，它知道有哪些适配器，
收到消息后把请求转发给 gateway，处理完再把结果发回用户。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from .base import BaseChannelAdapter
from .protocol import IncomingMessage, OutgoingMessage

logger = logging.getLogger(__name__)


class ChannelManager:
    """管理所有已注册的通道适配器，提供统一的启停和消息路由接口。"""

    def __init__(self, gateway_port: int = 8420) -> None:
        self._adapters: dict[str, BaseChannelAdapter] = {}
        self.gateway_port = gateway_port
        self._session_channel_file: Path | None = None  # 用于持久化活跃通道

    @property
    def session_channel_file(self) -> Path | None:
        """返回活跃通道存储文件路径。"""
        return self._session_channel_file

    @session_channel_file.setter
    def session_channel_file(self, path: Path | None) -> None:
        """设置活跃通道存储文件路径（供测试和外部注入）。"""
        self._session_channel_file = path

    # -------------------------------------------------------------------------
    # 适配器注册
    # -------------------------------------------------------------------------

    def register_adapter(self, adapter: BaseChannelAdapter) -> None:
        """注册一个通道适配器。"""
        name = adapter.adapter_name
        if name in self._adapters:
            logger.warning(f"适配器 {name} 已注册，将被替换")
        self._adapters[name] = adapter
        logger.info(f"已注册通道适配器: {name}")

    def get_adapter(self, name: str) -> BaseChannelAdapter | None:
        """获取指定名称的适配器。"""
        return self._adapters.get(name)

    def list_adapters(self) -> list[str]:
        """列出所有已注册的适配器名称。"""
        return list(self._adapters.keys())

    # -------------------------------------------------------------------------
    # 启停
    # -------------------------------------------------------------------------

    def start_all(self) -> None:
        """启动所有已注册的适配器。"""
        for adapter in self._adapters.values():
            if not adapter.running:
                try:
                    adapter.start()
                except Exception as exc:
                    logger.error(f"启动适配器 {adapter.adapter_name} 失败: {exc}")

    def stop_all(self) -> None:
        """停止所有已注册的适配器。"""
        for adapter in self._adapters.values():
            if adapter.running:
                try:
                    adapter.stop()
                except Exception as exc:
                    logger.error(f"停止适配器 {adapter.adapter_name} 失败: {exc}")

    # -------------------------------------------------------------------------
    # 消息路由
    # -------------------------------------------------------------------------

    def route_message(self, msg: IncomingMessage) -> bool:
        """把外部消息路由到 gateway（POST /ask），异步等待结果并回复用户。"""
        import urllib.error
        import urllib.request

        try:
            # 构造 gateway ask 请求
            ask_payload = {
                "kind": "ask",
                "prompt": msg.content,
                "metadata": {
                    "channel": msg.channel,
                    "user_id": msg.user_id,
                    "message_id": msg.message_id,
                    "adapter": msg.channel,
                },
            }

            url = f"http://127.0.0.1:{self.gateway_port}/ask"
            data = json.dumps(ask_payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
            )

            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            request_id = result.get("request_id", "")
            if not request_id:
                logger.error(f"gateway /ask 未返回 request_id: {result}")
                return False

            # 轮询结果
            response_text = self._poll_gateway_result(request_id)

            # 构造回复消息
            outgoing = OutgoingMessage(
                channel=msg.channel,
                user_id=msg.user_id,
                content=response_text,
                format="text",
                metadata={"gateway_request_id": request_id},
            )

            # 通过对应适配器发送回复
            adapter = self._adapters.get(msg.channel)
            if adapter:
                ok = adapter.send_message(msg.user_id, outgoing)
                if ok:
                    self._update_active_channel(msg.user_id, msg.channel)
                return ok
            else:
                logger.error(f"找不到 channel={msg.channel} 的适配器")
                return False

        except urllib.error.URLError as exc:
            logger.error(f"gateway 请求失败: {exc}")
            return False
        except Exception as exc:
            logger.error(f"route_message 异常: {exc}")
            return False

    def _poll_gateway_result(self, request_id: str, timeout: float = 60.0, interval: float = 1.0) -> str:
        """轮询 gateway /result/<id> 直到拿到结果或超时。"""
        import urllib.error
        import urllib.request

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                url = f"http://127.0.0.1:{self.gateway_port}/result/{request_id}"
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    body = json.loads(resp.read().decode("utf-8"))

                if resp.status == 200:
                    if body.get("ok"):
                        return body.get("response", "")
                    elif "error" in body:
                        return f"错误: {body.get('error', 'unknown')}"
                elif resp.status == 404:
                    time.sleep(interval)
                    continue
                else:
                    time.sleep(interval)

            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    time.sleep(interval)
                    continue
                return f"HTTP 错误: {exc.code}"
            except Exception:
                time.sleep(interval)
                continue

        return "gateway 响应超时"

    # -------------------------------------------------------------------------
    # 活跃通道查询
    # -------------------------------------------------------------------------

    def get_active_channel(self, user_id: str) -> str | None:
        """查询用户当前活跃的通道。"""
        if self._session_channel_file is None:
            return None
        try:
            if not self._session_channel_file.exists():
                return None
            data = json.loads(self._session_channel_file.read_text(encoding="utf-8"))
            return data.get(user_id)
        except (json.JSONDecodeError, OSError):
            return None

    def _update_active_channel(self, user_id: str, channel: str) -> None:
        """更新用户当前活跃通道到本地文件。"""

        if self._session_channel_file is None:
            return
        try:
            data = {}
            if self._session_channel_file.exists():
                data = json.loads(self._session_channel_file.read_text(encoding="utf-8"))
            data[user_id] = channel
            self._session_channel_file.parent.mkdir(parents=True, exist_ok=True)
            self._session_channel_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"更新活跃通道失败: {exc}")

    def update_active_channel(self, user_id: str, channel: str) -> None:
        """公开的更新活跃通道方法。"""
        self._update_active_channel(user_id, channel)
