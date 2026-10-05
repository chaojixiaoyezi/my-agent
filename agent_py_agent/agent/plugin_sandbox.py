# LLM: 旧版插件进程沙箱受 plugin_process_sandbox 控制，复用 AttemptExecutionSandbox；启用沙箱时保留 G1 凭据路径的 H2 隐藏。
#   v8 事件插件无条件强制沙箱，默认 hide_home：隐藏 Gateway 用户家目录，仅放行插件环境、插件数据和核验过的解释器前缀。
#   旧格式受限策略可显式选择 allowlist；系统必要读根按平台窄列，不能借 /、/Users、/home 或 /private 扩大底图。
#   v8 与旧插件受限策略共用同一 AttemptSandboxSpec 工厂；network:false 全断网，network:true 仍受 G4/G5 端口边界约束。
#   沙箱不可用时调用方须在启动前结构化拒绝，不能退回无沙箱启动。
#   allowlist cwd 覆盖判定与同步 Attempt.run 共用 tooling/sandbox，不在两条入口各写一套。
#   改动须同步 plugin_runtime、plugin_enable_tool、plugin_management、tooling/sandbox 与测试。
# 模块用途: 为插件候选、业务与面板共用平台沙箱规格，并提供启动前能力检查。

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from pathlib import Path

from .attempt.sandbox import (
    AttemptExecutionSandbox,
    AttemptSandboxSpec,
    SandboxReadMode,
    SandboxUnavailableError,
    gateway_bound_ports,
    system_read_roots_for_platform,
)
from .gateway_parts.local_client_token import local_client_credential_path
from .path_access_policy import agent_home_root_for_owner
from .tooling.sandbox import allowlisted_directory

# 插件数据目录下给沙箱内进程用的临时目录名；TMPDIR 指向它，保证临时文件也落在唯一可写处
SANDBOX_TMP_DIRECTORY = ".tmp"
PLUGIN_GATEWAY_PORT_ISOLATION_ERROR_CODE = "PLUGIN_GATEWAY_PORT_ISOLATION_UNAVAILABLE"


# LLM: v8 事件插件的安全底座选项（B7）：隐藏根是 Gateway 用户家目录；公开只读根只来自解释器前缀，端口只来自 G4 登记表。
#   只由宿主事实构造，不接受包声明路径；None 根对 v8 是不可用，不能解释为放行。
# 类用途: 把 v8 的断网与收窄读两项打包，避免 spec 构造参数过多。
@dataclass(frozen=True)
class PluginV8Sandbox:
    network: bool
    hidden_read_root: Path | None
    public_read_roots: tuple[Path, ...] = ()


# LLM: 仅承载宿主核验后的有限读、写、程序根、网络和读模式事实；hide_home 保持 B7 原行为，allowlist 不隐式公开 cwd。
#   程序根可为单文件，构造规格时不扩大到其父目录；平台系统根由沙箱底座按结构化清单另行加入。
# 类用途: 为 v8 与旧格式策略共用的沙箱工厂提供受限权限输入。
@dataclass(frozen=True)
class PluginRestrictedSandbox:
    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]
    execute_roots: tuple[Path, ...]
    network: bool
    hidden_read_root: Path | None
    read_mode: SandboxReadMode = "hide_home"


PluginSandboxPolicy = PluginV8Sandbox | PluginRestrictedSandbox


# LLM: 启用时的结构化运行时事实；path 保留 PATH 命中的别名，realpath 是实际固定启动文件，两者都要避开 my-agent 数据根。
# 类用途: 暂存一个插件解释器的候选路径和已解析路径，供 v8 权限裁决使用。
@dataclass(frozen=True)
class PluginInterpreterPaths:
    name: str
    path: Path | None = None
    realpath: Path | None = None


# LLM: None 仅表示旧版宽读；v8 与显式受限策略均汇入同一 builder，隐藏根、有限根和网络事实不从包路径猜测。
# 函数用途: 为插件进程生成统一 AttemptSandboxSpec，保留旧版宽读兼容分支。
def plugin_sandbox_spec(*, cwd: Path, data_dir: Path, owner_home: Path,
                        sandbox_policy: PluginSandboxPolicy | None = None) -> AttemptSandboxSpec:
    credential = local_client_credential_path(agent_home_root_for_owner(owner_home) or owner_home)
    if sandbox_policy is None:
        return AttemptSandboxSpec(
            attempt_view=cwd, staging_root=data_dir, shared_workspace=owner_home, owner_home=owner_home,
            extra_write_roots=(data_dir,), implicit_attempt_write_roots=False, read_only_root=True,
            hidden_paths=(credential, credential.parent),
        )
    if isinstance(sandbox_policy, PluginV8Sandbox):
        sandbox_policy = PluginRestrictedSandbox(
            read_roots=sandbox_policy.public_read_roots,
            write_roots=(),
            execute_roots=(),
            network=sandbox_policy.network,
            hidden_read_root=sandbox_policy.hidden_read_root,
            read_mode="hide_home",
        )
    return plugin_restricted_sandbox_spec(
        cwd=cwd, data_dir=data_dir, owner_home=owner_home, policy=sandbox_policy
    )


# LLM: 这是候选预检、插件业务连接与面板连接共同使用的受限规格入口；根须已由宿主权限层核验且必须仍存在。
#   目录根只开放该目录；文件型 execute_root 不带父目录；cwd 根覆盖判定与 Attempt.run 共用同一函数。
# 函数用途: 将有限 R/W/E 根、网络开关和隐藏根投影为跨平台 AttemptSandboxSpec。
def plugin_restricted_sandbox_spec(
    *, cwd: Path, data_dir: Path, owner_home: Path, policy: PluginRestrictedSandbox
) -> AttemptSandboxSpec:
    if type(policy.network) is not bool:
        raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: RESTRICTED_NETWORK_INVALID")
    if policy.read_mode not in ("hide_home", "allowlist"):
        raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: RESTRICTED_READ_MODE_INVALID")
    hidden_root = (
        _required_directory(policy.hidden_read_root)
        if policy.read_mode == "hide_home" or policy.hidden_read_root is not None
        else None
    )
    data_root = _required_directory(data_dir)
    public_roots = _verified_access_roots((*policy.read_roots, *policy.execute_roots))
    write_roots = _verified_access_roots((*policy.write_roots, data_root))
    system_roots = (
        system_read_roots_for_platform()
        if policy.read_mode == "allowlist"
        else ()
    )
    if hidden_root is not None and any(
        hidden_root.is_relative_to(Path(root).resolve(strict=True))
        for root in (*system_roots, *public_roots, *write_roots)
    ):
        raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: RESTRICTED_ROOT_COVERS_HIDDEN_ROOT")
    if policy.read_mode == "allowlist" and not allowlisted_directory(
        cwd, (*system_roots, *public_roots, *write_roots)
    ):
        raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: RESTRICTED_CWD_NOT_ALLOWLISTED")
    credential = local_client_credential_path(agent_home_root_for_owner(owner_home) or owner_home)
    return AttemptSandboxSpec(
        attempt_view=cwd, staging_root=data_dir, shared_workspace=cwd, owner_home=cwd,
        extra_write_roots=write_roots, implicit_attempt_write_roots=False, read_only_root=True,
        hidden_paths=(credential, credential.parent), network_access=policy.network,
        private_read_roots=(hidden_root,) if policy.read_mode == "hide_home" else (),
        public_read_roots=public_roots, read_mode=policy.read_mode, system_read_roots=system_roots,
        deny_gateway_ports=gateway_bound_ports(),
    )


# LLM: 从 owner 布局、Gateway 进程和固定解释器事实推出唯一 v8 沙箱白名单；任何根或前缀不完整都返回 sandbox_unavailable。
#   解释器候选路径及 realpath 若落在 my-agent 数据根内，必须以 interpreter_inside_hidden_root 拒绝，不能靠白名单重新露出。
# 函数用途: 为插件构造隐藏 Gateway 家目录、已存在的安装前缀及 G4 Gateway 端口规则。
def plugin_v8_sandbox_options(
    owner_home: Path,
    network: bool,
    interpreter: PluginInterpreterPaths | None = None,
) -> tuple[PluginV8Sandbox | None, str]:
    data_root = agent_home_root_for_owner(owner_home)
    try:
        data_root = _required_directory(data_root)
        hidden_root = _required_directory(Path.home())
        prefixes = _interpreter_prefixes(interpreter)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None, "sandbox_unavailable"
    try:
        if interpreter is not None and _interpreter_path_in_root(interpreter, data_root):
            return None, "interpreter_inside_hidden_root"
    except (OSError, RuntimeError, TypeError, ValueError):
        return None, "sandbox_unavailable"
    if any(_is_relative_to(prefix, data_root) for prefix in prefixes):
        return None, "interpreter_inside_hidden_root"
    return PluginV8Sandbox(
        network=network,
        hidden_read_root=hidden_root,
        public_read_roots=prefixes,
    ), ""


# LLM: 拒绝缺失、悬空或非目录路径；沙箱白名单不能把不存在的根当成“无需挂载”。
# 函数用途: 将一个宿主路径核成已存在目录，否则以沙箱不可用失败关闭。
def _required_directory(path: object) -> Path:
    if path is None:
        raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: V8_READ_ROOT_UNAVAILABLE")
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: V8_READ_ROOT_UNAVAILABLE") from exc
    if not resolved.is_dir():
        raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: V8_READ_ROOT_UNAVAILABLE")
    return resolved


# LLM: 显式解释器目录需同时公开原路径与严格 realpath；symlink 别名在隐藏家目录下仍需要自身读授权。
# 函数用途: 校验解释器相关路径为已存在目录，并返回原路径与真实目录。
def _read_directory_paths(path: object) -> tuple[Path, ...]:
    original = Path(path).expanduser().absolute()
    resolved = _required_directory(original)
    return tuple(dict.fromkeys((original, resolved)))


# LLM: R/W/E 根必须仍指向普通文件或目录；保留输入别名和 strict realpath，文件根只授权文件本身。
# 函数用途: 校验受限策略路径并返回原路径/真实路径配对。
def _verified_access_roots(raw_roots: tuple[Path, ...]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for raw in raw_roots:
        try:
            original = Path(raw).expanduser().absolute()
            resolved = original.resolve(strict=True)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: RESTRICTED_ROOT_UNAVAILABLE") from exc
        if not (resolved.is_dir() or resolved.is_file()):
            raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: RESTRICTED_ROOT_UNAVAILABLE")
        roots = list(dict.fromkeys((*roots, original, resolved)))
    return tuple(roots)


# LLM: Python 使用 Gateway 的 sys.prefix/base_prefix；Node 使用真实可执行文件的两级上级；未知运行时不能扩大读范围。
# 函数用途: 返回当前插件解释器实际需要读取的安装目录，任何目录缺失都视为沙箱不可用。
def _interpreter_prefixes(interpreter: PluginInterpreterPaths | None) -> tuple[Path, ...]:
    if interpreter is None:
        return _python_runtime_prefixes()
    name = interpreter.name.casefold()
    if "python" in name:
        return _python_runtime_prefixes()
    if name not in {"node", "nodejs"} or interpreter.realpath is None:
        raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: INTERPRETER_PREFIX_UNAVAILABLE")
    executable = Path(interpreter.realpath).expanduser().resolve(strict=True)
    if len(executable.parents) < 2:
        raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: INTERPRETER_PREFIX_UNAVAILABLE")
    return (_required_directory(executable.parents[1]),)


# LLM: Python 前缀只来自当前 Gateway 解释器报告的 sys.prefix/sys.base_prefix；保留原路径与 realpath，避免符号链接别名在家目录拒读下失效。
# 函数用途: 返回 Python 虚拟环境和基础解释器的已存在安装目录及真实路径。
def _python_runtime_prefixes() -> tuple[Path, ...]:
    prefixes = []
    for value in (sys.prefix, sys.base_prefix):
        prefixes.extend(_read_directory_paths(value))
    return tuple(dict.fromkeys(prefixes))


# LLM: 同时检查 PATH 命中别名和启动时固定的真实路径；符号链接别名落在数据根内也不能经 realpath 绕开拒绝。
# 函数用途: 判断候选解释器或其真实目标是否位于 my-agent 数据根。
def _interpreter_path_in_root(interpreter: PluginInterpreterPaths, root: Path) -> bool:
    candidates = (interpreter.path, interpreter.realpath)
    for candidate in candidates:
        if candidate is None:
            continue
        path = Path(candidate).expanduser().absolute()
        if _is_relative_to(path, root) or _is_relative_to(path.resolve(strict=True), root):
            return True
    return False


# LLM: 路径比较只在真实路径上做祖先关系，不用字符串前缀，避免 sibling 名称相似造成错判。
# 函数用途: 判断路径是否位于指定目录内。
def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


# LLM: 规格由调用方用 plugin_sandbox_spec 从宿主事实构造后传入（唯一 spec 工厂，避免这里再长一套参数）；
#   构造 AttemptExecutionSandbox 时做平台就绪自检，不就绪抛 SandboxUnavailableError；只构造参数，不启动进程。
# 函数用途: 把插件启动命令按给定沙箱规格包进平台沙箱，返回新的完整 argv。
def sandboxed_plugin_argv(argv: list[str], spec: AttemptSandboxSpec) -> list[str]:
    return AttemptExecutionSandbox(spec).build_argv(argv)


# LLM: 只读平台自检结论，不启动插件；开关关闭时恒为空串。供启用与显式调用在建运行/起进程之前结构化拒绝。
# 函数用途: 沙箱开关打开时检查本机沙箱是否可用，不可用返回原因码 sandbox_unavailable。
# LLM: 启用策略按类型结构化传入，网络与受限根检查不读取展示文本；候选、业务和面板最终使用同一规格工厂。
# 函数用途: 沙箱启用时检查平台能力；网络端口隔离不可用或规格缺失时返回结构化原因。
def plugin_sandbox_problem(
    enabled: bool, owner_home: Path, *, sandbox_policy: PluginSandboxPolicy | None = None
) -> str:
    if not enabled:
        return ""
    if sandbox_policy is not None and sandbox_policy.network and platform.system() == "Linux":
        return "gateway_port_isolation_unavailable"
    try:
        spec = plugin_sandbox_spec(
            cwd=owner_home, data_dir=owner_home, owner_home=owner_home, sandbox_policy=sandbox_policy
        )
        AttemptExecutionSandbox(spec).require_ready()
    except SandboxUnavailableError:
        return "sandbox_unavailable"
    return ""
