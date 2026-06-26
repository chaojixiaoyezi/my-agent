
from __future__ import annotations

"""list_capabilities —— 让 agent 发现自己的产品级内置能力,避免"不知道有"而造轮子。

设计要点(回应"加能力是否要改系统提示词"):**系统提示词不列具体能力,只指向本工具**;能力清单住在
这里(代码),其中**通道列表从 adapter 模块自动汇总**——加一个新 adapter,它自动出现在 list_capabilities
里,提示词和清单都不用改。这样安全纪律(提示词)与能力清单(注册表/代码)彻底解耦。
"""

import json
from typing import Any

from .models import BaseTool, ToolExecutionResult, ToolSpec


def _available_channels() -> list[str]:
    """自动从 adapter 模块汇总可用通道(凡有 adapter_name 的 BaseChannelAdapter 子类)。
    加新通道 adapter 自动出现,不用改这里、不用改系统提示词。"""
    try:
        from .. import adapter as _adapter_mod

        names = [
            name
            for attr in dir(_adapter_mod)
            if isinstance((name := getattr(getattr(_adapter_mod, attr, None), "adapter_name", None)), str)
            and name
            and name != "base"  # 排除抽象基类 BaseChannelAdapter,只留真实通道
        ]
        return sorted(set(names))
    except Exception:
        return ["feishu", "qq"]


def build_capability_inventory() -> dict[str, Any]:
    """my-agent 的产品级能力清单(通道自动汇总,能力区在代码维护——都不在系统提示词里)。"""
    return {
        "channels": _available_channels(),
        "capabilities": [
            {"area": "多通道网关", "what": "接入飞书/QQ/微信/Telegram 收发消息",
             "how": "my-agent adapter start --channel <名>(飞书支持长连接、免公网);**不要自己搭外部 webhook bot**"},
            {"area": "网关服务", "what": "常驻服务,把通道消息路由给主代理处理并回复用户",
             "how": "my-agent gateway start"},
            {"area": "多用户隔离", "what": "每个通道用户独立 home/记忆/数据/成本/审计(支撑几万十万用户)",
             "how": "配置 gateway_per_user_owner_scoping=true(每用户按 channel+user_id 自动得到独立 owner 作用域)"},
            {"area": "持久记忆", "what": "跨会话自动记忆/回忆,语义召回", "how": "记忆工具 + 自动注入;重要事实主动记下"},
            {"area": "人格定制", "what": "用户经对话改自己 agent 的身份画像(SOUL人设/USER画像/AGENTS规矩),每用户隔离、下条消息即生效",
             "how": "这三个文件的路径每轮已在你上下文的 # Home Entry 块里;用户要改设定时直接用 edit_file/write_file 改对应文件即可(不必单造工具),写入层会自动做注入扫描"},
            {"area": "子代理编排", "what": "大任务拆给多个子代理并行/协作完成", "how": "create_subagents / subagents dispatch"},
            {"area": "定时调度", "what": "周期性任务、长期值守", "how": "cron / 唤醒机制"},
            {"area": "浏览器/视觉", "what": "网页自动化(a11y 快照)、看图理解", "how": "浏览器工具 / analyze_image"},
        ],
        "discover_more": "更细命令用 `my-agent --help` 或 `my-agent <子命令> --help`;低层文件/命令工具用 list_tools。",
        "principle": "遇到产品级需求(接通道/搭网关/做监控/调度/多用户)先查这里,有内置就用内置——别造轮子、别接第三方大模型,你就是那个大脑。",
    }


class ListCapabilitiesTool(BaseTool):
    """列出 my-agent 自己的内置产品能力及调用方式,防止"不知道有"而造轮子。"""

    spec = ToolSpec(
        name="list_capabilities",
        category="meta",
        description=(
            "列出 my-agent 自己的内置产品能力(多通道网关、多用户隔离、持久记忆、子代理编排、定时调度等)"
            "及调用方式。遇到'接通道/搭网关/做调度/多用户'这类需求,**先调它查清自己有没有现成能力,别从零造轮子**。"
        ),
        use_cases=[
            "要接入飞书/QQ/微信等通道前,先查有没有内置 adapter",
            "要做定时任务/多用户隔离前,先查有没有内置能力",
            "不确定自己能做什么、有什么内置命令时",
        ],
        avoid_when=["只是要低层文件/命令/网络工具时,用 list_tools 即可"],
        keywords=["capabilities", "能力", "内置", "adapter", "通道", "channel", "gateway", "网关", "list_capabilities", "造轮子"],
        parameters={},
        parameter_schema={},
        examples=['{"tool": "list_capabilities"}'],
        effect="read_only",
        default_mode="real",
        requires_idempotency=False,
        requires_approval=False,
    )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        payload = build_capability_inventory()
        return ToolExecutionResult(
            self.spec.name,
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
            result_envelope=payload,
        )


__all__ = ["ListCapabilitiesTool", "build_capability_inventory"]
