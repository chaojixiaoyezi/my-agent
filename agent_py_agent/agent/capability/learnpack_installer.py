# LLM: learnpack 安装驱动：把"装她打的某个包"变成管理员同一串 /plugins 宿主命令（安装或停用+更新 → 启用 → 必要时确认），
#   全部经调用方给的 PluginManagement.command 执行，所以账本、幂等、/plugins status 与管理员手敲完全一样，这里不碰安装表。
#   代确认按预览类型白名单判断（复审 M1）：能力包检查程序预览（capability_verifiers）可代确认；老格式/文件型插件权限预览
#   只在 mode=restricted 且不带联网、读/写目录、包外程序目录时可代确认；事件插件预览（顶层 network/events/tool_gates）
#   和认不出的类型一律交给用户。代确认还要 consent.run_programs 为真。某条命令抛异常时不往外抛，记成"结果未确认"并保留
#   已发出的请求编号（复审 M3）。谁能调用、开关是否打开由调用方裁决；包名归属由调用方给的 guard 在读安装表的同一次快照上裁决，
#   她的包进了安装表之后、启用之前回调 on_installed（记包名归属；插件数据目录只在启用及之后才建）。
#   改命令文字要同步 command_catalog 的 /plugins 动作与 test_learnpack_install。
# 模块用途: 宿主替用户把"安装 + 启用"这几条现成命令按顺序执行完，并如实说明停在哪一步、下一步要用户发什么。
from __future__ import annotations

import shlex
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from ..capability_verifier_consent import CONSENT_KIND as VERIFIER_CONSENT_KIND
from .learnpack_store import BuildRecord

# 安装成功并已启用。
STATE_ENABLED = "installed_enabled"
# 已装上但停在启用确认：要用户发 next_command（含联网/读写目录授权，或未获代确认许可）。
STATE_NEEDS_USER = "installed_needs_user_confirmation"
# 某一步宿主命令明确没成功。
STATE_FAILED = "failed"
# 某一步宿主命令抛了异常，结果没法确认（可能已部分执行）。
STATE_UNKNOWN = "outcome_unknown"
# 老格式/文件型插件权限预览的类型标记（plugin_permissions/grants.permission_details 写入）。
_LEGACY_PERMISSION_KIND = "plugin_legacy_permissions"
# 内部步骤状态（不对外）：进度没能落盘、这条命令没有发出；包名归属没记下、没有启用。
_NOT_SENT = "not_sent"
_NOT_RECORDED = "owner_not_recorded"
# 内部步骤状态对应的如实说明。
_INTERNAL_FAILURES = {_NOT_SENT: "没能先把安装进度记进宿主存储，这一步和后面的步骤都没有执行。",
                      _NOT_RECORDED: "包已经装进安装表，但没能记下包名归属，所以没有启用；再装一次会补记并启用。"}


# LLM: run_programs：是否代用户确认"运行她自己做的程序"，来源只能是插件开关现读值或用户发回的确认行；
#   user_session：执行命令的管理服务是不是用户自己的会话。老格式权限确认码绑定发起会话，自动装（learnpack 线程）停下时
#   要给用户一条在他自己会话里能用的启用命令（复审 S4）。
# 类用途: 一次安装里宿主可以代确认的范围和执行会话。
@dataclass(frozen=True)
class InstallConsent:
    run_programs: bool
    user_session: bool


# LLM: source_path 是宿主 learnpack 存储里的包路径（模型写不进去）；on_step 在每条命令发出前收到 (请求编号, 命令文字)，
#   供确认单把进度先落盘（进程中途退出也能如实回放）；guard 拿到与安装同一次读到的整张安装表快照，返回拒绝结果就什么都不做
#   （同名外来包、只差大小写、残留的别家数据目录，由调用方按结构化事实判断）；on_installed 在她的包进了安装表之后、启用之前
#   调用（记包名归属），抛 OSError 就不启用。
# 类用途: 一次安装驱动的输入。
@dataclass(frozen=True)
class InstallRequest:
    record: BuildRecord
    source_path: str
    consent: InstallConsent
    on_step: Callable[[str, str], None] | None = None
    guard: Callable[[tuple], InstallOutcome | None] | None = None
    on_installed: Callable[[], None] | None = None


# LLM: request_ids 是这次实际发出的全部宿主命令编号（宿主改用授权编号时记宿主的）；next_command 是要用户原样发的下一行；
#   previous_enabled 表示装之前同名的旧版本是否在启用中（停在中途时要告诉用户旧版已停用）。
# 类用途: 一次安装驱动的结构化结果。
@dataclass(frozen=True)
class InstallOutcome:
    state: str
    package_id: str
    message: str
    next_command: str = ""
    error_code: str = ""
    request_ids: tuple[str, ...] = field(default_factory=tuple)
    previous_sha256: str = ""
    previous_enabled: bool = False


# LLM: manager 是已按管理员身份组装好的 PluginManagement。同一包已装同一字节：跳过安装只补启用；已装旧版本：先停用（若启用）
#   再更新；没装：安装。全程按顺序、遇失败即停，异常记为结果未确认。有副作用：经宿主命令改安装表、启用插件。
# 函数用途: 按顺序执行安装与启用命令，返回停在哪一步。
def run_install(manager: object, request: InstallRequest) -> InstallOutcome:
    record = request.record
    entries = tuple(manager.installations.snapshot())
    refused = request.guard(entries) if request.guard is not None else None
    if refused is not None:
        return refused
    existing = next((row for row in entries if row.manifest.plugin_id == record.package_id), None)
    runner = _Runner(manager, request, existing)
    for text in _install_steps(record.package_id, request.source_path, existing, record.sha256):
        result = runner.send(text)
        if not result.get("ok"):
            return runner.failed(result)
    unrecorded = runner.mark_installed()
    if unrecorded is not None:
        return runner.failed(unrecorded)
    return _enable(runner, request.consent)


# LLM: 已装同一字节则无需安装；已装其它版本要先停用（启用中时）再 update；纯函数，只拼命令文字。
# 函数用途: 算出启用前要执行的安装类命令。
def _install_steps(package_id: str, source_path: str, existing: object, sha256: str) -> list[str]:
    quoted = shlex.quote(source_path)
    if existing is None:
        return [f"/plugins install {quoted}"]
    if getattr(existing, "package_sha256", "") == sha256:
        return []
    steps = [f"/plugins disable {package_id}"] if getattr(existing, "enabled", False) else []
    return [*steps, f"/plugins update {package_id} {quoted}"]


# LLM: 先发不带任何授权参数的启用；回执要求确认时，只有白名单内的预览且有代确认许可才发预览给出的确认命令。
# 函数用途: 启用刚装上的包，必要时代确认一次。
def _enable(runner: _Runner, consent: InstallConsent) -> InstallOutcome:
    label = version_label(runner.request.record)
    result = runner.send(f"/plugins enable {runner.package_id}")
    if result.get("ok"):
        return runner.outcome(STATE_ENABLED, f"已装上并启用：{label}。")
    confirmation = _confirmation(result)
    if confirmation is None:
        return runner.failed(result)
    if not consent.run_programs or not auto_confirmable(confirmation):
        command = _user_command(runner.package_id, confirmation, consent)
        return runner.outcome(STATE_NEEDS_USER, _needs_user_message(confirmation, label), command=command)
    confirmed = runner.send(_confirm_command(runner.package_id, confirmation), _host_request_id(confirmation))
    if confirmed.get("ok"):
        return runner.outcome(STATE_ENABLED, f"已装上并启用：{label}（已代你确认运行她自己做的程序）。")
    return runner.failed(confirmed)


# LLM: 只认结构化回执：details.reason == "confirmation_required" 且带 confirmation 对象；不解析回执文字。纯函数。
# 函数用途: 从启用回执里取出确认预览。
def _confirmation(result: dict) -> dict | None:
    details = result.get("details")
    if not isinstance(details, dict) or details.get("reason") != "confirmation_required":
        return None
    confirmation = details.get("confirmation")
    return confirmation if isinstance(confirmation, dict) and confirmation.get("confirm_code") else None


# LLM: 白名单（复审 M1）：检查程序预览可代确认；老格式权限预览只在 restricted 且没有联网、读写目录、包外程序目录、宿主接口时可代确认；
#   其余（含事件插件预览、宽权限 wide、未知类型）一律交给用户。纯函数。
# 函数用途: 判断一份启用预览能不能由宿主代用户确认。
def auto_confirmable(confirmation: dict) -> bool:
    kind = confirmation.get("kind")
    if kind == VERIFIER_CONSENT_KIND:
        return True
    if kind != _LEGACY_PERMISSION_KIND or confirmation.get("mode") != "restricted":
        return False
    permissions = confirmation.get("permissions")
    if not isinstance(permissions, dict) or permissions.get("network") or confirmation.get("host_api"):
        return False
    return not any(permissions.get(key) for key in ("read_roots", "write_roots", "program_roots"))


# LLM: 预览自带完整确认命令（老格式权限预览）就原样用；否则拼 enable --confirm（检查程序确认码与会话无关）。纯函数。
# 函数用途: 得到宿主或用户要发的那一行确认命令。
def _confirm_command(package_id: str, confirmation: dict) -> str:
    command = confirmation.get("confirm_command")
    if isinstance(command, str) and command.startswith("/plugins enable "):
        return command
    return f"/plugins enable {package_id} --confirm {confirmation['confirm_code']}"


# LLM: 老格式权限预览自带确认命令，宿主执行它时改用预览里的授权编号作请求编号（plugin_management 按 --authorization 取）；
#   落进度用同一个编号，回放时才查得到。其它预览返回空串（用本地生成的编号）。纯函数。
# 函数用途: 预先算出宿主执行这条确认命令时会用的请求编号。
def _host_request_id(confirmation: dict) -> str:
    return str(confirmation.get("authorization_id") or "") if confirmation.get("confirm_command") else ""


# LLM: 老格式权限确认码绑定发起会话；不在用户自己的会话里拿到的预览，只给一条不带授权参数的启用命令，
#   让用户在自己的会话里拿到完整预览再确认（复审 S4）。检查程序确认码与会话无关，照给。纯函数。
# 函数用途: 生成停在确认时交给用户的那一行。
def _user_command(package_id: str, confirmation: dict, consent: InstallConsent) -> str:
    if consent.user_session or confirmation.get("kind") == VERIFIER_CONSENT_KIND:
        return _confirm_command(package_id, confirmation)
    return f"/plugins enable {package_id}"


# LLM: 按预览类型说清为什么要用户自己确认；不解析预览文字。label 是宿主给的版本说法。纯函数。
# 函数用途: 停在确认时给人看的一句话。
def _needs_user_message(confirmation: dict, label: str) -> str:
    if confirmation.get("kind") == VERIFIER_CONSENT_KIND:
        return f"已装上 {label}，启用前要你确认它会运行的检查程序；核对后把下面这一行发给我。"
    return (f"已装上 {label}，但启用要你自己确认（它会运行程序，可能还要联网、读写别的目录或不受限运行）；"
            "核对预览后把下面这一行发给我。")


# LLM: 宿主回执里统一的版本说法：包名、版本、摘要前 12 位（复审：装的是哪一版由宿主说，不靠她转述）。纯函数。
# 函数用途: 生成"包名 版本（摘要前 12 位）"。
def version_label(record: BuildRecord) -> str:
    return f"{record.package_id} {record.version}（{record.sha256[:12]}）"


# LLM: 每条命令发出前回调 on_step 落进度，再现取目录版本执行；命令抛异常不外抛，记成结果未确认（复审 M3）。
#   记下的请求编号优先用回执里宿主实际使用的（如授权编号，复审 S2）。有副作用（经 manager 执行命令）。
# 类用途: 按顺序发送宿主命令并收集请求编号与结果。
class _Runner:
    # LLM: existing 是装之前的同名安装记录（可能为空），只用于记上一版摘要与是否启用。
    # 函数用途: 绑定管理服务、安装请求与装之前的状态。
    def __init__(self, manager: object, request: InstallRequest, existing: object) -> None:
        self.manager, self.request, self.package_id = manager, request, request.record.package_id
        self.previous_sha256 = str(getattr(existing, "package_sha256", "") or "")
        self.previous_enabled = bool(getattr(existing, "enabled", False))
        self.request_ids: list[str] = []
        self.unknown = False
        # 这次真的把旧版本停用了（停用命令成功）才算，用于中途停下时如实提醒。
        self.disabled_previous = False

    # LLM: 先落进度再执行（落进度失败就不发这条命令，按没开始收尾）；老格式确认命令由宿主改用预览里的授权编号，
    #   这里同样用它落盘，回放时才查得到。命令抛异常时标记 unknown 并返回结构化失败。有副作用。
    # 函数用途: 发送一条 /plugins 命令并返回回执。
    def send(self, text: str, host_request_id: str = "") -> dict:
        request_id = f"lp-{uuid.uuid4().hex}"
        try:
            if self.request.on_step is not None:
                self.request.on_step(host_request_id or request_id, text)
        except OSError:
            return {"ok": False, "state": _NOT_SENT, "error_code": "PACKAGE_INSTALL_FAILED"}
        try:
            result = self.manager.command(text, revision=self.manager.catalog().revision, request_id=request_id)
        except Exception:  # noqa: BLE001 - 结果没法确认：如实记"未确认"，不抛给模型或用户回执，也不重试
            self.unknown = True
            self.request_ids.append(request_id)
            return {"ok": False, "state": STATE_UNKNOWN, "error_code": "PACKAGE_INSTALL_UNCONFIRMED"}
        result = result if isinstance(result, dict) else {"ok": False, "state": STATE_UNKNOWN}
        self.request_ids.append(str(result.get("request_id") or request_id))
        self.disabled_previous = self.disabled_previous or (text.startswith("/plugins disable ") and bool(result.get("ok")))
        return result

    # LLM: 她的包已进安装表（安装/更新成功，或本来装着的就是同一份字节）之后、启用之前回调 on_installed；写不进去就不启用
    #   （插件数据目录只在启用及之后才建），返回内部失败回执。有副作用（经回调写包名归属）。
    # 函数用途: 记下"这个包名现在装的是她的包"，失败时返回失败回执。
    def mark_installed(self) -> dict | None:
        if self.request.on_installed is None:
            return None
        try:
            self.request.on_installed()
        except OSError:
            return {"ok": False, "state": _NOT_RECORDED, "error_code": "PACKAGE_INSTALL_FAILED"}
        return None

    # LLM: 明确失败、结果未确认、宿主存储写不进去（进度或包名归属）分开报；旧版本已被停用时在说明里讲清楚（复审 S3）。
    # 函数用途: 把一步失败汇总成 InstallOutcome。
    def failed(self, result: dict) -> InstallOutcome:
        state = STATE_UNKNOWN if self.unknown else STATE_FAILED
        code = str(result.get("error_code") or result.get("state") or "unknown")
        last = self.request_ids[-1] if self.request_ids else ""
        if result.get("state") in _INTERNAL_FAILURES:
            message = _INTERNAL_FAILURES[result["state"]]
        elif state == STATE_FAILED:
            message = f"宿主的插件管理命令没有成功（{code}，请求编号 {last}），后面的步骤都没有执行。"
        else:
            message = f"宿主的插件管理命令结果没法确认（请求编号 {last}），可能已部分执行。"
        if self.request.consent.user_session and last:
            message += f"可以发 /plugins status {last} 查。"
        return self.outcome(state, message, code=code)

    # LLM: 统一出口；停在中途且旧版本原来启用时补一句"旧版已停用"。纯组装。
    # 函数用途: 汇总成 InstallOutcome。
    def outcome(self, state: str, message: str, *, command: str = "", code: str = "") -> InstallOutcome:
        if state != STATE_ENABLED and self.disabled_previous:
            message += "注意：原来启用的旧版本已经停用。"
        return InstallOutcome(state, self.package_id, message, command, code, tuple(self.request_ids),
                              self.previous_sha256, self.previous_enabled)


__all__ = [
    "STATE_ENABLED",
    "STATE_FAILED",
    "STATE_NEEDS_USER",
    "STATE_UNKNOWN",
    "InstallConsent",
    "InstallOutcome",
    "InstallRequest",
    "auto_confirmable",
    "run_install",
    "version_label",
]
