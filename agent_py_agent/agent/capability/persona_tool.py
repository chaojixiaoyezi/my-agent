# LLM: update_persona 工具——把用户的【长期人设/画像/工作约定】写进对应人格文件(SOUL/USER/AGENTS.md),
#   这三件每轮整文件注入系统上下文、真正塑造每次交互。区别于 remember(记"需要时才想起"的具体事实/事件到
#   长期记忆,按相关性召回)。用户表达"以后叫我X/我是做Y的/你说话别太正式"这类长期设定时用本工具,不用 remember。
#   写入前 scan_memory_content 注入扫描(人格文件每轮注入=注入长效面)。改动时同步 tests/test_persona_tool.py。
from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from ..user_space.owner_quota import OwnerQuotaExceeded, OwnerQuotaUnavailable
from .memory_threat_scan import scan_memory_content
from .persona_repository import (
    PersonaConflictError,
    PersonaEntryNotFoundError,
    PersonaMutationRequest,
    PersonaRepository,
    PersonaRepositoryError,
    PersonaSecurityError,
)

if TYPE_CHECKING:
    from ..core import SimpleAgent

_TARGET_ATTR = {"soul": "owner_soul_md", "user": "owner_user_md", "agents": "owner_agents_md"}


@dataclass(frozen=True)
class _PersonaToolRequest:
    """Validated structured parameters for one update_persona action."""

    action: str
    target: str
    content: str
    entry_id: str
    source_quote: str
    expected_sha256: str
    rollback_version: int | None
    confirmed: bool


def build_update_persona_spec() -> ToolSpec:
    return ToolSpec(
        name="update_persona",
        category="capability",
        effect="mutating",
        requires_idempotency=True,
        description=(
            "把用户的长期设定写进人格文件(每轮整文件注入)。区别于 remember(只记'需要时才想起'的具体事实/事件)。"
            "**target=user(用户画像/称呼/长期偏好)可直接写**;"
            "**target=soul(你自己的性格语气)/ agents(长期工作约定)是长期人设,不能随意自动改(会越堆越乱)——"
            "只能走本工具的用户确认链：飞书会发确认卡片且点击前绝不写；其他通道须先取得用户明确同意，"
            "再带 confirmed=true 调用。禁止改用 write/edit/patch/shell 绕过。**"
        ),
        use_cases=[
            "用户说怎么称呼他 / 自我介绍身份角色 / 表达长期偏好 → target=user(直接写)",
            "飞书用户明确要求长期调整性格/语气/风格 → target=soul(直接调用后由确认卡片裁决)",
            "飞书用户明确要求长期工作约定/产物习惯 → target=agents(直接调用后由确认卡片裁决)",
            "其他通道调整 soul/agents → 先问用户，同意后 confirmed=true",
        ],
        avoid_when=[
            "用基础文件或 shell 工具改 soul/agents → 必须改用 update_persona 的确认链",
            "一次性临时语气(如'这次说话活泼点')→ 当场照做即可,别写进 soul",
            "只是'需要时才想起'的具体事实/事件(如'下周三交报告''项目叫X')→ 用 remember 记 memory",
        ],
        keywords=[
            "以后叫我",
            "喊我",
            "称呼",
            "我是做",
            "人设",
            "画像",
            "性格",
            "语气",
            "长期偏好",
            "以后都",
            "写进设定",
        ],
        parameters={
            "action": "可选。add(默认)/list/replace/remove/history/rollback/status。replace/remove 必须先 list 取得 entry_id。",
            "target": "必填。user(用户画像/称呼,可直接写)/ soul(你的性格语气)/ agents(长期工作约定)。",
            "content": "add/replace 必填。要写进的一句话纯描述；一次只写一个事实，不得换行。list/remove 不需要。",
            "entry_id": "replace/remove 必填。只能使用 list 返回的精确 entry_id，不能按自然语言猜删除目标。",
            "source_quote": (
                "target=user 的 add/replace/remove 必填。逐字引用当前这条用户消息中能证明本次变更的最短原文；"
                "必须是当前用户消息的原样子串，且新增值或被删除值必须出现在引用中。"
            ),
            "confirmed": "非飞书改 soul/agents 时，在用户已明确同意后填 true；飞书会忽略此值并始终等待卡片点击；改 user 不需要。",
            "expected_sha256": "可选。list 返回的文件哈希；并发修改后不匹配则拒绝覆盖。",
            "rollback_version": "rollback 必填。history 返回的精确版本号。",
        },
        parameter_schema={
            "action": {
                "type": "string",
                "enum": ["add", "list", "replace", "remove", "history", "rollback", "status"],
            },
            "target": {"type": "string", "enum": ["soul", "user", "agents"]},
            "content": {"type": "string"},
            "entry_id": {"type": "string"},
            "source_quote": {"type": "string"},
            "confirmed": {"type": "boolean"},
            "expected_sha256": {"type": "string"},
            "rollback_version": {"type": "integer", "minimum": 1},
        },
        required_parameters=["target"],
        examples=[
            '{"tool":"update_persona","target":"user","content":"称呼:松果","source_quote":"以后请叫我松果"}',
            '{"tool":"update_persona","action":"list","target":"user"}',
            '{"tool":"update_persona","action":"remove","target":"user","entry_id":"persona-...","source_quote":"我不叫松果，请删掉这个称呼"}',
            '{"tool":"update_persona","target":"soul","content":"语气偏活泼","confirmed":true}',
        ],
    )


class UpdatePersonaTool(BaseTool):
    # 类用途: 把"更新用户人设/画像/工作约定"暴露成模型工具,落到 owner 的 SOUL/USER/AGENTS.md(每轮注入)。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_update_persona_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        request = _parse_persona_tool_request(params)
        if isinstance(request, ToolExecutionResult):
            return request
        try:
            repository = self._repository()
            repository.path_for(request.target)
        except PersonaRepositoryError as exc:
            return _err(str(exc), "TOOL_UNAVAILABLE")
        read_result = _persona_read_result(repository, request)
        if read_result is not None:
            return read_result
        if request.target == "user":
            grounding_error = _user_persona_grounding_error(
                self.agent,
                repository,
                action=request.action,
                content=request.content,
                entry_id=request.entry_id,
                source_quote=request.source_quote,
            )
            if grounding_error is not None:
                return grounding_error
        # SOUL/AGENTS 是长期人设/工作约定(每轮注入、管所有行为),不能随意自动改(会越堆越乱)。
        if request.target in ("soul", "agents"):
            # 飞书通道:不再靠 confirmed 拒写,而是发一张交互卡片让用户按钮确认(非阻塞,工具立即返回)。
            if self._owner_provider() == "feishu":
                return self._feishu_confirm_flow(
                    request.target,
                    request.action,
                    request.content,
                    request.entry_id,
                    expected_sha256=request.expected_sha256
                    or repository.current_sha256(request.target),
                    rollback_version=request.rollback_version,
                )
            # 非飞书:保持现有 confirmed 闸行为不变(必须先问用户、拿到同意带 confirmed=true 才写)。
            if not request.confirmed:
                return _err(
                    f"{request.target.upper()}.md 是长期人设/工作约定,不能自动改(会越改越乱)。"
                    "请先在回复里明确问用户'要不要把这条写进长期设定',用户明确同意后,再带 confirmed=true 调用。",
                    "APPROVAL_REQUIRED",
                )
        if request.content:
            scan = scan_memory_content(request.content)  # 人格文件每轮注入,写入前过注入/外泄扫描
            if not scan.safe:
                return _err(
                    scan.reason(),
                    "PERSONA_INJECTION_BLOCKED",
                    hint="人格文件每轮注入,改成纯描述再写",
                )
        return _execute_persona_mutation(self.agent, repository, request)

    def _repository(self) -> PersonaRepository:
        repository = getattr(self.agent, "persona_repository", None)
        if isinstance(repository, PersonaRepository):
            return repository
        return PersonaRepository.from_home_paths(getattr(self.agent, "home_paths", None))

    def _owner_provider(self) -> str:
        """当前 agent 的 owner 通道(飞书=feishu),取自 home_paths(scoped owner 身份)。"""
        return (
            str(getattr(getattr(self.agent, "home_paths", None), "owner_provider", "") or "")
            .strip()
            .lower()
        )

    def _feishu_confirm_flow(
        self,
        target: str,
        action: str,
        content: str,
        entry_id: str,
        *,
        expected_sha256: str,
        rollback_version: int | None,
    ) -> ToolExecutionResult:
        """飞书改 SOUL/AGENTS:存一条待确认记录 + 发交互卡片,用户点『确认写入』才落写(回调侧完成)。
        非阻塞——本工具立即返回。凭据缺失/卡片发不出 → 清掉悬挂记录、回落"就地不写 + 提示用户"。"""
        scan = (
            scan_memory_content(content) if content else None
        )  # 卡片确认后照写,内容先过注入/外泄扫描
        if scan is not None and not scan.safe:
            return _err(
                scan.reason(), "PERSONA_INJECTION_BLOCKED", hint="人格文件每轮注入,改成纯描述再写"
            )
        home = getattr(self.agent, "home_paths", None)
        root = getattr(home, "root", None)
        open_id = str(getattr(home, "owner_id", "") or "")
        if not root or not open_id:
            return _err("无法定位待确认存储或飞书身份", "TOOL_UNAVAILABLE")
        from ..adapter.feishu_card import build_persona_confirm_card, send_interactive_card
        from . import persona_pending

        owner = ("feishu", str(getattr(home, "owner_kind", "user") or "user"), open_id)
        token = persona_pending.add(
            root,
            owner,
            target,
            content,
            action=action,
            entry_id=entry_id,
            expected_sha256=expected_sha256,
            rollback_version=rollback_version,
        )
        app_id, app_secret = self._feishu_creds()
        sent = send_interactive_card(
            app_id,
            app_secret,
            open_id,
            build_persona_confirm_card(token, target, content, action=action),
        )
        if not sent:
            persona_pending.pop(root, token)  # 发不出去 → 清掉悬挂记录(用户永远收不到按钮)
            payload = {
                "ok": True,
                "target": target,
                "pending": False,
                "note": "现在没法给你发确认卡片(飞书没连上或缺凭据),这条长期设定我先没写。稍后可以再让我改。",
            }
            return ToolExecutionResult(
                "update_persona", True, json.dumps(payload, ensure_ascii=False)
            )
        label = "SOUL(长期人设)" if target == "soul" else "AGENTS(工作约定)"
        payload = {
            "ok": True,
            "target": target,
            "pending": True,
            "note": f"已给你发了确认卡片，确认后才会对{label}执行 {action}；取消则不改。",
        }
        return ToolExecutionResult("update_persona", True, json.dumps(payload, ensure_ascii=False))

    def _feishu_creds(self) -> tuple[str, str]:
        """从 config 取飞书凭据并过 SecretRef 解析(值可为 env:/file: 间接引用)。取不到 → 空串,
        由 send_interactive_card fail-open 处理(回落不写)。"""
        from ..settings.secret_ref import resolve_secret_ref

        cfg = getattr(self.agent, "config", None)
        app_id = resolve_secret_ref(str(getattr(cfg, "feishu_app_id", "") or ""))
        app_secret = resolve_secret_ref(str(getattr(cfg, "feishu_app_secret", "") or ""))
        return app_id, app_secret


# LLM: Parse and validate the action schema before repository or consent side effects.
# 函数用途: 把 update_persona 参数整理成一个结构化请求，统一返回可读的参数错误。
def _parse_persona_tool_request(
    params: dict[str, object],
) -> _PersonaToolRequest | ToolExecutionResult:
    action = str(params.get("action") or "add").strip().lower()
    target = str(params.get("target") or "").strip().lower()
    content = str(params.get("content") or "").strip()
    entry_id = str(params.get("entry_id") or "").strip()
    source_quote = str(params.get("source_quote") or "").strip()
    expected_sha256 = str(params.get("expected_sha256") or "").strip()
    allowed = {"add", "list", "replace", "remove", "history", "rollback", "status"}
    if target not in _TARGET_ATTR or action not in allowed:
        return _err(
            "target 须为 soul/user/agents，action 须为 add/list/replace/remove/history/rollback/status",
            "TOOL_INVALID_ARGUMENTS",
        )
    if action in {"add", "replace"} and not content:
        return _err("add/replace 的 content 必填", "TOOL_INVALID_ARGUMENTS")
    if action in {"replace", "remove"} and not entry_id:
        return _err("replace/remove 必须提供 list 返回的 entry_id", "TOOL_INVALID_ARGUMENTS")
    rollback_version = _parse_rollback_version(action, params.get("rollback_version"))
    if isinstance(rollback_version, ToolExecutionResult):
        return rollback_version
    return _PersonaToolRequest(
        action=action,
        target=target,
        content=content,
        entry_id=entry_id,
        source_quote=source_quote,
        expected_sha256=expected_sha256,
        rollback_version=rollback_version,
        confirmed=bool(params.get("confirmed")),
    )


def _parse_rollback_version(
    action: str,
    value: object,
) -> int | None | ToolExecutionResult:
    if action == "rollback" and value in (None, ""):
        return _err("rollback 必须提供 history 返回的 rollback_version", "TOOL_INVALID_ARGUMENTS")
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _err("rollback_version 必须是正整数", "TOOL_INVALID_ARGUMENTS")
    if parsed < 1:
        return _err("rollback_version 必须是正整数", "TOOL_INVALID_ARGUMENTS")
    return parsed


# LLM: Read-only Persona actions bypass consent and mutation while sharing repository diagnostics.
# 函数用途: 统一 list/history/status 三种只读返回，避免主执行函数重复异常处理。
def _persona_read_result(
    repository: PersonaRepository,
    request: _PersonaToolRequest,
) -> ToolExecutionResult | None:
    try:
        if request.action == "list":
            payload = repository.list_entries(request.target)
            payload.update({"ok": True, "action": "list"})
        elif request.action == "history":
            payload = {
                "ok": True,
                "action": "history",
                "target": request.target,
                "versions": repository.history(request.target),
            }
        elif request.action == "status":
            payload = {
                "ok": True,
                "action": "status",
                "target": request.target,
                "diagnostic": repository.load(request.target).diagnostic.to_dict(),
            }
        else:
            return None
    except (OSError, PersonaRepositoryError) as exc:
        return _err(f"读取人格状态失败: {exc}", "TOOL_EXECUTION_FAILED")
    return ToolExecutionResult("update_persona", True, json.dumps(payload, ensure_ascii=False))


# LLM: All mutation failures map to the registered tool error taxonomy in one place.
# 函数用途: 调用 Persona repository 并把并发、注入、配额和文件错误转换成稳定工具结果。
def _execute_persona_mutation(
    agent: object,
    repository: PersonaRepository,
    request: _PersonaToolRequest,
) -> ToolExecutionResult:
    try:
        payload = repository.mutate(
            PersonaMutationRequest(
                target=request.target,
                action=request.action,
                content=request.content,
                entry_id=request.entry_id,
                source_quote=request.source_quote,
                confirmed=request.confirmed or request.target == "user",
                expected_sha256=request.expected_sha256,
                rollback_version=request.rollback_version,
                source=_persona_source(agent),
            )
        )
    except PersonaEntryNotFoundError:
        return _err(
            "entry_id 或版本不存在；请重新 list/history 后再操作", "PERSONA_ENTRY_NOT_FOUND"
        )
    except PersonaConflictError as exc:
        return _err(
            str(exc), "PERSONA_VERSION_CONFLICT", hint="人格文件已被其他会话修改，请重新 list。"
        )
    except PersonaSecurityError as exc:
        return _err(str(exc), "PERSONA_INJECTION_BLOCKED")
    except OwnerQuotaExceeded as exc:
        return _err(
            str(exc), "OWNER_DISK_QUOTA_EXCEEDED", hint="清理当前 owner 文件或联系管理员调整配额。"
        )
    except OwnerQuotaUnavailable:
        return _err(
            "owner 配额策略当前不可用",
            "OWNER_QUOTA_UNAVAILABLE",
            hint="配额策略恢复前拒绝写入。",
        )
    except (OSError, PersonaRepositoryError, ValueError) as exc:
        return _err(f"写入失败: {exc}", "TOOL_EXECUTION_FAILED")
    payload["hint"] = "人格文件已按结构化 entry_id 更新，下一轮起长期生效。"
    return ToolExecutionResult("update_persona", True, json.dumps(payload, ensure_ascii=False))


def _normalized_grounding_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _persona_fact_value(content: str) -> str:
    for separator in (":", "："):
        if separator in content:
            return content.split(separator, 1)[1].strip()
    return content.strip()


def _entry_content_by_id(repository: PersonaRepository, target: str, entry_id: str) -> str:
    for entry in repository.list_entries(target)["entries"]:
        if entry["entry_id"] == entry_id:
            return entry["content"]
    return ""


def _user_persona_grounding_error(
    agent: object,
    repository: PersonaRepository,
    *,
    action: str,
    content: str,
    entry_id: str,
    source_quote: str,
) -> ToolExecutionResult | None:
    """USER 画像变更必须逐项锚定到当前用户原文，模型自己补出的值不能落盘。"""
    if action in {"add", "replace"} and len(content.splitlines()) != 1:
        return _err(
            "USER.md 一次只能变更一个单行事实；本次没有写入。",
            "PERSONA_CONTENT_NOT_ATOMIC",
            hint="拆成多个 update_persona 调用，每次各带对应的 source_quote。",
        )
    current_prompt = str(getattr(agent, "_current_user_prompt", "") or "")
    if not source_quote:
        return _err(
            "USER.md 变更缺少当前用户原文依据；本次没有写入。",
            "PERSONA_SOURCE_REQUIRED",
            hint="source_quote 必须逐字引用当前这条用户消息，不能由模型改写或补充。",
        )
    if source_quote not in current_prompt:
        return _err(
            "source_quote 不是当前用户消息的原样子串；本次没有写入。",
            "PERSONA_SOURCE_MISMATCH",
            hint="只能引用当前这条用户消息中真实存在的原文。",
        )
    if action == "rollback":
        return None
    grounded_content = content
    if action == "remove":
        try:
            grounded_content = _entry_content_by_id(repository, "user", entry_id)
        except (OSError, PersonaRepositoryError):
            return _err("读取 USER.md 失败；本次没有写入。", "TOOL_EXECUTION_FAILED")
        if not grounded_content:
            return _err(
                "entry_id 不存在或已变化；本次没有写入，请重新 list。",
                "PERSONA_ENTRY_NOT_FOUND",
            )
    value = _normalized_grounding_text(_persona_fact_value(grounded_content))
    evidence = _normalized_grounding_text(source_quote)
    if not value or value not in evidence:
        return _err(
            "本次要新增、替换或删除的画像值没有出现在 source_quote 中；本次没有写入。",
            "PERSONA_CONTENT_UNGROUNDED",
            hint="不要推测用户没说过的称呼、身份或偏好；一次只处理原文明确支持的一个事实。",
        )
    return None


def _persona_source(agent: object) -> str:
    current = getattr(agent, "_current_run_params", None)
    request_id = str(getattr(current, "request_id", "") or "").strip()
    return f"agent_tool:{request_id}" if request_id else "agent_tool"


def _err(msg: str, code: str, hint: str = "") -> ToolExecutionResult:
    body = {"error": msg}
    if hint:
        body["hint"] = hint
    return ToolExecutionResult(
        "update_persona", False, json.dumps(body, ensure_ascii=False), error_code=code
    )


__all__ = [
    "UpdatePersonaTool",
    "build_update_persona_spec",
]
