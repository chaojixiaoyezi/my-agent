
from __future__ import annotations

"""main-agent artifact E2E cases.

这里的测试会准备一个比较长的资料文件，让主代理自己找远距离证据。
它用来检查“大输出外置、分片续读、报告验收”这一类能力。
"""

import json
import textwrap
from pathlib import Path

from .main_agent_complex_case import _ensure_main_agent_only

ARTIFACT_READBACK_BYTES = 96 * 1024


def case_main_artifact_readback(lab) -> None:
    lab.section("CASE main_artifact_readback")
    _ensure_main_agent_only(lab)
    source = lab.fixture_root / "data" / "artifact-readback" / "source.txt"
    _seed_artifact_readback_source(source)
    prompt = _main_artifact_readback_prompt()
    lab.record_prompt("main_artifact_readback", prompt)
    response = lab.run_command(
        lab.agent_command("run", prompt, "--save"),
        timeout=lab.args.timeout + 150,
    )
    (lab.responses_dir / "main_artifact_readback.stdout.txt").write_text(response.stdout, encoding="utf-8")
    output = lab.fixture_root / "lab_outputs" / "artifact-readback" / "report.md"
    _assert_artifact_readback_report(output)
    lab.log(f"artifact_readback_report={output}")


def case_main_compact_resume_roundtrip(lab) -> None:
    lab.section("CASE main_compact_resume_roundtrip")
    fact_id = _latest_artifact_readback_fact_id(lab.fixture_root)
    fact_payload = _run_json_command(
        lab,
        "main_compact_resume_fact_write",
        lab.agent_command(
            "memory-fact-write",
            "--fact-id",
            fact_id,
            "--goal",
            "长输出读回报告",
            "--next-action",
            "继续检查 compact resume 交接包",
            "--acceptance",
            "report.md 包含 ALPHA-ANCHOR、OMEGA-ANCHOR、TRACE-ARTIFACT-991",
            "--constraint",
            "不得猜测未读取内容",
            "--latest-test",
            "main_artifact_readback Live Lab gate passed",
            "--json",
        ),
        timeout=90,
    )
    if not fact_payload.get("ok"):
        raise RuntimeError(f"memory-fact-write 未通过: {fact_payload}")
    apply_payload = _run_json_command(
        lab,
        "main_compact_resume_apply",
        lab.agent_command("memory-compact", "--apply", "--request-id", fact_id, "--json"),
        timeout=120,
    )
    apply_id = str(apply_payload.get("apply_id") or "")
    resume_payload = _run_json_command(
        lab,
        "main_compact_resume_auto",
        lab.agent_command("memory-resume", "--from-compact", apply_id, "--compact-resume-mode", "auto", "--json"),
        timeout=90,
    )
    _assert_compact_resume_roundtrip_payload(apply_payload, resume_payload)
    lab.log(f"compact_resume_apply_id={apply_id}")


def _main_artifact_readback_prompt() -> str:
    return textwrap.dedent(
        """
        这次你自己完成，不要派小傻妞。

        data/artifact-readback/source.txt 这个资料比较长。
        请你找出 ALPHA-ANCHOR、OMEGA-ANCHOR、TRACE-ARTIFACT-991 三处附近分别在说什么。
        如果系统一次只给你一部分内容，或者提示内容已经放到外置文件里，请继续按线索读完整，不要猜。

        最终把三处证据、你的中文说明、风险判断和下一步建议写到 lab_outputs/artifact-readback/report.md。
        报告要让普通人能看懂，不能只列三个词。
        """
    ).strip()


def _seed_artifact_readback_source(path: Path) -> None:
    if path.exists() and path.stat().st_size >= ARTIFACT_READBACK_BYTES:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    filler = (
        "普通段落：这是一段用于拉开距离的资料文字，描述库存、渠道、售后、物流和用户反馈。"
        "它本身不是目标证据，只用于模拟真实长文件中的普通信息。\n"
    )
    blocks = [
        "ALPHA-ANCHOR: 第一处证据说明北区门店的高端家具库存连续三周偏低，影响新品展示。\n",
        filler * 220,
        "OMEGA-ANCHOR: 第二处证据说明线上预约系统在周末高峰出现排队延迟，影响客户到店体验。\n",
        filler * 260,
        "TRACE-ARTIFACT-991: 第三处证据说明售后回访里反复出现同一批次沙发面料色差反馈。\n",
    ]
    text = "".join(blocks)
    while len(text.encode("utf-8")) < ARTIFACT_READBACK_BYTES:
        text += filler
    path.write_text(text, encoding="utf-8")


def _assert_artifact_readback_report(output: Path) -> None:
    if not output.exists():
        raise RuntimeError(f"长输出读回报告不存在: {output}")
    content = output.read_text(encoding="utf-8", errors="replace")
    lowered = content.lower()
    required = ["alpha-anchor", "omega-anchor", "trace-artifact-991"]
    missing = [item for item in required if item not in lowered]
    if missing:
        raise RuntimeError(f"长输出读回报告缺少远距离证据: {missing}")
    if len(content.strip()) < 180:
        raise RuntimeError("长输出读回报告过短，不足以说明证据、风险和建议。")


def _latest_artifact_readback_fact_id(fixture_root: Path) -> str:
    facts_root = (
        fixture_root / ".my_agent" / "home" / "owners" / "local" / "main" / "memory_archive" / "runtime_facts"
    )
    roots = sorted(facts_root.glob("*/task.json"))
    matches = [
        (path.stat().st_mtime, str(path.parent.name))
        for path in roots
        if "TRACE-ARTIFACT-991" in _read_text(path)
    ]
    if not matches:
        raise RuntimeError("没有找到 main_artifact_readback 的 runtime fact source。")
    return sorted(matches)[-1][1]


def _run_json_command(lab, name: str, command: list[str], *, timeout: float) -> dict:
    response = lab.run_command(command, timeout=timeout)
    (lab.responses_dir / f"{name}.stdout.json").write_text(response.stdout, encoding="utf-8")
    try:
        payload = json.loads(response.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{name} 输出不是 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{name} JSON 输出不是对象。")
    return payload


def _assert_compact_resume_roundtrip_payload(apply_payload: dict, resume_payload: dict) -> None:
    if not apply_payload.get("ok"):
        raise RuntimeError("compact apply 没有成功。")
    work_state = apply_payload.get("work_state_snapshot", {})
    if work_state.get("missing_fields"):
        raise RuntimeError(f"compact work_state 仍有缺字段: {work_state.get('missing_fields')}")
    if not apply_payload.get("post_compact_self_check", {}).get("ok"):
        raise RuntimeError("compact self-check 没有通过。")
    _assert_recorded_field(work_state, "acceptance", "source_status")
    _assert_recorded_field(work_state, "constraints", "source_status")
    _assert_recorded_field(work_state, "latest_tests", "status")
    if not work_state.get("artifact_refs"):
        raise RuntimeError("compact work_state 没有携带 tool-output artifact refs。")
    if not resume_payload.get("ok"):
        raise RuntimeError("compact resume 没有成功。")
    guard = resume_payload.get("action_guard", {})
    if guard.get("status") != "allow_automated_continue" or guard.get("allowed_to_continue") is not True:
        raise RuntimeError(f"compact auto guard 没有放行续接: {guard}")
    handoff = resume_payload.get("handoff", {})
    if handoff.get("missing_fields"):
        raise RuntimeError(f"compact handoff 仍有缺字段: {handoff.get('missing_fields')}")
    if not resume_payload.get("recommended_read_paths"):
        raise RuntimeError("compact resume 没有推荐读取路径。")


def _assert_recorded_field(work_state: dict, field: str, status_key: str) -> None:
    payload = work_state.get(field, {})
    if payload.get(status_key) != "recorded" or not payload.get("items") or not payload.get("source_paths"):
        raise RuntimeError(f"compact work_state 字段 {field} 没有结构化事实源: {payload}")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


__all__ = [
    "_assert_artifact_readback_report",
    "_assert_compact_resume_roundtrip_payload",
    "_main_artifact_readback_prompt",
    "case_main_artifact_readback",
    "case_main_compact_resume_roundtrip",
]
