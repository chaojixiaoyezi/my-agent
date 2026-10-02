# LLM: 本模块只实现离线夹具作业状态机；供应商请求结构化拒绝，工作区输出逐文件走 SDK check→anchor→no-follow 原子写。
# 模块用途: 提供 prepare、confirm、run、status、collect 的生产外壳，不联网、不起子进程、不建立宿主任务账。

from __future__ import annotations

import uuid
from pathlib import Path, PurePosixPath

from my_agent_plugin_api.nofollow_fs import (
    NoFollowPathError,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext
from my_agent_plugin_api.workspace_write_context import WorkspaceWriteContext

from .errors import DramaShellError
from .fixture_adapter import fixture_bytes
from .job_contract import (
    MEDIA_TYPES,
    inputs_current,
    normalize_job,
    preview,
    sha256_bytes,
    utc_now,
)
from .private_store import remove_record, write_record
from .production_state import (
    confirmation_parts,
    job_parts,
    latest_current,
    load_history,
    load_job,
    load_receipt,
    namespace,
    write_run,
)

FIXTURE_NOTICE = "夹具，不是真实生成；夹具产物不算生成成功。"
Contexts = tuple[WorkspaceReadContext, WorkspaceWriteContext]


# LLM: prepare 只冻结工作区输入摘要并写插件私有记录，不碰输出文件；同一 job_id 重做会明确撤销旧确认。
# 函数用途: 校验并保存一份待确认媒体作业。
def prepare_job(context: WorkspaceReadContext, raw: object) -> dict:
    job = normalize_job(raw, context)
    root, scope = namespace(context)
    history = load_history(root, scope, str(job["job_id"]))
    latest = latest_current(history, job["fingerprint"])
    if latest is not None and latest.get("status") == "running":
        raise DramaShellError("JOB_RUNNING", "该作业已有运行中的尝试。")
    write_record(root, job_parts(scope, str(job["job_id"])), job)
    remove_record(root, confirmation_parts(scope, str(job["job_id"])))
    result = preview(job)
    result["notice"] = FIXTURE_NOTICE
    return result


# LLM: 确认严格匹配当前冻结指纹，回执只进插件私有目录；确认不等于执行，更不等于真实媒体生成。
# 函数用途: 为当前作业写入一次性确认回执。
def confirm_job(context: WorkspaceReadContext, job_id: str, confirmation: str) -> dict:
    root, scope = namespace(context)
    job = load_job(root, scope, job_id)
    expected = preview(job)["confirmation"]
    if confirmation != expected:
        raise DramaShellError("CONFIRMATION_REQUIRED", "确认文本与当前冻结作业不匹配。")
    receipt = {
        "schema_version": "1.0", "job_id": job_id, "fingerprint": job["fingerprint"],
        "confirmed_at": utc_now(), "consumed_at": None, "run_id": None,
    }
    write_record(root, confirmation_parts(scope, job_id), receipt)
    return {"job_id": job_id, "state": "confirmed", "fingerprint": job["fingerprint"],
            "generation_success": False, "notice": FIXTURE_NOTICE}


# LLM: run 在任何输出前拒绝非 fixture 适配器；夹具也必须有未消费确认且所有输入摘要仍一致。
# 函数用途: 消费确认并把确定性夹具媒体写到调用方授权的工作区路径。
def run_job(read_context: WorkspaceReadContext, write_context: WorkspaceWriteContext,
            job_id: str) -> dict:
    root, scope = namespace(read_context)
    job = load_job(root, scope, job_id)
    _require_fixture(job)
    receipt = _valid_receipt(load_receipt(root, scope, job_id), job)
    if not inputs_current(job, read_context):
        raise DramaShellError("NEEDS_RECONFIRMATION", "作业输入已变化，请重新 prepare 和 confirm。")
    run_id = _run_id()
    receipt.update({"consumed_at": utc_now(), "run_id": run_id})
    write_record(root, confirmation_parts(scope, job_id), receipt)
    run = _running_record(job, run_id)
    write_run(root, scope, run)
    try:
        outputs = _write_fixture_outputs(job, (read_context, write_context), False)
    except DramaShellError as exc:
        run.update({"status": "failed", "finished_at": utc_now(),
                    "error": {"code": exc.code, "retryable": False}})
        write_run(root, scope, run)
        raise
    run.update({"status": "fixture_succeeded", "finished_at": utc_now(), "outputs": outputs})
    write_run(root, scope, run)
    return _run_response(run, collected=False)


# LLM: status 每次重算输入摘要并只选择当前指纹历史；旧运行不能让新 prepare 看起来已经完成。
# 函数用途: 查看一份作业当前需要确认、已确认或夹具执行状态。
def job_status(context: WorkspaceReadContext, job_id: str) -> dict:
    root, scope = namespace(context)
    job = load_job(root, scope, job_id)
    latest = latest_current(load_history(root, scope, job_id), job["fingerprint"])
    if not inputs_current(job, context):
        state = "needs_reconfirmation"
    elif latest is not None:
        state = str(latest["status"])
    else:
        state = _confirmation_state(root, scope, job)
    return {
        "job_id": job_id, "modality": job["modality"], "adapter": job["adapter"],
        "outputs": job["outputs"], "state": state, "latest_run": latest,
        "fixture": job["adapter"] == "fixture", "generation_success": False,
        "notice": FIXTURE_NOTICE,
    }


# LLM: collect 不接供应商；它只可按当前成功运行重新构造同一确定性夹具，恢复缺失输出且不再次消费确认。
# 函数用途: 从已完成的夹具运行记录恢复或核对离线夹具产物。
def collect_job(read_context: WorkspaceReadContext, write_context: WorkspaceWriteContext,
                job_id: str) -> dict:
    root, scope = namespace(read_context)
    job = load_job(root, scope, job_id)
    _require_fixture(job)
    latest = latest_current(load_history(root, scope, job_id), job["fingerprint"])
    if latest is None or latest.get("status") != "fixture_succeeded":
        raise DramaShellError("NOTHING_TO_COLLECT", "没有可恢复的已完成夹具尝试。")
    outputs = _write_fixture_outputs(job, (read_context, write_context), True)
    latest.update({"outputs": outputs, "collected": True, "finished_at": utc_now()})
    write_run(root, scope, latest)
    return _run_response(latest, collected=True)


# LLM: 供应商名称可以在 prepare 阶段留作计划，但任何执行或回收入口都只准 fixture；拒绝结果不得暗示已发请求。
# 函数用途: 拒绝真实供应商并声明没有付费调用。
def _require_fixture(job: dict) -> None:
    provider = job.get("adapter")
    if provider != "fixture":
        raise DramaShellError(
            "PROVIDER_NOT_CONFIGURED", "未配置供应商，本插件不调用付费生成。",
            provider=provider, paid_generation_called=False,
        )


# LLM: receipt 字段和当前指纹必须完全匹配，consumed_at 非空表示一次性确认已经使用。
# 函数用途: 校验一份可消费的确认回执。
def _valid_receipt(receipt: dict, job: dict) -> dict:
    fields = {"schema_version", "job_id", "fingerprint", "confirmed_at", "consumed_at", "run_id"}
    if set(receipt) != fields or receipt.get("job_id") != job.get("job_id"):
        raise DramaShellError("CONFIRMATION_REQUIRED", "确认回执无效，请重新确认。")
    if receipt.get("fingerprint") != job.get("fingerprint") or receipt.get("consumed_at") is not None:
        raise DramaShellError("CONFIRMATION_REQUIRED", "作业需要新的明确确认。")
    return receipt


# LLM: run_id 仅用于插件私有尝试记录，随机后缀避免同一秒的重复执行覆盖彼此。
# 函数用途: 创建按 UTC 时间可读且全局不碰撞的运行编号。
def _run_id() -> str:
    return f"{utc_now().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"


# LLM: running 记录在实际写入前落盘，使异常留下操作证据；固定 fixture/generation_success 防止状态冒充真实生成。
# 函数用途: 构造一次夹具运行的初始私有记录。
def _running_record(job: dict, run_id: str) -> dict:
    return {
        "schema_version": "1.0", "run_id": run_id, "job_id": job["job_id"],
        "fingerprint": job["fingerprint"], "modality": job["modality"],
        "adapter": "fixture", "status": "running", "started_at": utc_now(),
        "finished_at": None, "outputs": [], "fixture": True,
        "generation_success": False, "notice": FIXTURE_NOTICE,
    }


# LLM: 多输出逐项复用同一固定夹具字节；每项仍独立接受写入上下文裁决并读回核验。
# 函数用途: 写入或恢复一份作业声明的所有夹具输出。
def _write_fixture_outputs(job: dict, contexts: Contexts, restore: bool) -> list[dict]:
    read_context, write_context = contexts
    if read_context.cwd != write_context.cwd:
        raise DramaShellError("WORKSPACE_CONTEXT_MISMATCH", "读写上下文不属于同一工作区。")
    return [_write_fixture_file(job, str(path), contexts, restore) for path in job["outputs"]]


# LLM: 这是工作区写入的唯一实现：check 后 anchor，再由 SDK no-follow 原子写；restore 不覆盖未授权的不同字节。
# 函数用途: 安全写入一个夹具文件并读回生成摘要记录。
def _write_fixture_file(job: dict, relative: str, contexts: Contexts, restore: bool) -> dict:
    read_context, write_context = contexts
    payload = fixture_bytes(str(job["modality"]))
    target = read_context.cwd / relative
    decision = write_context.check(target)
    if not decision.allowed:
        raise DramaShellError(decision.code, "夹具输出不在本次允许写入范围内。")
    try:
        root, parts = write_context.anchor(target)
        if root.joinpath(*parts) != target:
            raise NoFollowPathError("write anchor differs from lexical target")
    except (NoFollowPathError, OSError, ValueError) as exc:
        raise DramaShellError("UNSAFE_OUTPUT_PATH", "夹具输出路径不安全。") from exc
    try:
        existing = read_bytes_beneath(root, parts, max_bytes=len(payload), require_dir_fd=True)
    except ValueError:
        existing = b"__different__"
    except (NoFollowPathError, OSError) as exc:
        raise DramaShellError("UNSAFE_OUTPUT_PATH", "夹具输出路径不安全。") from exc
    overwrite = bool(job["overwrite"])
    if existing is not None and existing != payload and not overwrite:
        raise DramaShellError("OUTPUT_EXISTS", f"输出已存在且 overwrite 为 false：{relative}")
    if not restore and existing is not None and not overwrite:
        raise DramaShellError("OUTPUT_EXISTS", f"输出已存在且 overwrite 为 false：{relative}")
    if existing != payload:
        _atomic_output(root, parts, payload)
    verified = _read_back(root, parts, len(payload))
    if verified != payload:
        raise DramaShellError("OUTPUT_VERIFY_FAILED", "夹具输出写后核验失败。")
    suffix = PurePosixPath(relative).suffix.casefold()
    return {"path": relative, "media_type": MEDIA_TYPES[suffix], "bytes": len(payload),
            "sha256": sha256_bytes(payload), "fixture": True}


# LLM: 原子写异常不推断提交与否，调用方随后总会重新读回；这里仅把路径结构错误转成稳定业务失败。
# 函数用途: 通过 SDK 原子原语提交一个工作区输出。
def _atomic_output(root: Path, parts: tuple[str, ...], payload: bytes) -> None:
    try:
        write_bytes_atomic_beneath(root, parts, payload)
    except (NoFollowPathError, OSError, ValueError) as exc:
        raise DramaShellError("OUTPUT_WRITE_FAILED", "夹具输出写入失败。") from exc


# LLM: 回读沿 anchor 的 no-follow 路径执行，验证的是当前版本实际落盘字节而非内存中待写负载。
# 函数用途: 有界读回一个刚写入的工作区输出。
def _read_back(root: Path, parts: tuple[str, ...], size: int) -> bytes:
    try:
        value = read_bytes_beneath(root, parts, max_bytes=size, require_dir_fd=True)
    except (NoFollowPathError, OSError, ValueError) as exc:
        raise DramaShellError("OUTPUT_VERIFY_FAILED", "夹具输出无法读回核验。") from exc
    if value is None:
        raise DramaShellError("OUTPUT_VERIFY_FAILED", "夹具输出写入后不存在。")
    return value


# LLM: 没有运行时，只认可当前指纹未消费的确认回执；损坏或已消费回执一律回到 needs_confirmation。
# 函数用途: 计算尚未运行作业的确认状态。
def _confirmation_state(root: Path, scope: tuple[str, ...], job: dict) -> str:
    try:
        receipt = load_receipt(root, scope, str(job["job_id"]))
    except DramaShellError as exc:
        if exc.code == "CONFIRMATION_REQUIRED":
            return "needs_confirmation"
        raise
    if receipt.get("fingerprint") == job.get("fingerprint") and receipt.get("consumed_at") is None:
        return "confirmed"
    return "needs_confirmation"


# LLM: 回执同时给操作状态与否定的生成结论；调用方不得把 fixture_succeeded 翻译为真实媒体生成成功。
# 函数用途: 构造 run 或 collect 的夹具完成回执。
def _run_response(run: dict, collected: bool) -> dict:
    return {
        "job_id": run["job_id"], "run_id": run["run_id"], "state": "fixture_succeeded",
        "collected": collected, "outputs": run["outputs"], "fixture": True,
        "generation_success": False, "notice": FIXTURE_NOTICE,
    }
