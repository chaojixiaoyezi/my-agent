
from __future__ import annotations

"""未验证即交付的一次性提醒门（uncontracted 路径专用，保守、幂等）。

实锤背景（native 回归）：弱模型把可运行的代码成品直接写进 output/ 后停手，
uncontracted closeout 见「产物可打开」即判完成——它本就只守客观事实（产物能不能打开 /
派过人零产物 / 子代理未收口），**不验证「任务明确要求跑测试，却没有任何测试执行证据」**。
于是「写了 .py 就算交付」绕过了用户白纸黑字要的「真实运行测试确保全过」。text 下模型
凭借每轮可见的完整 transcript 更容易自发把测试跑完再交付，native 下这条纪律连同其它
运行时指引一起被旁路掉，回归就此暴露。

本门只做一件很窄的事——当且仅当三件事同时成立时，**打回一次**让模型补跑测试：
  ①任务 prompt 里有明确的「跑测试 / 运行 / 确保全过 / run tests / make sure ... pass」
    这类**验证执行**信号（不是泛泛的"测试"词，避免误伤"写一个测试方案"之类）；
  ②本 run 没有任何**测试执行证据**（成功跑过 run_command/controlled_exec 且命令看着像在
    跑测试：pytest / unittest / npm test / go test / cargo test / ...）；
  ③交付区至少有一个**代码源文件**产物（.py/.js/.ts/.go/...）——纯报告/文档/数据类任务
    天然没有代码产物，永不触发本门。

打回是**幂等一次**（与 _declared_gap_rework 同款语义）：注入一条带双出口的提醒后，
第二次同形态直接放行（写进 advisories 供把关），所以即便模型确实没有 shell 工具、
确实跑不了测试，也绝不会卡死——最多浪费一轮提醒。纯加法、零配置；text 路径同样适用
（text 下这条纪律本就更容易被模型自发满足，门只是兜底，不改变 text 既有正常交付）。
"""

import json as _json
import re
from typing import Any

# 任务 prompt 里"要求真实跑测试/验证"的强信号。只认带"执行/运行/通过"语义的组合，
# 不认裸"测试"二字（"写测试用例/设计测试方案"不应触发本门）。
_VERIFICATION_REQUIRED_PATTERNS = (
    r"跑(一下|通)?测试",
    r"运行(测试|单元测试|用例)",
    r"真实运行",
    r"(确保|保证)(全部|所有)?(测试)?(全)?(通过|过)",
    r"测试(全部|都)?(通过|跑通|跑过)",
    r"run\s+(the\s+)?tests?\b",
    r"run\s+(the\s+)?test\s+suite",
    r"make\s+sure\s+.*\btests?\b.*\bpass",
    r"all\s+tests?\s+(should\s+)?pass",
    r"ensure\s+.*\btests?\b.*\bpass",
)

# 命令看起来"在跑测试"的标志（出现在成功 run_command/controlled_exec 的命令文本里
# 即视为有测试执行证据）。覆盖主流测试入口；宽松匹配，宁可放行不误拦。
_TEST_RUN_COMMAND_PATTERNS = (
    r"\bpytest\b",
    r"\bunittest\b",
    r"-m\s+pytest",
    r"-m\s+unittest",
    r"\bnpm\s+(run\s+)?test\b",
    r"\byarn\s+test\b",
    r"\bpnpm\s+test\b",
    r"\bgo\s+test\b",
    r"\bcargo\s+test\b",
    r"\bgradle\s+test\b",
    r"\bmvn\s+test\b",
    r"\bphpunit\b",
    r"\brspec\b",
    r"\bjest\b",
    r"\bvitest\b",
    r"\bmocha\b",
    r"\bctest\b",
    r"\bdotnet\s+test\b",
)

# 视为"代码源文件"的后缀（只有交付区有这类产物才可能触发本门）。
_CODE_ARTIFACT_SUFFIXES = frozenset(
    {
        "py", "js", "ts", "tsx", "jsx", "mjs", "cjs",
        "go", "rs", "java", "kt", "rb", "php", "c", "h",
        "cc", "cpp", "hpp", "cs", "swift", "scala", "sh", "bash",
    }
)

_VERIFICATION_GAP_MARKER = "[verification-evidence-rework]"


def verification_evidence_rework(request: object, report: dict[str, Any]) -> bool:
    """任务要求跑测试、却无测试执行证据、且交了代码产物 → 打回一次（True）。

    幂等：本 run 已提醒过则直接 False（放行）。无三要素之一也 False。
    """
    params = getattr(request, "params", None)
    if params is None:
        return False
    if _rework_already_emitted(params):
        return False
    if not _task_requires_test_run(params):
        return False
    if not _has_code_artifact(report):
        return False
    if _has_test_run_evidence(params):
        return False
    _append_verification_rework_context(params, report)
    report["verification_evidence_gate"] = {
        "allowed": False,
        "finding": "VERIFICATION_REQUIRED_BUT_NO_TEST_RUN_EVIDENCE",
        "message_zh": (
            "任务要求真实运行测试确保全部通过，但本次没有任何测试执行证据"
            "（没有成功跑过 pytest / unittest / npm test 等测试命令），交付区却已写入代码产物。"
            "请先用 run_command 真实运行测试并确认全部通过，再提交验收。"
        ),
    }
    report["ok"] = False
    from .artifacts import _write_report

    _write_report(_report_root(report), report)
    return True


def _rework_already_emitted(params: object) -> bool:
    context = getattr(params, "tool_context", None)
    if not isinstance(context, list):
        return False
    return any(_VERIFICATION_GAP_MARKER in str(item) for item in context)


def _task_requires_test_run(params: object) -> bool:
    text = _prompt_text(params)
    if not text:
        return False
    lowered = text.casefold()
    return any(re.search(pattern, lowered) for pattern in _VERIFICATION_REQUIRED_PATTERNS)


def _prompt_text(params: object) -> str:
    return "\n".join(
        part
        for part in (
            str(getattr(params, "root_user_prompt", "") or ""),
            str(getattr(params, "user_prompt", "") or ""),
        )
        if part
    )


def _has_code_artifact(report: dict[str, Any]) -> bool:
    for item in report.get("artifacts", []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").lower().lstrip(".")
        if kind in _CODE_ARTIFACT_SUFFIXES:
            return True
        path = str(item.get("path") or "")
        suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if suffix in _CODE_ARTIFACT_SUFFIXES:
            return True
    return False


def _has_test_run_evidence(params: object) -> bool:
    for record in getattr(params, "archive_tool_calls", []) or []:
        if not isinstance(record, dict):
            continue
        if record.get("ok") is not True:
            continue
        if str(record.get("tool") or "").strip() not in {"run_command", "controlled_exec"}:
            continue
        if _command_runs_tests(_record_command_text(record)):
            return True
    return False


def _record_command_text(record: dict[str, Any]) -> str:
    params = record.get("parameters")
    params = params if isinstance(params, dict) else {}
    parts = [_command_field_text(params.get(key)) for key in ("command", "cmd", "args", "script")]
    src = record.get("source_input")
    parts.append(src if isinstance(src, str) else "")
    return "\n".join(part for part in parts if part)


def _command_field_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return ""


def _command_runs_tests(text: str) -> bool:
    if not text:
        return False
    lowered = text.casefold()
    return any(re.search(pattern, lowered) for pattern in _TEST_RUN_COMMAND_PATTERNS)


def _append_verification_rework_context(params: object, report: dict[str, Any]) -> None:
    context = getattr(params, "tool_context", None)
    if not isinstance(context, list):
        return
    context.append(
        f"{_VERIFICATION_GAP_MARKER}\n"
        + _json.dumps(
            {
                "ok": False,
                "finding": "VERIFICATION_REQUIRED_BUT_NO_TEST_RUN_EVIDENCE",
                "report_ref": report.get("report_ref", ""),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n本次任务明确要求真实运行测试并确保全部通过，但当前没有任何测试执行证据"
        "（没有成功跑过 pytest / unittest / npm test / go test 等测试命令）。"
        "请先用 run_command 在交付目录里真实运行测试，确认全部通过后再 submit_for_acceptance；"
        "如果确实没有可用的执行环境跑不了测试，请在交付说明里写清楚原因，再提交。"
    )


def _report_root(report: dict[str, Any]):
    from pathlib import Path

    return Path(str(report.get("workspace_root") or "."))


__all__ = ["verification_evidence_rework"]
