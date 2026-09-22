# LLM: venv/pip 必须沿原 operation 的固定资源和原 ProcessSessionStore 启动；不另建进程账，未知退出不能洗成成功。
# 模块用途: 将同步环境准备接到既有托管进程和精确停止链，宿主崩溃后仍保留归属。

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .local_storage.tool_operations import ToolOperationRecord
from .runtime_db.host_commands import HostCommandBinding
from .runtime_db.managed_operation_store import ManagedOperationStore, ToolOperationAuthorityRequest
from .tooling._subprocess_env_scrub import scrub_subprocess_env
from .tooling.background_process_launch import (
    BackgroundLaunchError,
    BackgroundLaunchRequest,
    start_background_process,
)
from .tooling.cancellation import ToolCancelled, raise_if_cancelled
from .tooling.process_scope import ProcessAccessScope, ProcessExecutionScope
from .tooling.process_session_cleanup import stop_process_session
from .tooling.process_session_store import ProcessSessionStore, process_session_store_root
from .user_space.owner_resolver import OwnerHomeResult

if TYPE_CHECKING:
    from .plugin_environment_plan import PluginEnvironmentPlan


# LLM: 这是原 claim 的进程内冻结引用，不能重读当前 holder 补权；持久资源仍归原操作和 ProcessSessionStore。
# 类用途: 将一批准备命令绑定到真实 owner、会话、任务、尝试和已领取的环境计划。
@dataclass(frozen=True)
class PluginEnvironmentOperation:
    owner: OwnerHomeResult
    binding: HostCommandBinding
    plan: PluginEnvironmentPlan
    store: ManagedOperationStore
    claim: ToolOperationRecord

    # LLM: 仅不重入、不 reopen 的 host command handler 使用；未来若允许重开，必须改由 coordinator 直接传原 claim，不能迟到查询补权。
    # 函数用途: 从原管理操作读取一次不可变领取身份，供准备期间反复核对。
    @classmethod
    def bind(cls, owner, repo, binding, plan) -> PluginEnvironmentOperation:
        if owner.owner_id != binding.request.owner_id or plan.operation_id != binding.request.operation_id:
            raise ValueError("环境准备与原管理身份不符")
        store = ManagedOperationStore(repo)
        claim = store.get_tool_operation(
            owner_id=owner.owner_id, run_id=binding.run_id, task_id=binding.task_id,
            attempt_id=binding.attempt_id, operation_id=plan.operation_id,
            tool_name=binding.request.command_name, args_hash="sha256:" + binding.request.input_digest,
        )
        if claim is None:
            raise ValueError("环境准备尚无原操作领取")
        result = cls(owner, binding, plan, store, claim)
        result.authorize()
        return result

    # LLM: 每次目录创建、启动和等待均核对原 holder/代数/epoch/锁；取消不因已完成进程交接而丢失。
    # 函数用途: 拒绝原操作撤销后继续准备，不续租或改动数据库。
    def authorize(self) -> None:
        raise_if_cancelled()
        self.store.require_authority(ToolOperationAuthorityRequest(
            self.owner.owner_id, self.binding.run_id, self.binding.task_id,
            self.plan.operation_id, self.binding.request.command_name, self.binding.attempt_id,
        ), claim=self.claim, resource_scopes=(self.plan.resource_scope,))
        raise_if_cancelled()

    # LLM: 访问会话与执行任务分别来自可信原绑定；Full Access 不得抹去 owner 的规范地址。
    # 函数用途: 为一个准备命令生成原后台启动请求，日志留私有候选且没有完成唤醒。
    def launch_request(self, argv, cwd, environment, deadline, log_path) -> BackgroundLaunchRequest:
        binding, owner = self.binding, self.owner
        return BackgroundLaunchRequest(
            argv=list(argv), command="插件独立环境准备", cwd=cwd, log_path=log_path,
            env=environment, max_log_bytes=65536,
            store_root=process_session_store_root(owner.home_dir, owner.home_dir),
            access_scope=ProcessAccessScope(owner.owner_id, binding.request.thread_id, str(owner.home_dir)),
            execution_scope=ProcessExecutionScope(str(owner.home_dir), binding.request.thread_id,
                                                  binding.task_id, binding.run_id, binding.attempt_id),
            authority_check=self.authorize, deadline_monotonic=deadline, stop_on_launcher_exit=True,
        )


# LLM: 退出确认与环境准备结果分开；上层原操作记录必须保留未知副作用，不据这个异常自动重试。
# 类用途: 返回安装命令失败或退出未确认的稳定原因，不回显命令、私有路径或安装日志。
class EnvironmentPreparationError(RuntimeError):
    # LLM: 本异常不是状态账；started 表示本次命令是否启动，不代表前面的环境写入已回滚。
    # 函数用途: 保存可供原工具结果使用的失败分类与进程确认信息。
    def __init__(self, reason: str, *, started: bool = False, exit_confirmed: bool = True) -> None:
        super().__init__("插件独立环境准备失败。" if exit_confirmed else "插件环境准备进程退出尚未确认。")
        self.reason = reason
        self.started = started
        self.exit_confirmed = exit_confirmed


# LLM: 复用原密钥擦除并去除 Python/安装器/动态库注入；TMPDIR 只指向本候选，配置禁用是离线安装的必要约束。
# 函数用途: 为准备命令创建受控环境副本，不修改 Gateway 自身环境或用户的全局配置。
def preparation_environment(temporary_directory: Path) -> dict[str, str]:
    cleaned = scrub_subprocess_env(dict(os.environ))
    prefixes = ("PYTHON", "PIP_", "UV_", "CONDA_", "LD_", "DYLD_")
    exact = {"VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "__PYVENV_LAUNCHER__"}
    environment = {
        key: value for key, value in cleaned.items()
        if not key.upper().startswith(prefixes) and key.upper() not in exact
    }
    environment.update(PIP_CONFIG_FILE=os.devnull, TMPDIR=str(temporary_directory), TEMP=str(temporary_directory), TMP=str(temporary_directory))
    return environment


# LLM: 同一 monotonic 截止时间贯穿宿主 I/O 阶段；这是协作式检查，不能宣称可中断任意底层文件系统调用。
# 函数用途: 在准备阶段和有界文件循环中接收取消、拒绝超过期限后继续写入或返回成功。
def check_preparation_deadline(deadline: float) -> None:
    raise_if_cancelled()
    if time.monotonic() >= deadline:
        raise EnvironmentPreparationError("preparation_timeout")


# LLM: argv 只来自宿主；原进程 Store 先预留再启动，交接后仍受原 operation 和期限约束，不能回退裸 Popen。
# 函数用途: 同步等待一个精确托管准备命令，失败只清理本次 session，日志仅保存在私有候选。
def run_environment_process(
    argv: tuple[str, ...], *, cwd: Path, environment: dict[str, str], deadline: float,
    operation: PluginEnvironmentOperation, stage: str, capture: bool = False,
) -> bytes:
    check_preparation_deadline(deadline)
    operation.authorize()
    if stage not in {"venv", "probe", "install"}:
        raise ValueError("未知的宿主环境准备阶段")
    log_path = cwd / f"{stage}.log"
    with log_path.open("xb"):
        pass
    try:
        hosted = start_background_process(
            operation.launch_request(argv, cwd, environment, deadline, log_path),
            startup_timeout_seconds=min(3.0, max(0.1, deadline - time.monotonic())),
        )
    except BackgroundLaunchError as exc:
        if exc.cleanup_confirmed and isinstance(exc.cause, ToolCancelled):
            raise exc.cause from exc
        raise EnvironmentPreparationError("preparation_launch" if exc.cleanup_confirmed else "process_exit_unknown",
                                          started=True, exit_confirmed=exc.cleanup_confirmed) from exc
    store = ProcessSessionStore(hosted.store_root)
    try:
        record = _wait_for_preparation(hosted, store, operation, deadline)
        hosted.process.wait(timeout=2.0)
    except BaseException as exc:
        try:
            receipt = stop_process_session(store, hosted.record, host_process=hosted.process)
        except Exception as cleanup_error:
            raise EnvironmentPreparationError("process_exit_unknown", started=True, exit_confirmed=False) from cleanup_error
        if not receipt.confirmed:
            raise EnvironmentPreparationError("process_exit_unknown", started=True, exit_confirmed=False) from exc
        if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
            raise EnvironmentPreparationError("preparation_timeout", started=True) from exc
        if isinstance(exc, EnvironmentPreparationError):
            raise EnvironmentPreparationError(exc.reason, started=True, exit_confirmed=exc.exit_confirmed) from exc
        raise
    if record["status"] != "exited" or record["exit_code"] != 0:
        raise EnvironmentPreparationError("preparation_command", started=True)
    if not capture:
        return b""
    with log_path.open("rb") as stream:
        output = stream.read(65537)
    if len(output) > 65536:
        raise EnvironmentPreparationError("preparation_probe", started=True)
    return output


# LLM: Store 是退出唯一事实源，Popen 结束不代替成功；等待继续核对原权限，原 host 另行保证宿主消失和期限后的清理。
# 函数用途: 在同步准备期间读取同一进程记录，及时响应取消、丢权和未知结果。
def _wait_for_preparation(hosted, store, operation, deadline: float) -> dict:
    while True:
        check_preparation_deadline(deadline)
        operation.authorize()
        record = _preparation_record(hosted, store)
        if record["status"] in {"exited", "killed", "not_started"}:
            return record
        if record["status"] == "unknown":
            raise EnvironmentPreparationError("process_exit_unknown", started=True, exit_confirmed=False)
        if hosted.process.poll() is not None:
            record = _preparation_record(hosted, store)
            if record["status"] in {"exited", "killed", "not_started"}:
                return record
            raise EnvironmentPreparationError("process_exit_unknown", started=True, exit_confirmed=False)
        time.sleep(0.05)


# LLM: 只读原 session，宿主刚退出时允许复读同一权威；不可用 Popen 的 exit0 替代已提交终态。
# 函数用途: 获取准备进程的可信状态，读取失败保留未知。
def _preparation_record(hosted, store) -> dict:
    report = store.load(str(hosted.record["session_id"]))
    if report.load_error or not report.record:
        raise EnvironmentPreparationError("process_state_unreadable", started=True, exit_confirmed=False)
    return report.record
