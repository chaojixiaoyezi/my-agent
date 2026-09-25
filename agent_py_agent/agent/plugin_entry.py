# LLM: v6 非 Python 插件入口与随包文件的静态声明，只校验形状：不解析宿主路径、不查找解释器、不执行任何文件。
#   入口类型只有宿主支持的两种启动机制（随包可执行文件 / 系统解释器加随包脚本）；平台标记与解释器名是开放集合，只校验格式。
#   改动须同步 plugin_manifest、plugin_package、plugin_files_environment、plugin_runtime_pin 与 test_plugin_any_language。
# 模块用途: 描述非 Python 插件"怎么启动、带哪些文件、能在哪些平台跑"，并给出本机平台标记供启用时比对。

from __future__ import annotations

import platform
import re
import sys
from dataclasses import dataclass

PLUGIN_ENTRY_KINDS = ("executable", "interpreter")
ANY_PLATFORM = "any"
# 随包文件在插件独立环境里的解包目录名，环境准备、启动与随包 Skill 定位共用
FILES_DIRECTORY = "files"
# 与包读取器默认成员上限（128，含 plugin.json）一致，避免描述合法而读包必然超限
MAX_PLUGIN_FILES = 127
MAX_ENTRY_ARGS = 16
_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,63}"
_FILE_PATH = re.compile(rf"(?:{_SEGMENT}/){{0,7}}{_SEGMENT}\Z")
_INTERPRETER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")
_PLATFORM = re.compile(r"[a-z][a-z0-9]{0,15}-[a-z0-9_]{1,15}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ARCH_ALIASES = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}


# LLM: path 是包内相对路径，只用于核对 ZIP 成员和解包位置，不能含 ..、绝对路径、反斜杠或与包描述同名。
# 类用途: 声明一个随包文件及其摘要，executable 决定解包后是否给执行权限。
@dataclass(frozen=True)
class PluginFile:
    path: str
    sha256: str
    executable: bool = False

    # LLM: 摘要格式与 wheel 相同；执行位只能是真布尔值，不从文件内容或扩展名推断。
    # 函数用途: 在读包之前拒绝不安全的路径、错误摘要和非布尔执行标记。
    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not _FILE_PATH.fullmatch(self.path) or self.path == "plugin.json" \
                or any(part in {".", ".."} for part in self.path.split("/")):
            raise ValueError("随包文件路径无效")
        if not isinstance(self.sha256, str) or not _DIGEST.fullmatch(self.sha256):
            raise ValueError("随包文件摘要无效")
        if type(self.executable) is not bool:
            raise ValueError("随包文件执行标记无效")


# LLM: command 指向随包文件；interpreter 只是程序名（不含路径），由宿主在启用时解析并请用户确认，包不能指定宿主绝对路径。
# 类用途: 声明非 Python 插件的启动方式与固定参数。
@dataclass(frozen=True)
class PluginEntry:
    kind: str
    command: str
    args: tuple[str, ...] = ()
    interpreter: str = ""

    # LLM: 参数是固定字符串，不接受宿主占位符或控制字符；interpreter 只在 interpreter 类型出现。
    # 函数用途: 拒绝未知入口类型、不安全的命令路径、过多或含控制字符的参数，以及错配的解释器字段。
    def __post_init__(self) -> None:
        if self.kind not in PLUGIN_ENTRY_KINDS:
            raise ValueError("插件入口类型无效")
        if not isinstance(self.command, str) or not _FILE_PATH.fullmatch(self.command):
            raise ValueError("插件入口文件路径无效")
        if (not isinstance(self.args, tuple) or len(self.args) > MAX_ENTRY_ARGS
                or any(not isinstance(item, str) or not 0 < len(item) <= 512
                       or any(ord(char) < 32 or ord(char) == 127 for char in item) for item in self.args)):
            raise ValueError("插件入口参数无效")
        wants_interpreter = self.kind == "interpreter"
        if wants_interpreter != bool(self.interpreter) or (
                wants_interpreter and (not isinstance(self.interpreter, str) or not _INTERPRETER.fullmatch(self.interpreter))):
            raise ValueError("插件入口解释器声明无效")

    # LLM: 输出字段固定，解释器只在需要时出现，保证同一声明的 JSON 字节稳定。
    # 函数用途: 生成包描述里的 entry 对象。
    def to_payload(self) -> dict:
        return {"kind": self.kind, "command": self.command, "args": list(self.args),
                **({"interpreter": self.interpreter} if self.interpreter else {})}

    # LLM: 严格字段：kind/command/args 必填，interpreter 只允许在 interpreter 类型出现；未知键拒绝。
    # 函数用途: 从包描述 JSON 恢复入口声明。
    @classmethod
    def from_payload(cls, value: object) -> PluginEntry:
        base = {"kind", "command", "args"}
        if not isinstance(value, dict) or not base <= set(value) or set(value) - base - {"interpreter"}:
            raise ValueError("插件入口字段无效")
        if not isinstance(value["args"], list):
            raise ValueError("插件入口参数无效")
        return cls(value["kind"], value["command"], tuple(value["args"]), value.get("interpreter", ""))


# LLM: 文件名按大小写折叠去重（与 ZIP 成员碰撞规则一致）；入口文件必须在清单里，可执行入口必须声明执行权限；
#   skills/ 下的 SKILL.md（大小写折叠比较，任意深度）必须恰好是声明的 skills/<名称>/SKILL.md——Skill 目录按
#   SKILL.md 递归扫描，未声明的也会被暴露，所以两边必须相等。平台至少一个，只接受 any 或"系统-架构"格式。
# 函数用途: 在包描述构造时核对入口、文件清单、平台与 Skill 名单彼此一致。
def validate_entry_files(entry: PluginEntry, files: tuple[PluginFile, ...], platforms: tuple[str, ...],
                         skills: tuple[str, ...]) -> None:
    if not isinstance(files, tuple) or not files or len(files) > MAX_PLUGIN_FILES \
            or any(not isinstance(item, PluginFile) for item in files):
        raise ValueError("随包文件清单无效")
    by_path = {item.path: item for item in files}
    if len({path.casefold() for path in by_path}) != len(files):
        raise ValueError("随包文件重名")
    target = by_path.get(entry.command)
    if target is None or (entry.kind == "executable" and not target.executable):
        raise ValueError("插件入口文件缺失或没有执行权限")
    if (not isinstance(platforms, tuple) or not platforms or len(set(platforms)) != len(platforms) or len(platforms) > 16
            or any(not isinstance(tag, str) or (tag != ANY_PLATFORM and not _PLATFORM.fullmatch(tag)) for tag in platforms)):
        raise ValueError("插件平台声明无效")
    found = {path for path in by_path if path.casefold().startswith("skills/") and path.casefold().endswith("/skill.md")}
    if found != {f"skills/{name}/SKILL.md" for name in skills}:
        raise ValueError("随包 Skill 文件与声明不一致")


# LLM: 只读本进程的系统与架构，不访问网络或文件；常见别名统一（amd64/x64→x86_64，aarch64→arm64），其余原样保留。
# 函数用途: 生成本机平台标记（如 darwin-arm64、linux-x86_64），启用时与包声明的平台比对。
def host_platform_tag() -> str:
    system = "linux" if sys.platform.startswith("linux") else re.sub(r"[^a-z]", "", sys.platform.lower())[:16] or "unknown"
    machine = platform.machine().lower()
    machine = re.sub(r"[^a-z0-9_]", "_", _ARCH_ALIASES.get(machine, machine))[:15] or "unknown"
    return f"{system}-{machine}"


# LLM: any 表示包不依赖平台（例如只含脚本、由系统解释器运行）；其余必须精确等于本机标记。
# 函数用途: 判断包声明的平台是否包含本机。
def platform_supported(platforms: tuple[str, ...]) -> bool:
    return ANY_PLATFORM in platforms or host_platform_tag() in platforms
