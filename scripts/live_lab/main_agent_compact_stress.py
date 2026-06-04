from __future__ import annotations

"""real-LLM compact stress cases for the main agent."""

import json
import os
import textwrap
from dataclasses import dataclass
from pathlib import Path

DEFAULT_STRESS_SIZE_MB = 10
SECTION_COUNT = 80
REQUIRED_COMPACT_CYCLES = 20


@dataclass(frozen=True)
class StressFact:
    section: int
    city: str
    decision: str
    risk: str
    checkpoint: str


def case_main_compact_stress_long_read(lab) -> None:
    lab.section("CASE main_compact_stress_long_read")
    _ensure_compact_stress_config(lab)
    facts = _stress_facts()
    source = lab.fixture_root / "data" / "long_field_journal.txt"
    _seed_stress_file(source, facts, size_mb=_stress_size_mb())
    prompt = _compact_stress_prompt()
    lab.record_prompt("main_compact_stress_long_read", prompt)
    response = lab.run_command(
        lab.agent_command("run", prompt, "--save"),
        timeout=lab.args.timeout + 900,
    )
    (lab.responses_dir / "main_compact_stress_long_read.stdout.txt").write_text(response.stdout, encoding="utf-8")
    report = lab.fixture_root / "lab_outputs" / "compact-stress" / "report.md"
    _assert_stress_report(report, facts)
    compact_count = _compact_cycle_count(lab.fixture_root / ".my_agent" / "home")
    if compact_count < REQUIRED_COMPACT_CYCLES:
        raise RuntimeError(f"compact 次数不足：expected>={REQUIRED_COMPACT_CYCLES}, actual={compact_count}")
    lab.log(f"compact_stress_report={report}")
    lab.log(f"compact_stress_compact_cycles={compact_count}")


def _compact_stress_prompt() -> str:
    return textwrap.dedent(
        """
        这次你自己完成，不要派小傻妞。

        data/long_field_journal.txt 是一个很大的现场记录。请完整读完，按顺序慢慢读，不要只抽样，也不要只搜几个关键词。
        每次尽量读大段一点，比如一段约 5 万字；读到提示里的下一段位置后就从那里继续，不要回头重读。
        读的时候另外做一张章节草稿记录表，放在本轮任务的 work 里；每发现一个章节，就记下章节号、地点、最终决定、风险词、检查点编号。
        最终报告必须按这张记录表整理，不要最后只凭印象补章节。
        读的时候自己分段归纳，不要把原文全文复制到回复里。

        最终把报告写到 lab_outputs/compact-stress/report.md。
        报告里要包含：总体结论；每个章节的地点、最终决定和风险；最后 10 个检查点编号和一句证据。
        """
    ).strip()


def _seed_stress_file(path: Path, facts: list[StressFact], *, size_mb: int) -> None:
    target_chars = max(1, int(size_mb)) * 1024 * 1024
    if path.exists() and path.stat().st_size >= target_chars:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_stress_file_text(facts, target_chars=target_chars), encoding="utf-8")


def _stress_file_text(facts: list[StressFact], *, target_chars: int) -> str:
    filler = _filler_sentence()
    section_budget = max(1200, target_chars // max(1, len(facts)))
    chunks: list[str] = []
    for fact in facts:
        body = _section_body(fact)
        while len(body) < section_budget:
            body += filler
        chunks.append(body)
    return "\n".join(chunks)


def _section_body(fact: StressFact) -> str:
    return textwrap.dedent(
        f"""
        ===== 章节 {fact.section:03d} =====
        这一章记录的是 {fact.city} 的现场复盘。最终决定：{fact.decision}。
        需要特别留意的风险词：{fact.risk}。
        检查点编号：{fact.checkpoint}。这条编号只在本章出现一次，后续报告必须保留它。
        """
    ).strip() + "\n"


def _filler_sentence() -> str:
    return (
        "现场记录继续描述沟通顺序、人员交接、系统状态、观察口径、备注来源、"
        "以及不适合直接跳读的上下文；这些普通句子用于撑开真实长文本。\n"
    )


def _stress_facts() -> list[StressFact]:
    cities = ["北京", "南京", "西安", "杭州", "成都", "青岛", "厦门", "武汉"]
    decisions = ["继续观察", "改走人工复核", "暂停发布", "转交二线", "补充证据"]
    risks = ["付款超时", "库存漂移", "审批延迟", "重复派单", "状态回滚", "凭证缺口"]
    return [
        StressFact(
            section=index,
            city=cities[index % len(cities)],
            decision=decisions[index % len(decisions)],
            risk=risks[index % len(risks)],
            checkpoint=f"CP-{index:03d}-{(index * 37) % 997:03d}",
        )
        for index in range(1, SECTION_COUNT + 1)
    ]


def _assert_stress_report(report: Path, facts: list[StressFact]) -> None:
    if not report.exists():
        raise RuntimeError(f"compact stress 报告不存在: {report}")
    content = report.read_text(encoding="utf-8", errors="replace")
    if len(content.strip()) < 3000:
        raise RuntimeError("compact stress 报告过短，不足以证明完整阅读。")
    _assert_required_facts(content, facts)


def _assert_required_facts(content: str, facts: list[StressFact]) -> None:
    lowered = content.casefold()
    checkpoints = [fact.checkpoint for fact in facts[-10:]]
    required = [*checkpoints, "北京", "南京", "西安", "付款超时", "审批延迟", "最终决定"]
    missing = [item for item in required if item.casefold() not in lowered]
    missing.extend(_missing_section_facts(lowered, facts))
    if missing:
        raise RuntimeError(f"compact stress 报告缺少关键事实: {missing}")


def _missing_section_facts(lowered_content: str, facts: list[StressFact]) -> list[str]:
    missing: list[str] = []
    for fact in facts:
        section = f"{fact.section:03d}"
        for value in (section, fact.city, fact.decision, fact.risk, fact.checkpoint):
            if value.casefold() not in lowered_content:
                missing.append(f"section_{section}:{value}")
    return missing


def _compact_cycle_count(home: Path) -> int:
    apply_ids: set[str] = set()
    for ledger in home.glob("**/memory_archive/**/compact_applies/ledger.jsonl"):
        apply_ids.update(_jsonl_apply_ids(ledger))
    return len(apply_ids)


def _jsonl_apply_ids(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    apply_ids: set[str] = set()
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        apply_id = str(payload.get("apply_id") or payload.get("event_id") or "").strip()
        if apply_id:
            apply_ids.add(apply_id)
    return apply_ids


def _ensure_compact_stress_config(lab) -> None:
    marker = "# compact-stress overrides"
    text = lab.config_path.read_text(encoding="utf-8")
    if marker in text:
        return
    overrides = textwrap.dedent(
        f"""

        {marker}
        enable_subagents: false
        max_tool_rounds: 0
        max_tool_calls_per_round:
        model_context_window_tokens: 200000
        memory_compact_auto_trigger_percent: 50
        request_timeout: {max(600, int(lab.args.timeout))}
        tool_read_max_chars: 50000
        """
    )
    lab.config_path.write_text(text + overrides, encoding="utf-8")


def _stress_size_mb() -> int:
    raw = os.environ.get("MY_AGENT_COMPACT_STRESS_SIZE_MB", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_STRESS_SIZE_MB
    return max(1, value)


__all__ = ["case_main_compact_stress_long_read"]
