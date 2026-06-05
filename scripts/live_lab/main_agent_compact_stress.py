from __future__ import annotations

"""real-LLM compact stress cases for the main agent."""

import json
import os
import textwrap
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

DEFAULT_STRESS_SIZE_MB = 10
SECTION_COUNT = 80
REQUIRED_COMPACT_CYCLES = 5


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
    _disable_shell_tool_for_compact_stress(lab)
    facts = _stress_facts()
    source = lab.fixture_root / "data" / "long_field_journal.txt"
    _seed_stress_file(source, facts, size_mb=_stress_size_mb())
    prompt = _compact_stress_prompt()
    contract_path = _compact_stress_delivery_contract_path(lab, facts)
    lab.record_prompt("main_compact_stress_long_read", prompt)
    response = lab.run_command(
        lab.agent_command("run", prompt, "--delivery-contract-file", str(contract_path), "--save"),
        timeout=lab.args.timeout + 900,
    )
    (lab.responses_dir / "main_compact_stress_long_read.stdout.txt").write_text(response.stdout, encoding="utf-8")
    report = lab.fixture_root / "lab_outputs" / "compact-stress" / "report.md"
    _assert_stress_report(lab, report, facts)
    compact_count = _compact_cycle_count(lab.fixture_root / ".my_agent" / "home")
    if compact_count < REQUIRED_COMPACT_CYCLES:
        raise RuntimeError(f"compact 次数不足：expected>={REQUIRED_COMPACT_CYCLES}, actual={compact_count}")
    lab.log(f"compact_stress_report={report}")
    lab.log(f"compact_stress_compact_cycles={compact_count}")


def _compact_stress_prompt() -> str:
    return textwrap.dedent(
        """
        data/long_field_journal.txt 是一个很大的现场记录。请完整读完，按顺序慢慢读，不要只看局部。
        文件很长，可以分段读取；读到提示里的下一段位置后就从那里继续，不要回头重读。
        读的时候自己保留必要的过程记录；每发现一个章节，就记下章节号、地点、最终决定、风险词、检查点编号。
        最终报告必须基于实际读到的记录整理，不要最后只凭印象补章节。
        读的时候自己分段归纳，不要把原文全文复制到回复里。

        最终把报告写到 lab_outputs/compact-stress/report.md。
        报告里要包含：总体结论；每个章节的地点、最终决定和风险；最后 10 个检查点编号和一句证据。
        """
    ).strip()


def _seed_stress_file(path: Path, facts: list[StressFact], *, size_mb: int) -> None:
    target_bytes = max(1, int(size_mb)) * 1024 * 1024
    if path.exists() and path.stat().st_size >= target_bytes:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_stress_file_text(facts, target_bytes=target_bytes), encoding="utf-8")


def _stress_file_text(facts: list[StressFact], *, target_bytes: int) -> str:
    filler = _filler_sentence()
    section_budget = max(1200, target_bytes // max(1, len(facts)))
    chunks: list[str] = []
    for fact in facts:
        body = _section_body(fact)
        while len(body.encode("utf-8")) < section_budget:
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
    return [
        StressFact(
            section=index,
            city=_fact_token("地点", index, 0),
            decision=_fact_token("决定", index, 1),
            risk=_fact_token("风险", index, 2),
            checkpoint=f"CP-{index:03d}-{_fact_digest(index)[18:26].upper()}",
        )
        for index in range(1, SECTION_COUNT + 1)
    ]


def _fact_token(prefix: str, index: int, slot: int) -> str:
    digest = _fact_digest(index)
    start = slot * 6
    return f"{prefix}-{digest[start:start + 6].upper()}"


def _fact_digest(index: int) -> str:
    return sha256(f"compact-stress-fact:{index}".encode()).hexdigest()


def _assert_stress_report(lab, report: Path, facts: list[StressFact]) -> None:
    del lab
    if not report.exists():
        raise RuntimeError(f"compact stress 报告不存在: {report}")
    content = report.read_text(encoding="utf-8", errors="replace")
    if len(content.strip()) < 3000:
        raise RuntimeError("compact stress 报告过短，不足以证明完整阅读。")
    _assert_report_required_facts(content, facts)


def _assert_report_required_facts(content: str, facts: list[StressFact]) -> None:
    lowered = content.casefold()
    checkpoints = [fact.checkpoint for fact in facts[-10:]]
    required = [*checkpoints, "最终决定"]
    missing = [item for item in required if item.casefold() not in lowered]
    missing.extend(_missing_section_facts(lowered, facts, include_checkpoint=True))
    if missing:
        raise RuntimeError(f"compact stress 报告缺少关键事实: {missing}")


def _compact_stress_delivery_contract_path(lab, facts: list[StressFact]) -> Path:
    path = lab.run_root / "compact_stress_delivery_contract.json"
    path.write_text(
        json.dumps(
            _compact_stress_delivery_contract(facts),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    lab.log(f"delivery_contract_file={path}")
    return path


def _compact_stress_delivery_contract(facts: list[StressFact]) -> dict[str, object]:
    required_strings = ["最终决定"]
    for fact in facts:
        required_strings.extend([f"{fact.section:03d}", fact.city, fact.decision, fact.risk, fact.checkpoint])
    return {
        "schema_version": "delivery_contract.v1",
        "artifacts": [
            {
                "artifact_id": "compact_stress_report",
                "kind": "md",
                "preferred_path": "lab_outputs/compact-stress/report.md",
                "allowed_output_roots": ["lab_outputs/compact-stress"],
                "required": True,
                "validation_contract": {
                    "required_strings": list(dict.fromkeys(required_strings)),
                    "forbidden_strings": ["见原文"],
                },
            }
        ],
    }


def _missing_section_facts(
    lowered_content: str,
    facts: list[StressFact],
    *,
    include_checkpoint: bool,
) -> list[str]:
    missing: list[str] = []
    for fact in facts:
        section = f"{fact.section:03d}"
        values = [section, fact.city, fact.decision, fact.risk]
        if include_checkpoint:
            values.append(fact.checkpoint)
        for value in values:
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
        tool_read_max_chars: 100000
        tool_output_externalize_min_chars: 140000
        tool_output_preview_chars: 8000
        """
    )
    lab.config_path.write_text(text + overrides, encoding="utf-8")


def _disable_shell_tool_for_compact_stress(lab) -> None:
    policy_path = lab.fixture_root / ".my_agent" / "home" / "owners" / "local" / "main" / "tool_policy.json"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": "tool-policy.v1",
                "enabled_sources": ["builtin", "owner", "workspace", "shared"],
                "disabled_tools": ["run_command"],
                "pin_versions": {},
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    lab.log(f"compact_stress_disabled_tools={policy_path}")


def _stress_size_mb() -> int:
    raw = os.environ.get("MY_AGENT_COMPACT_STRESS_SIZE_MB", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_STRESS_SIZE_MB
    return max(1, value)


__all__ = ["case_main_compact_stress_long_read"]
