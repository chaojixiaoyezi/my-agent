# LLM: 插件只能转交宿主真人票据；秘密始终留在原私卡服务，查询不续租。
# 模块用途: 读取待审批/待输入并转交精确人工决定。
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field

from .contracts import DomainResult
from .errors import HostAPIError
from .schemas import canonical_json, freeze, thaw
from .tasks import checked, context_for, finish, require_scope, require_thread, resource_id, utc

# 票据最多存活六十秒，原卡到期更早时采用原卡期限。
TICKET_TTL_SECONDS = 60
# 单宿主实例的短期票据上限，不为插件构造无限内存账本。
MAX_TICKETS_COUNT = 1024
_KINDS = {"tool_approval": "tool_approval", "task_credential": "credential_request",
          "model_configuration": "model_selection", "feishu_oauth": "oauth_authorization"}
_SUMMARIES = {"tool_approval": "待确认工具操作", "credential_request": "请在宿主安全界面填写凭据",
              "model_selection": "请在宿主界面选择模型", "oauth_authorization": "请在宿主界面完成授权"}


# LLM: 决定和原卡只留在宿主内存，不放入token或公开DTO，绑定完整安装激活与owner。
# 类用途: 保存一条已由真人认证的不可变决定。
@dataclass(frozen=True)
class HumanResponse:
    binding: tuple
    interaction_id: str
    thread_id: str
    revision: int
    action: str
    expires_at: float
    interaction: object = field(repr=False)
    decision_payload: object = field(repr=False)
    human_identity: object = field(default=None, repr=False)


# LLM: 公开kind按D1任务统一命名；原卡kind仍原样保存在宿主记录中。
# 函数用途: 校验并映射既有私卡类别。
def public_kind(kind: str) -> str:
    mapped = _KINDS.get(kind, kind)
    if mapped not in _SUMMARIES:
        raise HostAPIError("UNSUPPORTED_INTERACTION", 400)
    return mapped


# LLM: 票据不可跨换代、包版本或owner复用；该元组不是授权证明，所有使用点还需issuer核验。
# 函数用途: 冻结票据的安装与身份绑定。
def ticket_binding(authorized) -> tuple:
    binding = authorized.binding
    return (binding.installation_ref, binding.activation_id, binding.package_sha256,
            binding.grant_revision, authorized.effective_owner)


# LLM: 列表summary使用固定安全文案，绝不复制工具摘要、秘密字段/现值、credential_ref或签名卡引用。
# 函数用途: 生成闭合且不含秘密的PendingInteraction。
def project_interaction(row: dict) -> dict:
    kind = public_kind(row["kind"])
    return {"interaction_id": resource_id(row["interaction_id"]), "request_id": resource_id(row["request_id"]),
            "thread_id": resource_id(row["thread_id"]), "run_id": row["run_id"], "kind": kind,
            "revision": row["revision"], "summary": _SUMMARIES[kind], "expires_at": utc(row["expires_at"]),
            "respond_via": "host_ui", "presentation_ref": resource_id(row["presentation_ref"])}


# LLM: 仅可信UI持有签发端口，插件动作目录不暴露该方法。
# 类用途: 保存短期人工响应票据。
class ResponseTickets:
    # LLM: verify_human(proof,a,interaction,decision)必须验证真实适配器来源及原卡，不接受插件token冒充真人。
    # 票据随宿主寿命失效；提交重放由原持久回执负责。不能把这个短期表当作第二份业务执行账。
    # 函数用途: 绑定宿主验证器和时钟。
    def __init__(self, services, verify_human, *, clock=time.time):
        self.services, self.verify_human, self.clock = services, verify_human, clock
        self._lock, self._tickets = threading.RLock(), {}

    # LLM: 先核真实human及原卡、当前grant和资源范围，再产生不可预测opaque token；不暴露公开签发动作。
    # 函数用途: 为真实人工操作签发票据。
    def issue(self, authorized, interaction, proof, decision_payload):
        self.services.revalidate(authorized)
        if "interactions.respond" not in authorized.binding.capabilities:
            raise HostAPIError("CAPABILITY_DENIED", 403)
        identity = self.verify_human(proof, authorized, interaction, decision_payload)
        context = context_for(self.services, authorized)
        require_thread(context, authorized, interaction["thread_id"])
        if interaction["run_id"]:
            require_scope(context, authorized, "task", interaction["run_id"])
        kind = public_kind(interaction["kind"])
        action = "interactions.approvals.respond" if kind == "tool_approval" else "interactions.inputs.respond"
        now = self.clock()
        expiry = min(now + TICKET_TTL_SECONDS, float(interaction["expires_at"]))
        if expiry <= now:
            raise HostAPIError("INTERACTION_EXPIRED", 410)
        record = HumanResponse(ticket_binding(authorized), resource_id(interaction["interaction_id"]),
            interaction["thread_id"], interaction["revision"], action, expiry,
            freeze(interaction), freeze(decision_payload), freeze(identity))
        with self._lock:
            self._tickets = {key: entry for key, entry in self._tickets.items() if entry["record"].expires_at > now}
            if len(self._tickets) >= MAX_TICKETS_COUNT:
                raise HostAPIError("RATE_LIMITED", 429)
            token = secrets.token_urlsafe(32)
            self._tickets[token] = {"record": record, "state": "pending", "key": None, "result": None}
            return token

    # LLM: 外层原receipt锁→短票据锁→原卡首决锁；backend必须在原CAS内核版本、scope、claim/attempt及到期。
    # 未知写入后绝不重新调用backend；重启丢失票据失败关闭，原已提交receipt仍可重放。
    # 函数用途: 消费唯一人工决定并保存本宿主寿命内的提交回执。
    def consume(self, authorized, respond):
        with self._lock:
            self.services.revalidate(authorized)
            entry = self._tickets.get(authorized.call.payload["response_ticket"])
            if entry is None:
                raise HostAPIError("HUMAN_TICKET_INVALID", 403)
            record = entry["record"]
            self._check_binding(authorized, record)
            if entry["state"] != "pending":
                if entry["key"] != authorized.call.idempotency_key:
                    raise HostAPIError("IDEMPOTENCY_CONFLICT", 409)
                if entry["result"] is None:
                    raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")
                return {**entry["result"], "replayed": True}
            if self.clock() >= record.expires_at:
                raise HostAPIError("HUMAN_TICKET_EXPIRED", 410)
            context = context_for(self.services, authorized)
            require_thread(context, authorized, record.thread_id)
            if record.interaction["run_id"]:
                require_scope(context, authorized, "task", record.interaction["run_id"])
            entry.update(state="consuming", key=authorized.call.idempotency_key)
            try:
                status = respond(authorized, record)
            except HostAPIError:
                raise
            except Exception:
                raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown") from None
            if status not in {"decided", "submitted", "cancelled"}:
                raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")
            result = {"interaction_id": record.interaction_id, "status": status, "replayed": False}
            entry.update(state="committed", result=result)
            return result

    # LLM: 不先读取原卡，避免错误installation/owner的票据变成资源枚举器。
    # 函数用途: 在消费前验证完整绑定和精确内容版本。
    def _check_binding(self, authorized, record):
        payload = authorized.call.payload
        if (record.binding != ticket_binding(authorized) or record.action != authorized.call.action
                or record.interaction_id != payload["interaction_id"]):
            raise HostAPIError("HUMAN_TICKET_INVALID", 403)
        if record.revision != payload["expected_revision"]:
            raise HostAPIError("REVISION_CONFLICT", 409)


# LLM: backend只访问已存在的原卡；不在插件层创建普通自由文本输入。
# 类用途: 适配四个交互动作。
class InteractionAdapter:
    # LLM: backend.pending(a,thread)只读原四类卡，backend.respond(a,HumanResponse)转交原首决CAS。
    # backend不能调用HTTP handler、consumer续租wrapper或允许插件覆盖票据中的决定。
    # 函数用途: 绑定原卡读取和人工票据服务。
    def __init__(self, services, backend, tickets):
        self.services, self.backend, self.tickets = services, backend, tickets

    # LLM: 重放也先核身份/capability，原receipt只回已提交结果；秘密决不通过公开请求。
    # 函数用途: 列出安全元数据或转交一次真人决定。
    def call(self, authorized):
        payload = checked(self.services, authorized, "interactions.")
        if authorized.call.action.endswith(".respond"):
            return self._respond(authorized)
        context = context_for(self.services, authorized)
        require_thread(context, authorized, payload["thread_id"])
        approvals = authorized.call.action == "interactions.pending_approvals.list"
        rows = []
        for row in self.backend.pending(authorized, payload["thread_id"]):
            if row["thread_id"] != payload["thread_id"]:
                continue
            if row["run_id"] and not context.allows(authorized, "task", row["run_id"]):
                continue
            if row["expires_at"] <= self.tickets.clock():
                continue
            if (public_kind(row["kind"]) == "tool_approval") == approvals:
                rows.append(project_interaction(row))
        return finish(self.services, authorized, 200, self.services.page(authorized, rows))

    # LLM: receipt重放与首次执行分别标记，结果状态来自原服务而不是是否调用过工具。
    # 函数用途: 在宿主幂等边界内消费人工票据。
    def _respond(self, authorized):
        executed = False

        # LLM: 原receipt只允许首次提交进入此回调，异常不自动重放副作用。
        # 函数用途: 交给票据锁及原卡CAS提交一次决定。
        def effect():
            nonlocal executed
            executed = True
            return DomainResult(200, self.tickets.consume(authorized, self.backend.respond))

        result = self.services.once(authorized, effect)
        return finish(self.services, authorized, 200,
                      {**result.data, "replayed": not executed or result.data["replayed"]}, outcome="committed")


# LLM: 原审批内容和版本只用于宿主签票与原CAS，公开列表必须仍经project_interaction裁剪。
# 函数用途: 从原主/子审批记录构造内部交互行。
def approval_interaction(agent, pending, presentation_ref):
    from ..conversation.agent_tool_approval import read_pending_approval_record

    record = read_pending_approval_record(agent, run_id=pending["run_id"], request_value=pending["request"])
    identity = [record["thread_id"], record["run_id"], record["request"]["permission_id"]]
    identifier = "approval-" + hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:40]
    return {"interaction_id": identifier, "request_id": record["request"]["request_id"],
            "thread_id": record["thread_id"], "run_id": record["run_id"], "kind": "tool_approval",
            "revision": approval_revision(record), "expires_at": record["expires_at"],
            "presentation_ref": presentation_ref, "request": record["request"]}


# LLM: 版本绑定完整原首决记录，包括request/claim/attempt/status，不另建版本状态。
# 函数用途: 计算原审批行的CAS指纹。
def approval_revision(record):
    digest = hashlib.sha256(canonical_json(record).encode()).digest()
    return (int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)) or 1


# LLM: 仅供原宿主backend消费HumanResponse，不接受插件JSON直接决定。
# 函数用途: 把已签人工票据交回原审批首决锁。
def respond_approval(services, agent, authorized, ticket):
    from ..conversation.agent_tool_approval import resolve_agent_tool_approval

    # LLM: 此回调在原permission锁内执行，不重取同一锁；票据过期、撤权及原记录变化都先于写入。
    # 函数用途: 末核当前宿主授权、票据绑定和原首决内容版本。
    def guard(record):
        checked(services, authorized, "interactions.")
        context = context_for(services, authorized)
        if agent.conversation_store is not context.store or ticket.binding != ticket_binding(authorized):
            raise HostAPIError("TARGET_DENIED", 403)
        require_thread(context, authorized, ticket.thread_id)
        require_scope(context, authorized, "task", ticket.interaction["run_id"])
        if time.time() >= ticket.expires_at:
            raise HostAPIError("HUMAN_TICKET_EXPIRED", 410)
        if approval_revision(record) != ticket.revision:
            raise HostAPIError("REVISION_CONFLICT", 409)

    try:
        result = resolve_agent_tool_approval(agent, run_id=ticket.interaction["run_id"],
            request_value=thaw(ticket.interaction["request"]), decision_value=thaw(ticket.decision_payload),
            record_guard=guard)
    except HostAPIError:
        raise
    except FileNotFoundError:
        raise HostAPIError("INTERACTION_EXPIRED", 410) from None
    except ValueError:
        raise HostAPIError("INTERACTION_CONFLICT", 409) from None
    except Exception:
        raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown") from None
    if result.get("ok") is not True:
        raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")
    return "decided"


# LLM: 所有构造依赖仅由宿主提供，插件JSON不能选择context、payload来源或真人认证器。
# 类用途: 将三类真实输入卡接入原读服务和原提交事务。
class NativeInputBackend:
    # LLM: pending_payloads只提供既有原卡载荷；authenticate_human须核真实SDK/UI来源，不能以插件token充当真人。
    # 函数用途: 绑定原owner资源及可信输入适配器。
    def __init__(self, services, context, pending_payloads, authenticate_human):
        self.services, self.context = services, context
        self.pending_payloads, self.authenticate_human = pending_payloads, authenticate_human

    # LLM: 此入口只读原卡；不能通过消费wrapper延长租期。
    # 函数用途: 返回原输入卡的内部安全投影。
    def pending(self, authorized, thread_id):
        from ..gateway_parts.private_interaction_service import read_plugin_pending_input

        context = context_for(self.services, authorized)
        require_thread(context, authorized, thread_id)
        if context.store is not self.context.inputs.store or context.owner != self.context.inputs.owner:
            raise HostAPIError("TARGET_DENIED", 403)
        rows = []
        for payload, presentation in self.pending_payloads(authorized, thread_id):
            row = read_plugin_pending_input(self.context.home_paths.owner_home_dir, self.context.inputs, payload,
                                            presentation_ref=presentation)
            if row is not None and row["thread_id"] == thread_id:
                rows.append({**row, "payload": payload})
        return rows

    # LLM: 仅宿主真人适配器可签票，原MAC、actor、私聊锁及卡版本必须同时满足。
    # 函数用途: 校验一次人工证明与准确原卡。
    def verify_human(self, proof, authorized, interaction, decision_payload):
        from ..gateway_parts.private_interaction_service import (
            read_plugin_pending_input,
            validate_plugin_input,
        )

        identity = self.authenticate_human(proof)
        payload = validate_plugin_input(self.context, thaw(decision_payload), identity)
        if payload != thaw(interaction["payload"]):
            raise HostAPIError("INTERACTION_CONFLICT", 409)
        row = read_plugin_pending_input(self.context.home_paths.owner_home_dir, self.context.inputs, payload,
                                        presentation_ref=interaction["presentation_ref"], body=thaw(decision_payload))
        if row is None:
            raise HostAPIError("INTERACTION_EXPIRED", 410)
        if row["revision"] != interaction["revision"] or row["interaction_id"] != interaction["interaction_id"]:
            raise HostAPIError("REVISION_CONFLICT", 409)
        return identity

    # LLM: 提交仍归原服务，票据不是第二个业务状态机。
    # 函数用途: 在原卡锁内末核版本后提交原决定。
    def respond(self, authorized, ticket):
        from ..gateway_parts.private_interaction_service import execute_plugin_input

        entered = False

        # LLM: 运行在原quota→原卡锁内；不重入卡锁，不信任锁外同一份旧payload，先核当前权限再核revision。
        # 函数用途: 在原提交点核对owner、执行范围、票据期限及原卡完整版本。
        def guard(row):
            nonlocal entered
            checked(self.services, authorized, "interactions.")
            context = context_for(self.services, authorized)
            if (ticket.binding != ticket_binding(authorized) or context.store is not self.context.inputs.store
                    or context.owner != self.context.inputs.owner):
                raise HostAPIError("TARGET_DENIED", 403)
            require_thread(context, authorized, ticket.thread_id)
            if ticket.interaction["run_id"]:
                require_scope(context, authorized, "task", ticket.interaction["run_id"])
            if time.time() >= ticket.expires_at:
                raise HostAPIError("HUMAN_TICKET_EXPIRED", 410)
            if approval_revision(row) != ticket.revision:
                raise HostAPIError("REVISION_CONFLICT", 409)
            entered = True

        try:
            result = execute_plugin_input(self.context, thaw(ticket.interaction["payload"]),
                thaw(ticket.decision_payload), thaw(ticket.human_identity), guard)
        except HostAPIError:
            raise
        except (ValueError, TypeError, KeyError):
            raise HostAPIError("OUTCOME_UNKNOWN" if entered else "INTERACTION_CONFLICT", 503 if entered else 409,
                               "unknown" if entered else "not_started") from None
        except Exception:
            raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown") from None
        if result.get("ok") is not True or not entered:
            raise HostAPIError("OUTCOME_UNKNOWN", 503, "unknown")
        return "cancelled" if ticket.decision_payload["decision"] == "cancel" else "submitted"
