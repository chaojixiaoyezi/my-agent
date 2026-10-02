# LLM: 私有状态的目录布局集中在这里，业务模块不得自建第二套路径或把作业记录写入用户工作区。
# 模块用途: 映射工作区私有命名空间，读写冻结作业、确认回执和运行历史。

from __future__ import annotations

from pathlib import Path

from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext

from .errors import DramaShellError
from .job_contract import validate_stored_job
from .private_store import (
    data_root,
    job_key,
    list_records,
    read_record,
    workspace_key,
    write_record,
)


# LLM: 命名空间只由宿主下发 cwd 的摘要决定，不接受业务参数指定或跨工作区查找。
# 函数用途: 返回插件数据根和当前工作区对应的私有目录段。
def namespace(context: WorkspaceReadContext) -> tuple[Path, tuple[str, ...]]:
    return data_root(), ("workspaces", workspace_key(context.cwd))


# LLM: 作业正文读回后始终复核完整指纹和请求的 job_id，避免摘要文件名与正文身份分离。
# 函数用途: 读取当前工作区的一份冻结作业。
def load_job(root: Path, scope: tuple[str, ...], job_id: str) -> dict:
    return validate_stored_job(read_record(root, job_parts(scope, job_id)), job_id)


# LLM: 确认回执由 confirm 写、run 一次性消费；缺失统一转成需要确认而不泄露私有路径。
# 函数用途: 读取当前作业的确认回执。
def load_receipt(root: Path, scope: tuple[str, ...], job_id: str) -> dict:
    try:
        return read_record(root, confirmation_parts(scope, job_id))
    except DramaShellError as exc:
        if exc.code == "JOB_NOT_FOUND":
            raise DramaShellError("CONFIRMATION_REQUIRED", "作业需要明确确认。") from exc
        raise


# LLM: 每条运行记录都校验固定身份和有限状态；畸形记录使调用失败关闭，不能静默跳过改写历史。
# 函数用途: 按开始时间读取当前作业的全部运行历史。
def load_history(root: Path, scope: tuple[str, ...], job_id: str) -> list[dict]:
    directory = run_directory_parts(scope, job_id)
    history = [read_record(root, directory + (name,)) for name in list_records(root, directory)]
    for run in history:
        _validate_run(run, job_id)
    history.sort(key=lambda item: (str(item["started_at"]), str(item["run_id"])))
    return history


# LLM: 运行记录和作业记录使用同一个安全命名规则与原子私有存储，不产生宿主任务或其他旁路账本。
# 函数用途: 原子保存一条当前作业的运行记录。
def write_run(root: Path, scope: tuple[str, ...], run: dict) -> None:
    job_id, run_id = run.get("job_id"), run.get("run_id")
    if not isinstance(job_id, str) or not isinstance(run_id, str):
        raise DramaShellError("PRIVATE_RECORD_INVALID", "运行记录身份无效。")
    write_record(root, run_parts(scope, job_id, run_id), run)


# LLM: prepare 会保留旧历史，状态和 collect 只能选择与当前作业指纹相同的尝试，避免旧产物冒充新作业。
# 函数用途: 返回当前指纹最新的运行记录。
def latest_current(history: list[dict], fingerprint: object) -> dict | None:
    current = [run for run in history if run.get("fingerprint") == fingerprint]
    return current[-1] if current else None


# LLM: 作业、确认与运行目录均用 job_id 摘要，原始编号只留在经校验的 JSON 正文。
# 函数用途: 生成冻结作业的私有路径段。
def job_parts(scope: tuple[str, ...], job_id: str) -> tuple[str, ...]:
    return scope + ("jobs", f"{job_key(job_id)}.json")


# LLM: 确认回执与冻结作业分开，prepare 可以原子替换作业后明确删除旧确认。
# 函数用途: 生成确认回执的私有路径段。
def confirmation_parts(scope: tuple[str, ...], job_id: str) -> tuple[str, ...]:
    return scope + ("confirmations", f"{job_key(job_id)}.json")


# LLM: 每个作业有独立运行目录，audit 可枚举历史而无需反向解析摘要文件名。
# 函数用途: 生成一个作业运行历史目录的私有路径段。
def run_directory_parts(scope: tuple[str, ...], job_id: str) -> tuple[str, ...]:
    return scope + ("runs", job_key(job_id))


# LLM: run_id 只由本插件生成，但仍经 job_key 摘要成文件名，避免时间戳字符影响平台兼容性。
# 函数用途: 生成单条运行记录的私有路径段。
def run_parts(scope: tuple[str, ...], job_id: str, run_id: str) -> tuple[str, ...]:
    return run_directory_parts(scope, job_id) + (f"{job_key(run_id)}.json",)


# LLM: 状态只允许外壳实际写出的三种值，运行记录绝不能携带供应商句柄或声明真实生成成功。
# 函数用途: 校验运行记录的身份、状态和夹具标志。
def _validate_run(run: dict, job_id: str) -> None:
    if run.get("job_id") != job_id or not isinstance(run.get("run_id"), str):
        raise DramaShellError("PRIVATE_RECORD_INVALID", "运行记录身份无效。")
    if run.get("status") not in {"running", "fixture_succeeded", "failed"}:
        raise DramaShellError("PRIVATE_RECORD_INVALID", "运行记录状态无效。")
    if run.get("fixture") is not True or run.get("generation_success") is not False:
        raise DramaShellError("PRIVATE_RECORD_INVALID", "运行记录不得冒充真实生成。")
    if not isinstance(run.get("started_at"), str) or not isinstance(run.get("fingerprint"), str):
        raise DramaShellError("PRIVATE_RECORD_INVALID", "运行记录时间或指纹无效。")
