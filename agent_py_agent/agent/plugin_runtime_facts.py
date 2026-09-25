# LLM: 非 Python 入口的运行时事实只在宿主侧确定：可执行入口绑定本机平台；解释器入口在启用时按当时 PATH 解析一次，固定
#   绝对路径与文件摘要，写进环境目录的定位文件；每次启动先比文件 stat，变化再比摘要，不一致即拒绝。解析、确认与复核都不执行
#   解释器或包内文件。指纹沿用环境计划的 interpreter_fingerprint 字段（64 位十六进制），不改计划格式。
#   改动须同步 plugin_environment_plan、plugin_files_environment、plugin_runtime、plugin_enable_tool 与 test_plugin_any_language。
# 模块用途: 给启用计划、用户确认回执、环境准备与启动提供同一份"这个插件在本机用什么运行"的事实。

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path

from .common.nofollow_fs import read_bytes_beneath
from .common.strict_json import load_strict_json
from .plugin_entry import host_platform_tag, platform_supported

RUNTIME_PIN_FILE = "runtime.json"
_PIN_SCHEMA = "plugin_runtime_pin.v1"
_FACT_FIELDS = ("kind", "platform", "interpreter_name", "interpreter_path", "interpreter_sha256")


# LLM: reason 是稳定机器分类，消息是给人看的中文说明；不回显 PATH 全文或包内容。
# 类用途: 表示平台不支持、解释器找不到或解释器已变化等运行时事实问题。
class PluginRuntimeError(ValueError):
    # 函数用途: 保存失败分类与中文说明。
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# LLM: 事实只含宿主解析的结构化值；指纹覆盖入口类型、本机平台、解释器绝对路径与内容摘要，任一变化都换指纹。
# 类用途: 描述一个非 Python 插件在本机的实际运行方式。
@dataclass(frozen=True)
class PluginRuntimeFacts:
    kind: str
    platform: str
    interpreter_name: str = ""
    interpreter_path: str = ""
    interpreter_sha256: str = ""

    # LLM: 与环境计划的 interpreter_fingerprint 同格式；环境引用由它参与生成，指纹变化即对应新环境。
    # 函数用途: 生成这份运行时事实的固定指纹。
    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps([getattr(self, name) for name in _FACT_FIELDS]).encode()).hexdigest()


# LLM: 只读文件系统与本进程 PATH，不执行解释器（连 --version 也不跑）；解释器必须解析成绝对路径的普通可执行文件，
#   且内容可读（要固定摘要）。deadline 由准备阶段传入，读取大文件时协作检查期限与取消。
# 函数用途: 在启用和准备时确定本机运行事实；平台不支持或解释器找不到时给出稳定原因。
def resolve_plugin_runtime(manifest, *, search_path: str | None = None, deadline: float | None = None) -> PluginRuntimeFacts:
    entry = manifest.entry
    if entry is None:
        raise PluginRuntimeError("not_file_entry", "这是 Python 插件，没有非 Python 运行时事实。")
    if not platform_supported(manifest.platforms):
        raise PluginRuntimeError("platform_unsupported", "插件包声明的平台不包含本机。")
    platform = host_platform_tag()
    if entry.kind == "executable":
        return PluginRuntimeFacts("executable", platform)
    found = shutil.which(entry.interpreter, path=os.environ.get("PATH", "") if search_path is None else search_path)
    real = os.path.realpath(found) if found else ""
    try:
        usable = bool(real) and os.path.isabs(real) and stat.S_ISREG(os.stat(real).st_mode) and os.access(real, os.X_OK)
    except OSError:
        usable = False
    if not usable:
        raise PluginRuntimeError("interpreter_not_found", f"没有在 PATH 中找到可执行的解释器 {entry.interpreter}。")
    try:
        digest = _file_sha256(real, deadline)
    except OSError as exc:
        raise PluginRuntimeError("interpreter_unreadable", f"解释器 {entry.interpreter} 不可读取，无法固定其内容摘要。") from exc
    return PluginRuntimeFacts("interpreter", platform, entry.interpreter, real, digest)


# LLM: 分块读取，deadline 存在时每块检查期限与取消；只读普通文件内容，不执行。
# 函数用途: 计算解释器文件的 sha256。
def _file_sha256(path: str, deadline: float | None) -> str:
    from .plugin_environment_process import check_preparation_deadline

    checkpoint = (lambda: None) if deadline is None else partial(check_preparation_deadline, deadline)
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checkpoint()
            digest.update(chunk)
    return digest.hexdigest()


# LLM: 定位文件只在 interpreter 类型写入；排他创建、不跟随链接、仅属主可读写。它只是定位，可信度由启动时与计划指纹比对来保证。
# 函数用途: 在准备好的环境目录里记录解释器绝对路径、摘要和当时的文件 stat。有副作用：写一个文件。
def write_runtime_pin(candidate: Path, facts: PluginRuntimeFacts) -> None:
    if facts.kind != "interpreter":
        return
    info = os.stat(facts.interpreter_path)
    payload = {"schema": _PIN_SCHEMA, **asdict(facts), "stat": [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]}
    descriptor = os.open(candidate / RUNTIME_PIN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True)


# LLM: 可执行入口只需本机平台与计划一致；解释器入口从定位文件恢复事实，指纹必须等于计划；stat 变化时重新计算摘要，
#   内容不同、文件消失或不可读都按"已变化"拒绝（需要重新启用并确认）。只读，不执行任何程序。
# 函数用途: 启动插件前复核运行时事实，返回要执行的解释器绝对路径（可执行入口返回空串）。
def verified_runtime_command(owner_root: Path, environment: Path, kind: str, fingerprint: str) -> str:
    if kind == "executable":
        if PluginRuntimeFacts("executable", host_platform_tag()).fingerprint != fingerprint:
            raise PluginRuntimeError("platform_changed", "插件环境不是为本机平台准备的，请重新启用。")
        return ""
    relative = (*environment.relative_to(owner_root).parts, RUNTIME_PIN_FILE)
    try:
        raw = read_bytes_beneath(owner_root, relative, max_bytes=64 * 1024)
        payload = load_strict_json(raw) if raw is not None else None
    except (OSError, ValueError) as exc:
        raise PluginRuntimeError("interpreter_pin_invalid", "插件解释器定位记录无法读取，请重新启用。") from exc
    if not isinstance(payload, dict) or payload.get("schema") != _PIN_SCHEMA:
        raise PluginRuntimeError("interpreter_pin_invalid", "插件解释器定位记录无效，请重新启用。")
    facts = PluginRuntimeFacts(**{name: payload.get(name) for name in _FACT_FIELDS})
    if facts.kind != "interpreter" or facts.fingerprint != fingerprint or facts.platform != host_platform_tag():
        raise PluginRuntimeError("interpreter_pin_invalid", "插件解释器定位记录与启用计划不符，请重新启用。")
    try:
        info = os.stat(facts.interpreter_path)
        unchanged = ([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns] == payload.get("stat")
                     or _file_sha256(facts.interpreter_path, None) == facts.interpreter_sha256)
    except OSError:
        unchanged = False
    if not unchanged:
        raise PluginRuntimeError("interpreter_changed", "插件使用的解释器已经变化或不可用，请重新启用并确认。")
    return facts.interpreter_path


# 结构化失败原因 → 给用户看的中文说明；启用失败与显式调用拒绝共用，原因码本身才是机器判断依据
_REASON_MESSAGES = {
    "platform_unsupported": "这个插件包没有为本机平台构建（包声明的平台不含本机），不能启用；请安装为本机平台构建的包。",
    "environment_platform_unsupported": "这个插件包没有为本机平台构建（包声明的平台不含本机），不能启用；请安装为本机平台构建的包。",
    "interpreter_not_found": "没有在 Gateway 的 PATH 里找到插件需要的解释器；请先安装它（或让 Gateway 的 PATH 能找到它）再启用。",
    "environment_interpreter_not_found": "没有在 Gateway 的 PATH 里找到插件需要的解释器；请先安装它（或让 Gateway 的 PATH 能找到它）再启用。",
    "interpreter_unreadable": "插件需要的解释器文件不可读取，无法核对其内容，不能启用。",
    "environment_interpreter_unreadable": "插件需要的解释器文件不可读取，无法核对其内容，不能启用。",
    "environment_interpreter_changed": "确认之后解释器发生了变化，本次没有启用；请重新执行 /plugins enable 查看新的确认回执。",
    "interpreter_changed": "插件使用的解释器已被替换或删除，为安全起见没有启动；请先停用该插件（/plugins disable <插件ID>），"
                           "再 /plugins enable 查看新的确认回执并重新确认。",
    "interpreter_pin_invalid": "插件解释器的定位记录与启用时不符，为安全起见没有启动；请先停用该插件，再重新启用并确认。",
    "platform_changed": "插件环境不是为本机平台准备的，没有启动；请先停用该插件，再重新启用并确认。",
}


# LLM: 只把已知的结构化原因码翻成中文说明，未知原因返回空串（调用方沿用原通用文案），不从说明文字反推任何状态。
# 函数用途: 给启用失败或调用被拒的回执取一句具体的中文说明。
def runtime_reason_message(reason: object) -> str:
    return _REASON_MESSAGES.get(reason, "") if isinstance(reason, str) else ""


# LLM: 只读复核已发布激活的运行事实（与启动前同一函数），不启动进程；Python 包与未激活安装返回空串。
# 函数用途: 显式调用插件前检查解释器/平台是否仍是用户确认时的样子，返回失败原因码。
def runtime_problem(owner, installation) -> str:
    activation = installation.activation
    if installation.manifest.entry is None or activation is None:
        return ""
    environment = owner.plugins_dir / "environments" / activation.plan.environment_ref
    try:
        verified_runtime_command(owner.root, environment, installation.manifest.entry.kind,
                                 activation.plan.interpreter_fingerprint)
    except PluginRuntimeError as exc:
        return exc.reason
    return ""


# LLM: 回执只列宿主解析出的结构化事实与随包文件元数据；确认码由这些事实生成，任一变化（包、平台、解释器）都会作废旧码。
# 函数用途: 生成"需要用户确认"回执的详细内容。
def confirmation_details(manifest, package_sha256: str, facts: PluginRuntimeFacts, files) -> dict:
    return {
        "plugin_id": manifest.plugin_id, "version": manifest.version, "package_sha256": package_sha256,
        "entry": manifest.entry.to_payload(), "platform": facts.platform, "declared_platforms": list(manifest.platforms),
        "files": [{"path": item.declaration.path, "sha256": item.declaration.sha256, "size": len(item.content),
                   "executable": item.declaration.executable, **({"shebang": item.shebang} if item.shebang else {})}
                  for item in files],
        **({"interpreter": {"name": facts.interpreter_name, "path": facts.interpreter_path,
                            "sha256": facts.interpreter_sha256}} if facts.kind == "interpreter" else {}),
    }


# LLM: 确认码只是这些事实的短摘要，不是密钥或授权令牌；用户必须在自己的命令里原样输入才会继续启用。
# 函数用途: 由确认内容生成 12 位确认码。
def confirmation_code(details: dict) -> str:
    return hashlib.sha256(json.dumps(details, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]


# LLM: 只把确认回执里的结构化事实排成中文文本给人看，不参与任何机器判断；文件多时只列前 20 个并注明总数。
# 函数用途: 生成 TUI 里展示的"启用前请确认"说明，末行给出带确认码的命令。
def confirmation_message(confirmation: dict) -> str:
    entry = confirmation.get("entry") or {}
    lines = ["启用前需要你确认：这个插件会以你本人的权限在本机运行下面的程序。",
             f"插件：{confirmation.get('plugin_id')} {confirmation.get('version')}（包摘要 {str(confirmation.get('package_sha256'))[:16]}…）"]
    interpreter = confirmation.get("interpreter")
    if interpreter:
        lines.append(f"入口：用系统解释器 {interpreter.get('name')}（{interpreter.get('path')}，"
                     f"摘要 {str(interpreter.get('sha256'))[:16]}…）运行随包脚本 {entry.get('command')}")
    else:
        lines.append(f"入口：随包可执行文件 {entry.get('command')}")
    if entry.get("args"):
        lines.append("固定参数：" + " ".join(entry["args"]))
    lines.append(f"本机平台：{confirmation.get('platform')}（包声明：{'、'.join(confirmation.get('declared_platforms') or [])}）")
    files = confirmation.get("files") or []
    lines.append(f"随包文件（{len(files)} 个）：")
    for item in files[:20]:
        shebang = f"，首行 {item['shebang']}" if item.get("shebang") else ""
        lines.append(f"  - {item.get('path')}  {'可执行' if item.get('executable') else '数据'}  {item.get('size')} 字节"
                     f"  sha256 {str(item.get('sha256'))[:16]}…{shebang}")
    if len(files) > 20:
        lines.append(f"  …… 另有 {len(files) - 20} 个文件")
    lines.append(f"确认无误后输入：/plugins enable {confirmation.get('plugin_id')} --confirm {confirmation.get('confirm_code')}")
    return "\n".join(lines)
