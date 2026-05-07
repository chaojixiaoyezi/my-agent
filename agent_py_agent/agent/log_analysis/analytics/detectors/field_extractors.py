# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

"""Field extractors that pull specific named values from raw event dicts.

给人看的解释：
本模块提供一组"取值函数"，每个函数从事件字典中提取一个特定字段
（如 source_ip、user、domain 等），并做标准化处理。
依赖 field_access.py 中的低级工具函数（_field、_text、_to_int、_basename）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .field_access import _basename, _field, _text, _to_int

# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------

# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _source_product 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 source product 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _source_product(event: Mapping[str, Any]) -> str:
    """Extract the source/product identifier from *event*.

    新手说明:
    获取事件来源产品名称（如 waf、edr）。"""
    return _text(_field(event, "source_product", "product", "source", "source_id")).lower()


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _event_class 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 event class 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _event_class(event: Mapping[str, Any]) -> str:
    """Extract the event class/category from *event*.

    新手说明:
    获取事件类别（如 alert、auth、network）。"""
    return _text(_field(event, "event_class", "class", "category", "type", "event_type")).lower()


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _event_action 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 event action 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _event_action(event: Mapping[str, Any]) -> str:
    """Extract the action/operation from *event*.

    新手说明:
    获取事件动作（如 exec、connect、login）。"""
    return _text(_field(event, "event_action", "action", "operation")).lower()


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _outcome 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 outcome 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _outcome(event: Mapping[str, Any]) -> str:
    """Extract the outcome/result from *event*.

    新手说明:
    获取事件结果（如 success、failure）。"""
    return _text(_field(event, "event_outcome", "outcome", "result", "auth_result", "login_result")).lower()


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _severity 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 severity 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _severity(event: Mapping[str, Any]) -> str:
    """Extract the severity/level from *event*.

    新手说明:
    获取事件严重级别（如 high、critical、medium）。"""
    return _text(_field(event, "severity", "severity_hint", "level", "risk_level")).lower()


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _source_ip 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 source ip 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _source_ip(event: Mapping[str, Any]) -> str:
    """Extract the source/attacker IP from *event*.

    新手说明:
    获取源 IP（攻击者 IP）。"""
    return _text(_field(event, "attacker_ip", "src_ip", "source_ip", "client_ip", "remote_ip", "source.ip"))


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _destination_ip 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 destination ip 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _destination_ip(event: Mapping[str, Any]) -> str:
    """Extract the destination IP from *event*.

    新手说明:
    获取目标 IP。"""
    return _text(_field(event, "dst_ip", "destination_ip", "dest_ip", "destination.ip"))


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _victim_ip 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 victim ip 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _victim_ip(event: Mapping[str, Any]) -> str:
    """Extract the victim/asset IP from *event*.

    新手说明:
    获取受害者 IP（受影响资产 IP）。"""
    return _text(_field(event, "victim_ip", "asset_ip", "host_ip", "dst_ip", "destination_ip"))


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _asset_ip 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 asset ip 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _asset_ip(event: Mapping[str, Any]) -> str:
    """Extract the asset IP from *event* (prefers victim/asset fields).

    新手说明:
    获取资产 IP，优先取 victim_ip/asset_ip 字段。"""
    return _text(_field(event, "victim_ip", "asset_ip", "host_ip", "src_ip", "dst_ip"))


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _host 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 host 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _host(event: Mapping[str, Any]) -> str:
    """Extract the hostname/asset-id from *event*.

    新手说明:
    获取主机名或资产标识。"""
    return _text(_field(event, "host", "hostname", "asset_id", "device_name", "computer_name"))


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _user 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 user 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _user(event: Mapping[str, Any]) -> str:
    """Extract the username/principal from *event*.

    新手说明:
    获取用户名。"""
    return _text(_field(event, "user", "username", "account", "principal", "user.name"))


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _domain 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 domain 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _domain(event: Mapping[str, Any]) -> str:
    """Extract the DNS domain/SNI from *event*.

    新手说明:
    获取域名或 DNS 查询字段。"""
    return _text(_field(event, "domain", "dns_query", "query", "host_header", "sni", "network.domain"))


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _dst_port 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 dst port 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _dst_port(event: Mapping[str, Any]) -> int | None:
    """Extract the destination port from *event*.

    新手说明:
    获取目标端口号。"""
    return _to_int(_field(event, "dst_port", "destination_port", "port", "network.dst_port"))


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _process_name 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 推进 process name 对应的调度、执行或处理步骤，并返回可追踪的状态结果。
def _process_name(event: Mapping[str, Any]) -> str:
    """Extract the process image basename from *event*.

    新手说明:
    获取进程名（只取文件名部分）。"""
    return _basename(_field(event, "process.name", "process_name", "image", "process", "child_process"))


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _parent_process_name 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 parent process name 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _parent_process_name(event: Mapping[str, Any]) -> str:
    """Extract the parent-process image basename from *event*.

    新手说明:
    获取父进程名（只取文件名部分）。"""
    return _basename(_field(event, "process.parent_name", "parent_process_name", "parent_process", "parent.name"))


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _cmdline 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 cmdline 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _cmdline(event: Mapping[str, Any]) -> str:
    """Extract the command-line string from *event*.

    新手说明:
    获取命令行内容。"""
    return _text(_field(event, "process.cmdline", "cmdline", "command_line", "process_command_line"))
