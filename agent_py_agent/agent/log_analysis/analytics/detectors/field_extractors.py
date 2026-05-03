"""LLM: Field extractors that pull specific named values from raw event dicts.

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

def _source_product(event: Mapping[str, Any]) -> str:
    """LLM: Extract the source/product identifier from *event*.

    新手说明:
    获取事件来源产品名称（如 waf、edr）。
    """
    return _text(_field(event, "source_product", "product", "source", "source_id")).lower()


def _event_class(event: Mapping[str, Any]) -> str:
    """LLM: Extract the event class/category from *event*.

    新手说明:
    获取事件类别（如 alert、auth、network）。
    """
    return _text(_field(event, "event_class", "class", "category", "type", "event_type")).lower()


def _event_action(event: Mapping[str, Any]) -> str:
    """LLM: Extract the action/operation from *event*.

    新手说明:
    获取事件动作（如 exec、connect、login）。
    """
    return _text(_field(event, "event_action", "action", "operation")).lower()


def _outcome(event: Mapping[str, Any]) -> str:
    """LLM: Extract the outcome/result from *event*.

    新手说明:
    获取事件结果（如 success、failure）。
    """
    return _text(_field(event, "event_outcome", "outcome", "result", "auth_result", "login_result")).lower()


def _severity(event: Mapping[str, Any]) -> str:
    """LLM: Extract the severity/level from *event*.

    新手说明:
    获取事件严重级别（如 high、critical、medium）。
    """
    return _text(_field(event, "severity", "severity_hint", "level", "risk_level")).lower()


def _source_ip(event: Mapping[str, Any]) -> str:
    """LLM: Extract the source/attacker IP from *event*.

    新手说明:
    获取源 IP（攻击者 IP）。
    """
    return _text(_field(event, "attacker_ip", "src_ip", "source_ip", "client_ip", "remote_ip", "source.ip"))


def _destination_ip(event: Mapping[str, Any]) -> str:
    """LLM: Extract the destination IP from *event*.

    新手说明:
    获取目标 IP。
    """
    return _text(_field(event, "dst_ip", "destination_ip", "dest_ip", "destination.ip"))


def _victim_ip(event: Mapping[str, Any]) -> str:
    """LLM: Extract the victim/asset IP from *event*.

    新手说明:
    获取受害者 IP（受影响资产 IP）。
    """
    return _text(_field(event, "victim_ip", "asset_ip", "host_ip", "dst_ip", "destination_ip"))


def _asset_ip(event: Mapping[str, Any]) -> str:
    """LLM: Extract the asset IP from *event* (prefers victim/asset fields).

    新手说明:
    获取资产 IP，优先取 victim_ip/asset_ip 字段。
    """
    return _text(_field(event, "victim_ip", "asset_ip", "host_ip", "src_ip", "dst_ip"))


def _host(event: Mapping[str, Any]) -> str:
    """LLM: Extract the hostname/asset-id from *event*.

    新手说明:
    获取主机名或资产标识。
    """
    return _text(_field(event, "host", "hostname", "asset_id", "device_name", "computer_name"))


def _user(event: Mapping[str, Any]) -> str:
    """LLM: Extract the username/principal from *event*.

    新手说明:
    获取用户名。
    """
    return _text(_field(event, "user", "username", "account", "principal", "user.name"))


def _domain(event: Mapping[str, Any]) -> str:
    """LLM: Extract the DNS domain/SNI from *event*.

    新手说明:
    获取域名或 DNS 查询字段。
    """
    return _text(_field(event, "domain", "dns_query", "query", "host_header", "sni", "network.domain"))


def _dst_port(event: Mapping[str, Any]) -> int | None:
    """LLM: Extract the destination port from *event*.

    新手说明:
    获取目标端口号。
    """
    return _to_int(_field(event, "dst_port", "destination_port", "port", "network.dst_port"))


def _process_name(event: Mapping[str, Any]) -> str:
    """LLM: Extract the process image basename from *event*.

    新手说明:
    获取进程名（只取文件名部分）。
    """
    return _basename(_field(event, "process.name", "process_name", "image", "process", "child_process"))


def _parent_process_name(event: Mapping[str, Any]) -> str:
    """LLM: Extract the parent-process image basename from *event*.

    新手说明:
    获取父进程名（只取文件名部分）。
    """
    return _basename(_field(event, "process.parent_name", "parent_process_name", "parent_process", "parent.name"))


def _cmdline(event: Mapping[str, Any]) -> str:
    """LLM: Extract the command-line string from *event*.

    新手说明:
    获取命令行内容。
    """
    return _text(_field(event, "process.cmdline", "cmdline", "command_line", "process_command_line"))
