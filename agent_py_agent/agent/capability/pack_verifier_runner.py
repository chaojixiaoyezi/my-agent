# LLM: 能力包 v2 第 11 条的唯一执行入口：宿主用“已安装、按 sha 钉住、启用时经管理员确认”的包内原版检查程序检查交付物。
#   成员字节只从安装 blob 读（PluginInstallStore.package_bytes 核整包 sha 与描述，read_plugin_member 核成员 sha），工作区副本一律不用；
#   运行只经唯一沙箱入口 AttemptExecutionSandbox：断网、整根只读、只写本次临时目录；沙箱不可用就不跑，记 sandbox_unavailable。
#   结果只认 stdout 的 pack_verifier_result.v1 结构化字段（valid、errors/warnings 的 code），不解释 message，不读模型自述。
#   本模块不决定返工或展示，调用方（块 3）按 PackVerificationResult 入账、生成有界摘要。改动同步 test_pack_verifier_runner.py。
# 模块用途: 在沙箱里运行能力包声明的检查程序，返回可入账的结构化检查事实。

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..attempt.sandbox import AttemptExecutionSandbox, AttemptSandboxSpec, SandboxUnavailableError
from ..capability_verification_manifest import TARGET_PLACEHOLDER, VerifierDeclaration
from ..capability_verifier_consent import verifier_consent_matches
from ..plugin_content_activation import PluginContentActivation
from ..plugin_install_store import PluginInstallStore
from ..plugin_installation import PluginInstallationError
from ..plugin_manifest import PluginPackageError
from ..plugin_package import inspect_plugin_package, read_plugin_member

PACK_VERIFIER_RESULT_SCHEMA = "pack_verifier_result.v1"
SUPPORTED_VERIFIER_RUNTIMES = frozenset({"python"})
# 检查程序 stdout 的字节上限；超过即判输出无效，防止异常程序把账本撑大。
MAX_VERIFIER_OUTPUT_BYTES = 1_048_576
# 检查程序成员文件的读取上限（包内脚本通常几十 KB）。
MAX_VERIFIER_MEMBER_BYTES = 4_194_304
# 结果里保留的不同错误/警告码个数上限；超出部分只计入总数。
MAX_VERIFIER_DISTINCT_CODES_COUNT = 64
# 有界摘要里列出的前几条错误码个数（写工具回执只附这么多）。
MAX_VERIFIER_SUMMARY_CODES_COUNT = 5
# 沙箱超时回收后返回的退出码（AttemptExecutionSandbox.run 的 TERM→KILL 约定）。
_SANDBOX_TIMEOUT_RC = 143


# LLM: 请求只带宿主已解析的结构化身份与绝对路径；target/baseline 必须是普通文件，由调用方按交付物声明挑出。
# 类用途: 描述一次“用某个包的某个检查程序检查某个交付物”的宿主请求。
@dataclass(frozen=True)
class PackVerifierRequest:
    owner: object
    installation: object
    verifier_id: str
    target: Path
    workspace_root: Path
    baseline: Path | None = None


# LLM: 事实字段是入账的唯一来源；status 只有 passed/failed/not_run/error 四种，reason_code 说明 not_run/error 的结构化原因。
#   计数按 code 聚合且有上限，不保存 message 或 location 正文。
# 类用途: 保存一次宿主检查的结构化结果，并给出入账事实和有界摘要两种投影。
@dataclass(frozen=True)
class PackVerificationResult:
    verifier_id: str
    package_id: str
    status: str
    reason_code: str = ""
    package_version: str = ""
    package_sha256: str = ""
    member: str = ""
    member_sha256: str = ""
    runtime: str = ""
    target: str = ""
    target_sha256: str = ""
    baseline: str = ""
    baseline_sha256: str = ""
    valid: bool | None = None
    returncode: int | None = None
    duration_ms: int = 0
    error_counts: dict[str, int] = field(default_factory=dict)
    warning_counts: dict[str, int] = field(default_factory=dict)

    # 函数用途: 生成写进运行事件和 channel_delivery 的完整结构化事实。
    def to_fact(self) -> dict:
        return {key: (dict(value) if isinstance(value, dict) else value) for key, value in self.__dict__.items()}

    # LLM: 只给通过/失败、错误与警告总数和前几条错误码；不含路径以外的正文，供写工具回执附带。
    # 函数用途: 生成有界的检查摘要。
    def summary(self) -> dict:
        codes = sorted(self.error_counts, key=lambda code: (-self.error_counts[code], code))
        return {"verifier_id": self.verifier_id, "status": self.status, "reason_code": self.reason_code,
                "valid": self.valid, "error_count": sum(self.error_counts.values()),
                "warning_count": sum(self.warning_counts.values()),
                "error_codes": codes[:MAX_VERIFIER_SUMMARY_CODES_COUNT], "target": self.target}


# LLM: 任何一步不满足都返回结构化 not_run/error，不抛异常给调用方，不退回无沙箱执行，也不改用工作区副本。
# 函数用途: 按请求核对同意、取钉住原件、在沙箱里运行并解析检查结果。
def run_pack_verifier(request: PackVerifierRequest) -> PackVerificationResult:
    entry = request.installation
    manifest = entry.manifest
    base = {"verifier_id": request.verifier_id, "package_id": manifest.plugin_id,
            "package_version": manifest.version, "package_sha256": entry.package_sha256}
    verifier = _declared_verifier(manifest, request.verifier_id)
    if verifier is None:
        return PackVerificationResult(**base, status="error", reason_code="verifier_not_declared")
    base.update(member=verifier.member, runtime=verifier.runtime,
                target=_relative(request.target, request.workspace_root))
    refusal = _refusal_reason(entry, verifier)
    if refusal:
        return PackVerificationResult(**base, status="not_run", reason_code=refusal)
    if not request.target.is_file():
        return PackVerificationResult(**base, status="error", reason_code="target_missing")
    try:
        member_bytes = _pinned_member(request, verifier)
    except (PluginInstallationError, PluginPackageError, ValueError):
        return PackVerificationResult(**base, status="error", reason_code="verifier_member_mismatch")
    base.update(member_sha256=hashlib.sha256(member_bytes).hexdigest(), target_sha256=_file_sha256(request.target))
    if request.baseline is not None:
        base.update(baseline=_relative(request.baseline, request.workspace_root),
                    baseline_sha256=_file_sha256(request.baseline))
    return _run_in_sandbox(request, verifier, member_bytes, base)


# LLM: 只有已激活的内容代次、且同意摘要覆盖当前声明，才允许宿主运行包内代码；运行方式不认识的不跑（开放世界：不拒绝安装）。
# 函数用途: 返回不能运行的结构化原因，可以运行时返回空串。
def _refusal_reason(entry, verifier: VerifierDeclaration) -> str:
    activation = getattr(entry, "activation", None)
    if (not isinstance(activation, PluginContentActivation) or activation.phase != "active"
            or not verifier_consent_matches(entry.manifest, entry.package_sha256, activation.verifier_consent_sha256)):
        return "verifier_consent_missing"
    if verifier.runtime not in SUPPORTED_VERIFIER_RUNTIMES:
        return "unsupported_runtime"
    return ""


# LLM: 整包摘要与描述由 package_bytes 复核，成员摘要由 read_plugin_member 复核；任何不符都抛错，调用方记 verifier_member_mismatch。
# 函数用途: 从安装 blob 读出检查程序成员的原始字节。
def _pinned_member(request: PackVerifierRequest, verifier: VerifierDeclaration) -> bytes:
    data = PluginInstallStore(request.owner).package_bytes(request.installation)
    return read_plugin_member(inspect_plugin_package(data), verifier.member, max_bytes=MAX_VERIFIER_MEMBER_BYTES)


# LLM: 临时目录在宿主自己的临时区，结束即删；沙箱整根只读、只写临时目录、断网；超时按 TERM→KILL 回收。
# 函数用途: 把原件写进临时目录，在沙箱里运行并把输出转成结构化结果。
def _run_in_sandbox(request: PackVerifierRequest, verifier: VerifierDeclaration, member_bytes: bytes,
                    base: dict) -> PackVerificationResult:
    temp = Path(tempfile.mkdtemp(prefix="pack-verifier-"))
    try:
        script = temp / "verifier.py"
        script.write_bytes(member_bytes)
        script.chmod(0o600)
        sandbox = AttemptExecutionSandbox(_sandbox_spec(request, temp))
        try:
            # 先显式确认沙箱可用且能断网，再启动进程；不可用时一个进程都不起
            sandbox.require_ready()
            started = time.monotonic()
            completed = sandbox.run(_argv(script, verifier, request), timeout=float(verifier.timeout_seconds))
        except SandboxUnavailableError:
            return PackVerificationResult(**base, status="not_run", reason_code="sandbox_unavailable")
        elapsed = time.monotonic() - started
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    base.update(returncode=completed.returncode, duration_ms=int(elapsed * 1000))
    if completed.returncode == _SANDBOX_TIMEOUT_RC and elapsed >= verifier.timeout_seconds:
        return PackVerificationResult(**base, status="error", reason_code="verifier_timeout")
    return _parsed_result(completed.stdout or "", base)


# 函数用途: 生成断网、整根只读、只写临时目录的沙箱规格。
def _sandbox_spec(request: PackVerifierRequest, temp: Path) -> AttemptSandboxSpec:
    owner_home = Path(getattr(request.owner, "home_dir", request.workspace_root))
    return AttemptSandboxSpec(attempt_view=temp, staging_root=temp, shared_workspace=request.workspace_root,
                              owner_home=owner_home, network_access=False, implicit_attempt_write_roots=False,
                              extra_write_roots=(temp,), read_only_root=True)


# LLM: 解释器用宿主自身的 Python（-I -S：不读环境变量、不加 site），参数模板只替换唯一的 {target}，基线参数名来自包声明。
# 函数用途: 组装在沙箱里运行检查程序的命令行。
def _argv(script: Path, verifier: VerifierDeclaration, request: PackVerifierRequest) -> list[str]:
    args = [str(request.target) if arg == TARGET_PLACEHOLDER else arg for arg in verifier.args]
    if request.baseline is not None and verifier.baseline is not None:
        args.extend((verifier.baseline.flag, str(request.baseline)))
    return [sys.executable, "-I", "-S", str(script), *args]


# LLM: 只认 schema 与 valid 布尔，以及 errors/warnings 里每项的字符串 code；格式不对一律 verifier_output_invalid。
# 函数用途: 把检查程序 stdout 解析成结构化检查结果。
def _parsed_result(stdout: str, base: dict) -> PackVerificationResult:
    payload = _json_object(stdout)
    if payload is None or payload.get("schema") != PACK_VERIFIER_RESULT_SCHEMA or type(payload.get("valid")) is not bool:
        return PackVerificationResult(**base, status="error", reason_code="verifier_output_invalid")
    errors, warnings = _code_counts(payload.get("errors", [])), _code_counts(payload.get("warnings", []))
    if errors is None or warnings is None:
        return PackVerificationResult(**base, status="error", reason_code="verifier_output_invalid")
    return PackVerificationResult(**base, status="passed" if payload["valid"] else "failed", valid=payload["valid"],
                                  error_counts=errors, warning_counts=warnings)


# 函数用途: 在字节上限内把 stdout 解析成 JSON 对象，失败返回 None。
def _json_object(stdout: str) -> dict | None:
    if len(stdout.encode("utf-8")) > MAX_VERIFIER_OUTPUT_BYTES:
        return None
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


# LLM: 每项必须是带非空字符串 code 的对象；不同 code 个数有上限，超出的计入 "_other"。
# 函数用途: 把错误或警告列表聚合成按 code 计数，格式不对返回 None。
def _code_counts(items: object) -> dict[str, int] | None:
    if not isinstance(items, list):
        return None
    counts: dict[str, int] = {}
    for item in items:
        code = item.get("code") if isinstance(item, dict) else None
        if not isinstance(code, str) or not code or len(code) > 128:
            return None
        key = code if code in counts or len(counts) < MAX_VERIFIER_DISTINCT_CODES_COUNT else "_other"
        counts[key] = counts.get(key, 0) + 1
    return counts


# 函数用途: 按编号找包声明里的检查程序。
def _declared_verifier(manifest, verifier_id: str) -> VerifierDeclaration | None:
    capability = getattr(manifest, "capability", None)
    verification = getattr(capability, "verification", None)
    verifiers = verification.verifiers if verification is not None else ()
    return next((item for item in verifiers if item.id == verifier_id), None)


# 函数用途: 计算文件内容的 sha256。
def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# 函数用途: 把绝对路径换成工作区相对路径；不在工作区内时只保留文件名，账本不记宿主绝对路径。
def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name
