# LLM: 插件进程 OS 沙箱试点（配置 plugin_process_sandbox，默认关）：复用唯一的 AttemptExecutionSandbox 网关
#   （Linux bwrap 整根只读形态 / macOS Seatbelt），读范围与宿主相同，写只落在该插件自己的数据目录；网络不变。
#   沙箱不可用时由调用方在启动前结构化拒绝（sandbox_unavailable），绝不退回无沙箱启动。
#   改动须同步 plugin_runtime、plugin_enable_tool、plugin_management、tooling/sandbox 与 test_plugin_sandbox。
# 模块用途: 给插件启动命令套上平台沙箱，并提供启动前的沙箱就绪检查。

from __future__ import annotations

from pathlib import Path

from .attempt.sandbox import AttemptExecutionSandbox, AttemptSandboxSpec, SandboxUnavailableError

# 插件数据目录下给沙箱内进程用的临时目录名；TMPDIR 指向它，保证临时文件也落在唯一可写处
SANDBOX_TMP_DIRECTORY = ".tmp"


# LLM: 规格只由宿主事实推出：cwd 是插件环境目录（只读），唯一写根是插件数据目录；不接受包声明的路径。
# 函数用途: 为一个插件进程生成沙箱规格。
def plugin_sandbox_spec(*, cwd: Path, data_dir: Path, owner_home: Path) -> AttemptSandboxSpec:
    return AttemptSandboxSpec(
        attempt_view=cwd, staging_root=data_dir, shared_workspace=owner_home, owner_home=owner_home,
        extra_write_roots=(data_dir,), implicit_attempt_write_roots=False, read_only_root=True,
    )


# LLM: 先做平台就绪自检（结果按平台缓存），不就绪抛 SandboxUnavailableError；只构造参数，不启动进程。
# 函数用途: 把插件启动命令包进平台沙箱，返回新的完整 argv。
def sandboxed_plugin_argv(argv: list[str], *, cwd: Path, data_dir: Path, owner_home: Path) -> list[str]:
    return AttemptExecutionSandbox(plugin_sandbox_spec(cwd=cwd, data_dir=data_dir, owner_home=owner_home)).build_argv(argv)


# LLM: 只读平台自检结论，不启动插件；开关关闭时恒为空串。供启用与显式调用在建运行/起进程之前结构化拒绝。
# 函数用途: 沙箱开关打开时检查本机沙箱是否可用，不可用返回原因码 sandbox_unavailable。
def plugin_sandbox_problem(enabled: bool, owner_home: Path) -> str:
    if not enabled:
        return ""
    try:
        AttemptExecutionSandbox(plugin_sandbox_spec(cwd=owner_home, data_dir=owner_home, owner_home=owner_home)).require_ready()
    except SandboxUnavailableError:
        return "sandbox_unavailable"
    return ""
