# LLM: External adapter module; keep platform payload and runtime boundary contracts stable.
# 模块用途: 对接 QQ、飞书等外部渠道，把平台事件转换成内部请求。


from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from .base import BaseChannelAdapter
from .protocol import IncomingMessage, OutgoingMessage

logger = logging.getLogger(__name__)


# LLM: _gateway_ask_payload 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 处理网关ask载荷相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _gateway_ask_payload(msg: IncomingMessage) -> dict[str, object]:
    return {
        "kind": "ask",
        "prompt": msg.content,
        "metadata": {
            "channel": msg.channel,
            "user_id": msg.user_id,
            "message_id": msg.message_id,
            "adapter": msg.channel,
        },
    }


# LLM: ChannelManager 属于外部通道适配的类边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 类用途: 协调通道管理器的下游服务和持久化入口，对外维持稳定管理接口；关键副作用: 方法可能触发通道配置、消息回调和平台输入输出相关副作用，需保持公开契约稳定。
class ChannelManager:
    """管理所有已注册的通道适配器，提供统一的启停和消息路由接口。"""

    # LLM: __init__ 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
    def __init__(self, gateway_port: int = 8420) -> None:
        self._adapters: dict[str, BaseChannelAdapter] = {}
        self.gateway_port = gateway_port
        self._session_channel_file: Path | None = None  # 用于持久化活跃通道

    # LLM: session_channel_file 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 处理会话通道文件相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
    @property
    def session_channel_file(self) -> Path | None:
        """返回活跃通道存储文件路径。"""
        return self._session_channel_file

    # LLM: session_channel_file 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 处理会话通道文件相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
    @session_channel_file.setter
    def session_channel_file(self, path: Path | None) -> None:
        """设置活跃通道存储文件路径（供测试和外部注入）。"""
        self._session_channel_file = path

    # -------------------------------------------------------------------------
    # 适配器注册
    # -------------------------------------------------------------------------

    # LLM: register_adapter 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 处理registeradapter相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
    def register_adapter(self, adapter: BaseChannelAdapter) -> None:
        """注册一个通道适配器。"""
        name = adapter.adapter_name
        if name in self._adapters:
            logger.warning(f"适配器 {name} 已注册，将被替换")
        self._adapters[name] = adapter
        logger.info(f"已注册通道适配器: {name}")

    # LLM: get_adapter 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 读取或查询adapter需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def get_adapter(self, name: str) -> BaseChannelAdapter | None:
        """获取指定名称的适配器。"""
        return self._adapters.get(name)

    # LLM: list_adapters 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 读取或查询adapters需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def list_adapters(self) -> list[str]:
        """列出所有已注册的适配器名称。"""
        return list(self._adapters.keys())

    # -------------------------------------------------------------------------
    # 启停
    # -------------------------------------------------------------------------

    # LLM: start_all 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 推进all的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响通道配置、消息回调和平台输入输出，需保持重试、超时和状态迁移语义。
    def start_all(self) -> None:
        """启动所有已注册的适配器。"""
        for adapter in self._adapters.values():
            if adapter.running:
                continue
            try:
                adapter.start()
            except Exception as exc:
                logger.error(f"启动适配器 {adapter.adapter_name} 失败: {exc}")

    # LLM: stop_all 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 推进all的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响通道配置、消息回调和平台输入输出，需保持重试、超时和状态迁移语义。
    def stop_all(self) -> None:
        """停止所有已注册的适配器。"""
        for adapter in self._adapters.values():
            if not adapter.running:
                continue
            try:
                adapter.stop()
            except Exception as exc:
                logger.error(f"停止适配器 {adapter.adapter_name} 失败: {exc}")

    # -------------------------------------------------------------------------
    # 消息路由
    # -------------------------------------------------------------------------

    # LLM: route_message 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 处理route消息相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
    def route_message(self, msg: IncomingMessage) -> bool:
        """把外部消息路由到 gateway（POST /ask），异步等待结果并回复用户。"""
        import urllib.error

        try:
            request_id = self._submit_gateway_ask(msg)
            if not request_id:
                return False
            response_text = self._poll_gateway_result(request_id)
            return self._send_gateway_reply(msg, request_id, response_text)
        except urllib.error.URLError as exc:
            logger.error(f"gateway 请求失败: {exc}")
            return False
        except Exception as exc:
            logger.error(f"route_message 异常: {exc}")
            return False

    # LLM: _submit_gateway_ask 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 发送网关ask请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def _submit_gateway_ask(self, msg: IncomingMessage) -> str:
        import urllib.request

        payload = _gateway_ask_payload(msg)
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.gateway_port}/ask",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        request_id = result.get("request_id", "")
        if not request_id:
            logger.error(f"gateway /ask 未返回 request_id: {result}")
        return request_id

    # LLM: _send_gateway_reply 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 发送网关reply请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def _send_gateway_reply(self, msg: IncomingMessage, request_id: str, response_text: str) -> bool:
        adapter = self._adapters.get(msg.channel)
        if adapter is None:
            logger.error(f"找不到 channel={msg.channel} 的适配器")
            return False
        outgoing = OutgoingMessage(
            channel=msg.channel,
            user_id=msg.user_id,
            content=response_text,
            format="text",
            metadata={"gateway_request_id": request_id},
        )
        ok = adapter.send_message(msg.user_id, outgoing)
        if ok:
            self._update_active_channel(msg.user_id, msg.channel)
        return ok

    # LLM: _poll_gateway_result 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 推进网关结果的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响通道配置、消息回调和平台输入输出，需保持重试、超时和状态迁移语义。
    def _poll_gateway_result(self, request_id: str, timeout: float = 60.0, interval: float = 1.0) -> str:
        """轮询 gateway /result/<id> 直到拿到结果或超时。"""
        import urllib.error
        import urllib.request

        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self._poll_gateway_once(request_id, interval)
            if result is not None:
                return result
        return "gateway 响应超时"

    # LLM: _poll_gateway_once 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 推进网关once的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响通道配置、消息回调和平台输入输出，需保持重试、超时和状态迁移语义。
    def _poll_gateway_once(self, request_id: str, interval: float) -> str | None:
        """Poll gateway once; return response string, error string, or None to retry."""
        import urllib.error
        import urllib.request

        try:
            url = f"http://127.0.0.1:{self.gateway_port}/result/{request_id}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if resp.status == 200 and body.get("ok"):
                return body.get("response", "")
            if resp.status == 200 and "error" in body:
                return f"错误: {body.get('error', 'unknown')}"
            time.sleep(interval)
            return None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                time.sleep(interval)
                return None
            return f"HTTP 错误: {exc.code}"
        except Exception:
            time.sleep(interval)
            return None

    # -------------------------------------------------------------------------
    # 活跃通道查询
    # -------------------------------------------------------------------------

    # LLM: get_active_channel 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 读取或查询active通道需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
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

    # LLM: _update_active_channel 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 更新active通道对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新通道配置、消息回调和平台输入输出，需避免破坏既有状态机约定。
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

    # LLM: update_active_channel 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 更新active通道对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新通道配置、消息回调和平台输入输出，需避免破坏既有状态机约定。
    def update_active_channel(self, user_id: str, channel: str) -> None:
        """公开的更新活跃通道方法。"""
        self._update_active_channel(user_id, channel)
