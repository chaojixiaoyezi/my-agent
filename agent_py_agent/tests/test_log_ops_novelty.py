
from __future__ import annotations

"""新颖性检测单测 —— 海量候选下"新攻击者被当已知坍缩漏报"的对抗机制(4h真机实锤:注入30起新攻击者0上报)。"""

import json
from pathlib import Path

from agent_py_agent.agent.tooling.log_ops import novelty
from agent_py_agent.agent.tooling.log_ops.store import LogOpsStore, build_source_specs
from agent_py_agent.agent.tooling.log_ops.tools import LogAlertPollTool


def _store(tmp_path: Path) -> LogOpsStore:
    store = LogOpsStore(tmp_path / ".log_ops", "m")
    store.write_config(build_source_specs(["/a.log"]), poll_interval_seconds=2.0)
    return store


def test_extract_attacker_iocs_external_only() -> None:
    iocs = novelty.extract_attacker_iocs("sshd Failed password for root from 45.137.21.9 port 22; dst 10.0.0.5")
    assert "45.137.21.9" in iocs  # 外部 IP = 攻击者来源
    assert "10.0.0.5" not in iocs  # 内网不算攻击者
    assert "127.0.0.1" not in novelty.extract_attacker_iocs("loopback 127.0.0.1 noise")  # 环回排除


def test_extract_attacker_iocs_bad_domain() -> None:
    iocs = novelty.extract_attacker_iocs("dns query: a8f3e9b2.exfil.attacker.io IN TXT dns_tunneling")
    assert any("exfil" in i or "attacker" in i for i in iocs)


def test_annotate_first_seen_then_known(tmp_path: Path) -> None:
    store = _store(tmp_path)
    c1 = [{"raw_line": "attack from 45.137.21.9"}, {"raw_line": "attack from 91.219.236.10"}]
    info = novelty.annotate_novelty(store, c1)
    assert info["novel_count"] == 2
    assert set(info["novel_attackers"]) == {"45.137.21.9", "91.219.236.10"}
    assert c1[0]["novel"] is True and c1[0]["iocs"] == ["45.137.21.9"]
    # 第二批:45.137.21.9 已见(不新), 193.27.228.40 新
    c2 = [{"raw_line": "again 45.137.21.9"}, {"raw_line": "new 193.27.228.40"}]
    info2 = novelty.annotate_novelty(store, c2)
    assert info2["novel_count"] == 1 and info2["novel_attackers"] == ["193.27.228.40"]
    assert c2[0]["novel"] is False  # 已见攻击者不再标新
    assert c2[1]["novel"] is True


def test_annotate_peek_does_not_persist(tmp_path: Path) -> None:
    store = _store(tmp_path)
    novelty.annotate_novelty(store, [{"raw_line": "from 45.137.21.9"}], persist=False)  # peek 不入已见集
    info2 = novelty.annotate_novelty(store, [{"raw_line": "from 45.137.21.9"}])
    assert info2["novel_count"] == 1  # 上次 peek 没持久化 → 仍算新


def test_alert_poll_flags_novel_attackers(tmp_path: Path) -> None:
    ws = tmp_path
    store = _store(ws)
    store.append_candidates([
        {"raw_line": "ssh brute from 45.137.21.9", "severity": "high", "matched_rules": ["ssh_brute"]},
        {"raw_line": "internal noise from 10.0.0.5", "severity": "low", "matched_rules": []},
    ])
    res = json.loads(LogAlertPollTool(ws).execute({"monitor_id": "m"}).output)
    assert res["novel_attacker_count"] == 1  # 只有外部 45.x 算新攻击者,内网 10.x 不算
    assert "45.137.21.9" in res["novel_attackers"]
    assert "novelty_alert" in res  # 有新攻击者 → 高亮提示
    assert res["alerts"][0]["novel"] is True and res["alerts"][1]["novel"] is False


def test_alert_poll_prioritizes_novel_high(tmp_path: Path) -> None:
    """海量候选下,novel 攻击者 + 高危排到批次最前,不被普通候选埋(cursor 仍 FIFO 不重不漏)。"""
    ws = tmp_path
    store = _store(ws)
    store.append_candidates([
        {"raw_line": "normal from 10.0.0.5", "severity": "low", "matched_rules": []},
        {"raw_line": "ssh brute from 45.137.21.9", "severity": "high", "matched_rules": ["x"]},
        {"raw_line": "internal from 10.0.0.6", "severity": "medium", "matched_rules": []},
    ])
    res = json.loads(LogAlertPollTool(ws).execute({"monitor_id": "m"}).output)
    assert res["alerts"][0]["novel"] is True and "45.137.21.9" in res["alerts"][0]["raw_line"]  # novel高危排第一
