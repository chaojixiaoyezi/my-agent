from __future__ import annotations

"""real-LLM compact stress cases for the main agent."""

import json
import os
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from .compact_observations import (
    assert_isolated_home_path,
    collect_bg_process_census,
    collect_compaction_observations,
    resolve_live_lab_home_context,
    snapshot_compaction_baseline,
)

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
    home_ctx = _ensure_compact_stress_config(lab)
    _disable_shell_tool_for_compact_stress(lab, home_ctx=home_ctx)

    baseline = snapshot_compaction_baseline(
        compact_dir=home_ctx["owner_compact_dir"],
        threads_dir=home_ctx["threads_dir"],
    )
    pre_bg = collect_bg_process_census(home_ctx["process_store_root"])

    facts = _stress_facts()
    source = lab.fixture_root / "data" / "long_field_journal.txt"
    source_meta = _seed_stress_file(source, facts, size_mb=_stress_size_mb())

    report = lab.fixture_root / "lab_outputs" / "compact-stress" / "report.md"
    pre_report_stamp = _artifact_stamp(report)

    prompt = _compact_stress_prompt()
    contract_path = _compact_stress_delivery_contract_path(lab, facts)
    lab.record_prompt("main_compact_stress_long_read", prompt)
    response = lab.run_command(
        lab.agent_command("run", prompt, "--delivery-contract-file", str(contract_path), "--save"),
        timeout=lab.args.timeout + 900,
    )
    stdout_path = lab.responses_dir / "main_compact_stress_long_read.stdout.txt"
    stdout_path.write_text(response.stdout, encoding="utf-8")

    _assert_current_run_report_provenance(report, pre_report_stamp)
    _assert_stress_report(lab, report, facts)

    telemetry_sources: list[Any] = [response.stdout, response.stderr]
    turn_usage = list(getattr(lab, "turn_usage_records", ()))
    observations = collect_compaction_observations(
        config_path=lab.config_path,
        turn_usages=turn_usage,
        telemetry_sources=telemetry_sources,
        baseline=baseline,
        pre_bg_census=pre_bg,
    )
    observations["source_provenance"] = source_meta
    observations["report_provenance"] = _artifact_stamp(report)
    stress_gate = evaluate_compact_stress_observations(observations, min_cycles=REQUIRED_COMPACT_CYCLES)
    observations["stress_gate"] = stress_gate

    telemetry_path = lab.responses_dir / "main_compact_stress_long_read.telemetry.json"
    telemetry_path.write_text(json.dumps(observations, ensure_ascii=False, indent=2), encoding="utf-8")
    if hasattr(lab, "record_case_telemetry"):
        lab.record_case_telemetry("main_compact_stress_long_read", observations)

    if stress_gate["checkpoint_errors"]:
        raise RuntimeError(
            "COMPACT_CHECKPOINT_AUTHORITY_ERROR: canonical checkpoint verification failed: "
            f"{stress_gate['checkpoint_errors']}"
        )
    compact_count = int(stress_gate["successful_cycle_count"])
    if compact_count < REQUIRED_COMPACT_CYCLES:
        raise RuntimeError(
            f"COMPACT_CYCLES_INSUFFICIENT: expected>={REQUIRED_COMPACT_CYCLES} committed canonical generations "
            f"above baseline, actual={compact_count} (uncommitted_or_superseded="
            f"{stress_gate['uncommitted_or_superseded_count']})"
        )
    if stress_gate["runaway_no_progress_detected"]:
        raise RuntimeError(
            "COMPACT_NO_PROGRESS_LOOP: repeated non-progress compaction skip events detected: "
            f"count={observations['telemetry']['no_progress_count']}"
        )
    bg_diff = observations["bg_census_diff"]
    if bg_diff["leaked_count"] > 0:
        raise RuntimeError(
            f"BG_PROCESS_LEAK: non-settled bg-* sessions remained after run: {bg_diff['leaked_sessions']}"
        )

    lab.log(f"compact_stress_report={report}")
    lab.log(f"compact_stress_telemetry={telemetry_path}")
    lab.log(f"compact_stress_compact_cycles={compact_count}")


def evaluate_compact_stress_observations(
    observations: Mapping[str, Any],
    *,
    min_cycles: int = REQUIRED_COMPACT_CYCLES,
) -> dict[str, Any]:
    """Evaluate canonical checkpoints, loop telemetry, and bg-* census for compact stress."""
    checkpoints = observations.get("checkpoints") if isinstance(observations.get("checkpoints"), Mapping) else {}
    telemetry = observations.get("telemetry") if isinstance(observations.get("telemetry"), Mapping) else {}
    bg_diff = observations.get("bg_census_diff") if isinstance(observations.get("bg_census_diff"), Mapping) else {}

    checkpoint_errors = list(checkpoints.get("errors") or ())
    compact_count = int(checkpoints.get("successful_cycle_count") or 0)
    uncommitted_count = int(checkpoints.get("uncommitted_or_superseded_count") or 0)
    runaway_no_progress = bool(telemetry.get("runaway_no_progress_detected"))
    leaked_count = int(bg_diff.get("leaked_count") or 0)

    ok = (
        not checkpoint_errors
        and compact_count >= max(1, int(min_cycles))
        and not runaway_no_progress
        and leaked_count == 0
    )
    return {
        "ok": ok,
        "successful_cycle_count": compact_count,
        "uncommitted_or_superseded_count": uncommitted_count,
        "checkpoint_errors": checkpoint_errors,
        "runaway_no_progress_detected": runaway_no_progress,
        "leaked_count": leaked_count,
    }


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


def _seed_stress_file(path: Path, facts: list[StressFact], *, size_mb: int) -> dict[str, Any]:
    target_bytes = max(1, int(size_mb)) * 1024 * 1024
    expected_text = _stress_file_text(facts, target_bytes=target_bytes)
    expected_digest = sha256(expected_text.encode("utf-8")).hexdigest()
    reused = False
    if path.is_file() and path.stat().st_size >= target_bytes:
        actual_digest = _sha256_path(path)
        if actual_digest == expected_digest:
            reused = True
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected_text, encoding="utf-8")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected_text, encoding="utf-8")
    st = path.stat()
    return {
        "path": str(path),
        "bytes": int(st.st_size),
        "sha256": expected_digest,
        "reused_verified_content": reused,
    }


def _sha256_path(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_stamp(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "path": str(path)}
    st = path.stat()
    return {
        "exists": True,
        "path": str(path),
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
        "sha256": _sha256_path(path),
    }


def _assert_current_run_report_provenance(report: Path, pre_stamp: Mapping[str, Any]) -> None:
    if not report.is_file():
        raise RuntimeError(f"compact stress 报告不存在: {report}")
    post_stamp = _artifact_stamp(report)
    if pre_stamp.get("exists") and (
        post_stamp.get("mtime_ns") == pre_stamp.get("mtime_ns")
        and post_stamp.get("size") == pre_stamp.get("size")
        and post_stamp.get("sha256") == pre_stamp.get("sha256")
    ):
        raise RuntimeError(
            "STALE_REPORT_PROVENANCE: compact stress report matches preexisting file unchanged across reused run_id"
        )


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
        "target_coverage_contract": {
            "scope_label": "long_field_journal",
            "coverage_requirement": "full_source_read",
            "enforcement": "required",
            "target_items": [
                {
                    "target_id": "data/long_field_journal.txt",
                    "source_path": "data/long_field_journal.txt",
                    "coverage_kind": "full_source_read",
                    "enforcement": "required",
                }
            ],
        },
    }


def _missing_section_facts(
    lowered_content: str,
    facts: list[StressFact],
    *,
    include_checkpoint: bool,
) -> list[str]:
    missing: list[str] = []
    for fact in facts:
        missing.extend(_missing_fact_values(lowered_content, fact, include_checkpoint=include_checkpoint))
    return missing


def _missing_fact_values(lowered_content: str, fact: StressFact, *, include_checkpoint: bool) -> list[str]:
    section = f"{fact.section:03d}"
    values = [section, fact.city, fact.decision, fact.risk]
    if include_checkpoint:
        values.append(fact.checkpoint)
    return [
        f"section_{section}:{value}"
        for value in values
        if value.casefold() not in lowered_content
    ]


def _compact_cycle_count(
    compact_dir: Path,
    *,
    threads_dir: Path | None = None,
    baseline: Mapping[str, Any] | None = None,
) -> int:
    from .compact_observations import load_canonical_compaction_checkpoints

    result = load_canonical_compaction_checkpoints(
        compact_dir=compact_dir,
        threads_dir=threads_dir,
        baseline=baseline,
    )
    return int(result["successful_cycle_count"])


def _ensure_compact_stress_config(lab) -> dict[str, Any]:
    marker = "# compact-stress overrides"
    text = lab.config_path.read_text(encoding="utf-8")
    if marker not in text:
        overrides = textwrap.dedent(
            f"""

            {marker}
            enable_subagents: false
            max_tool_rounds: 0
            model_context_window_tokens: 200000
            memory_compact_trigger_percent: 70
            request_timeout: {max(600, int(lab.args.timeout))}
            tool_read_max_chars: 100000
            tool_output_externalize_min_chars: 140000
            tool_output_preview_chars: 8000
            """
        )
        lab.config_path.write_text(text + overrides, encoding="utf-8")

    session = getattr(lab, "_session", None)
    if session is not None and hasattr(session, "refresh_resolved_home_context"):
        home_ctx = session.refresh_resolved_home_context()
    else:
        home_ctx = resolve_live_lab_home_context(lab.config_path)

    policy = home_ctx.get("compact_policy") or {}
    effective_trigger = policy.get("effective_trigger_percent")
    if effective_trigger != 70:
        raise RuntimeError(
            f"COMPACT_POLICY_OVERRIDE_NOT_EFFECTIVE: expected effective_trigger_percent=70, actual={effective_trigger}, "
            f"source={policy.get('effective_source')!r}"
        )
    if hasattr(lab, "log"):
        lab.log(f"compact_stress_effective_policy={json.dumps(policy, ensure_ascii=False)}")
    return home_ctx


def _disable_shell_tool_for_compact_stress(
    lab,
    *,
    home_ctx: Mapping[str, Any] | None = None,
) -> Path:
    resolved = dict(home_ctx) if home_ctx is not None else resolve_live_lab_home_context(lab.config_path)
    policy_path = Path(resolved["owner_tool_policy_json"])
    assert_isolated_home_path(resolved["isolated_home_root"], label="isolated_home")
    fixture_root = getattr(lab, "fixture_root", None)
    if isinstance(fixture_root, Path):
        resolved_fixture = fixture_root.resolve(strict=False)
        if policy_path.resolve(strict=False).is_relative_to(resolved_fixture):
            raise RuntimeError(
                f"TOOL_POLICY_INSIDE_FIXTURE: resolved owner_tool_policy_json {policy_path} unexpectedly inside {resolved_fixture}"
            )
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": "tool-policy.v1",
                "enabled_sources": ["builtin", "owner", "workspace", "shared"],
                "disabled_tools": ["run_command"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    if hasattr(lab, "log"):
        lab.log(f"compact_stress_disabled_tools={policy_path}")
    return policy_path


def _stress_size_mb() -> int:
    raw = os.environ.get("MY_AGENT_COMPACT_STRESS_SIZE_MB", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_STRESS_SIZE_MB
    return max(1, value)


__all__ = ["case_main_compact_stress_long_read", "evaluate_compact_stress_observations"]
