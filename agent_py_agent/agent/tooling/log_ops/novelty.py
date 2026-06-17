
from __future__ import annotations

"""新颖性检测 —— 海量候选下,弱模型会把"新攻击者"当成"已知模式"坍缩漏报(4h 真机实锤:注入 30 起
新攻击者事件,采集层抓到 22 起,但 LLM 全判'已知 P0 类型重复爆发'→ 0 上报)。

对抗办法:采集/读取层标记"首次出现的攻击者 IOC",alert_poll 把"本批新攻击者"单独高亮——先得能
发现"新的",才谈得上研判+上报。攻击者 IOC = 外部 IPv4(排除内网/环回) + 明显恶意域名。已见集落盘
跨 poll/跨 run 持久,新 IOC 只在首次出现那一批被标 novel。
"""

import re
from typing import Any

from ...common.json_io import read_json_object, write_json_file_atomic
from .store import LogOpsStore

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# 内网/环回/保留段不算"攻击者来源"(攻击者看外部 IP)。
_INTERNAL = re.compile(r"^(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|127\.|0\.|169\.254\.|255\.)")
_BAD_DOMAIN = re.compile(r"\b[a-z0-9.-]*(?:exfil|attacker|malware|botnet|\.bad\.|c2\.)[a-z0-9.-]*\b", re.I)


def _valid_ipv4(ip: str) -> bool:
    parts = ip.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def extract_attacker_iocs(raw_line: str) -> list[str]:
    """从一条日志行提取攻击者 IOC:外部 IP(非内网/环回) + 可疑恶意域名。内网 IP 不算攻击者来源。"""
    iocs: set[str] = set()
    for ip in _IPV4.findall(raw_line):
        if _valid_ipv4(ip) and not _INTERNAL.match(ip):
            iocs.add(ip)
    iocs.update(d.lower() for d in _BAD_DOMAIN.findall(raw_line))
    return sorted(iocs)


def read_seen(store: LogOpsStore) -> set[str]:
    """已见攻击者 IOC 集(跨 poll/跨 run 持久)。"""
    data = read_json_object(store.seen_iocs_path)
    raw = data.get("iocs", []) if isinstance(data, dict) else []
    return {str(x) for x in raw}


def _mark_one(cand: dict[str, Any], seen: set[str], novel_set: set[str], novel_order: list[str]) -> None:
    """就地标记单条候选的攻击者 IOC 和新颖性;新出现的 IOC 累进 novel_set/novel_order(保序去重)。"""
    cand_iocs = extract_attacker_iocs(str(cand.get("raw_line", "")))
    cand["iocs"] = cand_iocs
    fresh = [i for i in cand_iocs if i not in seen and i not in novel_set]
    cand["novel"] = bool(fresh)
    if fresh:
        cand["novel_iocs"] = fresh
        for ioc in fresh:
            novel_set.add(ioc)
            novel_order.append(ioc)


def annotate_novelty(store: LogOpsStore, candidates: list[dict[str, Any]], *, persist: bool = True) -> dict[str, Any]:
    """给一批候选标新颖性:提取每条攻击者 IOC,对比已见集,标出本批首次出现的攻击者(就地改候选 dict)。
    persist=True 时把本批新 IOC 并入已见集(下次同 IOC 不再算新)。返回 {novel_attackers(保序), novel_count}。"""
    seen = read_seen(store)
    novel_order: list[str] = []
    novel_set: set[str] = set()
    for cand in candidates:
        _mark_one(cand, seen, novel_set, novel_order)
    if persist and novel_set:
        store.ensure_dirs()
        write_json_file_atomic(store.seen_iocs_path, {"iocs": sorted(seen | novel_set)})
    return {"novel_attackers": novel_order, "novel_count": len(novel_order)}


__all__ = ["extract_attacker_iocs", "read_seen", "annotate_novelty"]
