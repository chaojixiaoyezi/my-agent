# LLM: 只运行宿主声明的 venv/pip 准备命令，不承接插件业务；取消沿原 token 和出生标识，未知退出不能洗成成功。
# 模块用途: 为一次有期限的依赖准备管理精确子进程，不增加后台会话或第二套停止账本。

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .tooling._subprocess_env_scrub import scrub_subprocess_env
from .tooling.cancellation import raise_if_cancelled
from .tooling.process_registry import capture_process_birth_token, terminate_process_tree


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


# LLM: argv 只能来自宿主准备器且不经 Shell；capture 只用于固定引导探测，不允许捕获插件输出或无界安装日志。
# 函数用途: 启动一个受控命令，响应原取消请求，超时或取消时核对并终止原进程树。
def run_environment_process(
    argv: tuple[str, ...], *, cwd: Path, environment: dict[str, str], deadline: float, capture: bool = False,
) -> bytes:
    check_preparation_deadline(deadline)
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except OSError as exc:
        raise EnvironmentPreparationError("preparation_spawn") from exc
    birth = ""
    try:
        birth = capture_process_birth_token(process.pid)
        output = _wait_for_preparation(process, deadline)
    except BaseException as exc:
        # 已启动进程的所有异常都必须先收尾，再保留原控制信号；空身份只会得到未确认，不猜 PID。
        try:
            receipt = terminate_process_tree(process.pid, process, expected_birth_token=birth)
        except Exception as cleanup_error:
            raise EnvironmentPreparationError("process_exit_unknown", started=True, exit_confirmed=False) from cleanup_error
        if not receipt.confirmed:
            raise EnvironmentPreparationError("process_exit_unknown", started=True, exit_confirmed=False) from exc
        if isinstance(exc, TimeoutError):
            raise EnvironmentPreparationError("preparation_timeout", started=True) from exc
        if isinstance(exc, OSError):
            raise EnvironmentPreparationError("preparation_io", started=True) from exc
        raise
    if process.returncode != 0:
        raise EnvironmentPreparationError("preparation_command", started=True)
    if len(output) > 65536:
        raise EnvironmentPreparationError("preparation_probe", started=True)
    return output


# LLM: 短等待共用全流程截止时间，不能每次重置预算；读取仅针对宿主固定小输出探测，其它命令输出为 DEVNULL。
# 函数用途: 等待环境命令结束并及时接收当前工具的取消信号。
def _wait_for_preparation(process, deadline: float) -> bytes:
    while True:
        raise_if_cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("environment preparation deadline")
        try:
            output, _ = process.communicate(timeout=min(0.15, remaining))
            raise_if_cancelled()
            return output or b""
        except subprocess.TimeoutExpired:
            continue
