# LLM: 审计只核对插件私有操作证据、当前输入摘要和夹具输出字节；绝不把这些证据升级为媒体质量或真实生成结论。
# 模块用途: 汇总当前工作区的媒体作业、夹具尝试和输出完整性问题。

from __future__ import annotations

from collections.abc import Mapping

from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext

from .errors import DramaShellError
from .job_contract import inputs_current, sha256_bytes, validate_stored_job
from .private_store import list_records, read_record
from .production import FIXTURE_NOTICE
from .production_state import load_history, namespace
from .workspace_io import read_workspace_bytes

MAX_AUDIT_OUTPUT_BYTES = 2 * 1024 * 1024


# LLM: audit 只枚举当前 cwd 摘要下的私有记录；其他工作区即使共用插件进程也不可见。
# 函数用途: 返回当前工作区生产外壳的操作证据审计摘要。
def audit_project(context: WorkspaceReadContext) -> dict:
    root, scope = namespace(context)
    jobs, problems = _load_jobs(root, scope)
    state = {"counts": _empty_counts(), "claims": {}, "problems": problems, "context": context}
    for job in jobs:
        _summarize_job(root, scope, job, state)
    counts, claims = state["counts"], state["claims"]
    output_counts = _verify_claims(context, claims, problems)
    return {
        "status": "attention" if problems else "pass",
        "scope": "operational_evidence_only", "quality_verdict": "not_assessed",
        "fixture": True, "generation_success": False, "notice": FIXTURE_NOTICE,
        "jobs": {"total": len(jobs), "running": counts["running_jobs"],
                 "recovered": counts["recovered_jobs"],
                 "terminal_failed": counts["terminal_failed_jobs"],
                 "retryable_terminal_failed": 0},
        "attempts": {"total": counts["attempts_total"],
                     "succeeded": counts["attempts_succeeded"],
                     "failed": counts["attempts_failed"],
                     "running": counts["attempts_running"],
                     "superseded": counts["attempts_superseded"],
                     "repeated_content": counts["repeated_content"]},
        "outputs": {"claimed_current": len(claims), **output_counts},
        "problems": problems,
    }


# LLM: 记录名不携带 job_id，因此逐个读正文并验指纹；损坏记录只形成可操作问题，不阻断其他作业审计。
# 函数用途: 读取当前命名空间的全部有效作业和坏记录问题。
def _load_jobs(root, scope: tuple[str, ...]) -> tuple[list[dict], list[dict]]:
    jobs, problems = [], []
    directory = scope + ("jobs",)
    for name in list_records(root, directory):
        try:
            jobs.append(validate_stored_job(read_record(root, directory + (name,))))
        except DramaShellError:
            problems.append({"code": "invalid_job_record", "record": name,
                             "action": "repair_plugin_private_metadata"})
    return jobs, problems


# LLM: 计数键固定对应上游 audit 公开结构，避免后续调用方解析自然语言统计。
# 函数用途: 创建单次审计使用的可变整数计数器。
def _empty_counts() -> dict[str, int]:
    return {
        "running_jobs": 0, "recovered_jobs": 0, "terminal_failed_jobs": 0,
        "attempts_total": 0, "attempts_succeeded": 0, "attempts_failed": 0,
        "attempts_running": 0, "attempts_superseded": 0, "repeated_content": 0,
    }


# LLM: 一份作业的历史只按当前指纹认领输出；旧指纹保留为 superseded 证据但不能影响当前产物结论。
# 函数用途: 更新单作业尝试计数、问题和最新输出声明。
def _summarize_job(root, scope: tuple[str, ...], job: dict, state: dict) -> None:
    job_id = str(job["job_id"])
    counts, problems = state["counts"], state["problems"]
    try:
        history = load_history(root, scope, job_id)
    except DramaShellError:
        problems.append({"code": "invalid_run_history", "job_id": job_id,
                         "action": "repair_plugin_private_metadata"})
        return
    current = [run for run in history if run.get("fingerprint") == job["fingerprint"]]
    counts["attempts_total"] += len(history)
    counts["attempts_superseded"] += len(history) - len(current)
    counts["attempts_succeeded"] += sum(run["status"] == "fixture_succeeded" for run in history)
    counts["attempts_failed"] += sum(run["status"] == "failed" for run in history)
    counts["attempts_running"] += sum(run["status"] == "running" for run in history)
    counts["repeated_content"] += len(history) - len({run["fingerprint"] for run in history})
    _record_terminal_state(current, job_id, state)
    _record_current_claims(current, job, state)
    if not inputs_current(job, state["context"]):
        problems.append({"code": "job_inputs_changed", "job_id": job_id,
                         "action": "prepare_and_confirm_again"})


# LLM: running 和末次失败是操作状态问题；fixture 失败不声称供应商失败，也没有重试收费语义。
# 函数用途: 记录当前作业的运行中或终态失败问题。
def _record_terminal_state(current: list[dict], job_id: str, state: dict) -> None:
    counts, problems = state["counts"], state["problems"]
    running = [run for run in current if run["status"] == "running"]
    if running:
        counts["running_jobs"] += 1
    for run in running:
        problems.append({"code": "running_fixture_attempt", "job_id": job_id,
                         "run_id": run["run_id"], "action": "inspect_fixture_attempt"})
    terminal = [run for run in current if run["status"] != "running"]
    if terminal and terminal[-1]["status"] == "failed":
        counts["terminal_failed_jobs"] += 1
        problems.append({"code": "terminal_fixture_failure", "job_id": job_id,
                         "run_id": terminal[-1]["run_id"], "action": "inspect_then_reconfirm"})
    if any(run["status"] == "failed" for run in terminal) and any(
            run["status"] == "fixture_succeeded" for run in terminal):
        counts["recovered_jobs"] += 1


# LLM: 同路径只保留完成时间最新的当前指纹成功声明，避免旧尝试覆盖较新的操作证据。
# 函数用途: 从当前成功记录提取并验证输出声明。
def _record_current_claims(current: list[dict], job: dict, state: dict) -> None:
    claims, problems = state["claims"], state["problems"]
    for run in current:
        if run["status"] != "fixture_succeeded":
            continue
        outputs = _valid_output_records(job, run)
        if outputs is None:
            problems.append({"code": "invalid_fixture_output_record", "job_id": job["job_id"],
                             "run_id": run["run_id"], "action": "repair_plugin_private_metadata"})
            continue
        for output in outputs:
            _update_claim(output, job, run, claims)


# LLM: 单路径新旧声明比较独立出来，避免历史循环形成三层分支；完成时间相同时让后读记录保持确定覆盖。
# 函数用途: 用较新的成功运行更新一个输出路径的当前声明。
def _update_claim(output: dict, job: dict, run: dict, claims: dict) -> None:
    previous = claims.get(output["path"])
    if previous is None or str(run["finished_at"]) >= str(previous["finished_at"]):
        claims[output["path"]] = {**output, "job_id": job["job_id"],
                                  "run_id": run["run_id"], "finished_at": run["finished_at"]}


# LLM: 成功记录必须逐项对应当前作业输出，并保持 fixture 标志、摘要和字节数类型完整。
# 函数用途: 验证一条成功运行的结构化输出记录。
def _valid_output_records(job: dict, run: dict) -> list[dict] | None:
    outputs, expected = run.get("outputs"), job.get("outputs")
    if not isinstance(outputs, list) or not isinstance(expected, list) or len(outputs) != len(expected):
        return None
    for path, output in zip(expected, outputs, strict=True):
        if not isinstance(output, Mapping) or set(output) != {
                "path", "media_type", "bytes", "sha256", "fixture"}:
            return None
        if output.get("path") != path or output.get("fixture") is not True:
            return None
        if type(output.get("bytes")) is not int or not isinstance(output.get("sha256"), str):
            return None
    return [dict(item) for item in outputs]


# LLM: 输出验证通过同一调用的读取上下文和 no-follow 上限执行；缺失与不安全路径归为可恢复问题而非质量失败。
# 函数用途: 核对当前输出声明与工作区实际字节并返回计数。
def _verify_claims(context: WorkspaceReadContext, claims: dict,
                   problems: list[dict]) -> dict[str, int]:
    counts = {"verified": 0, "missing": 0, "modified": 0}
    for path, claim in sorted(claims.items()):
        try:
            content = read_workspace_bytes(context, path, MAX_AUDIT_OUTPUT_BYTES)
        except DramaShellError:
            counts["missing"] += 1
            problems.append({"code": "output_missing_or_unsafe", "job_id": claim["job_id"],
                             "run_id": claim["run_id"], "path": path,
                             "action": "collect_fixture_output"})
            continue
        if len(content) != claim["bytes"] or sha256_bytes(content) != claim["sha256"]:
            counts["modified"] += 1
            problems.append({"code": "output_digest_mismatch", "job_id": claim["job_id"],
                             "run_id": claim["run_id"], "path": path,
                             "action": "review_or_collect_fixture_output"})
        else:
            counts["verified"] += 1
    return counts
