# LLM: C的纯授权合同，尚未接入任何写入入口；JSON不是授权证据。
# 模块用途: 冻结用户本人明确写入请求与宿主核对上下文。
import json
from dataclasses import asdict, dataclass
from hashlib import sha256

from ._agents_contract_fields import (
    AgentsContractError,
    decode_fields,
    require_counter,
    require_digest,
    require_text,
    validate_scalars,
)
from .agents_source_contract import _validate_locator


# LLM: 这是授权投影而非凭据；C须按authorization_id独立取宿主原记录。
# 类用途: 保存用户请求、目标CAS、时效和显式跨owner目标，不可变。
@dataclass(frozen=True, slots=True)
class AgentsWriteAuthorization:
    schema_version: int
    authorization_id: str
    owner: str
    actor: str
    thread_id: str
    turn_id: str
    request_id: str
    action: str
    target_cas: tuple[str, str, str, int, int]
    content_sha256: str
    issued_at: float
    expires_at: float
    host_request_ref: str
    admin_target_owner: str
    intent_sha256: str

    # LLM: 序列化只验形状，不授予执行权；输出不共享可变列表。
    # 函数用途: 生成完整v1 JSON对象，CAS用数组表示。
    def to_dict(self) -> dict[str, object]:
        _validate_authorization_shape(self)
        result = asdict(self)
        result["target_cas"] = list(self.target_cas)
        return result

    # LLM: 解码不是授权；使用前仍须validate_write_authorization对照可信宿主上下文。
    # 函数用途: 严格解析完整JSON，不接受role/user_explicit等附加权限声明。
    @classmethod
    def from_dict(cls, payload):
        values = decode_fields(cls, payload)
        if type(values["target_cas"]) is not list:
            raise AgentsContractError("AGENTS_CONTRACT_FIELD_INVALID", "target_cas")
        values["target_cas"] = tuple(values["target_cas"])
        authorization = cls(**values)
        _validate_authorization_shape(authorization)
        return authorization


# LLM: 只允许C从当前请求/权限/授权账本构造；无JSON解码入口，不得从模型参数填。
# 类用途: 汇集独立宿主事实；纯校验不消费nonce、不写账本、不取得文件权限。
@dataclass(frozen=True, slots=True)
class AgentsWriteContext:
    schema_version: int
    owner: str
    actor: str
    thread_id: str
    turn_id: str
    request_id: str
    action: str
    target_cas: tuple[str, str, str, int, int]
    content_sha256: str
    now: float
    origin_kind: str
    host_authorization: AgentsWriteAuthorization | None
    consumed_authorization_ids: tuple[str, ...]
    actor_is_admin: bool
    full_access: bool
    admin_target_owner: str
    intent_sha256: str


# LLM: context独立来自宿主；C提交时锁内重读CAS及消费nonce，本函数无副作用。
# 函数用途: 拒绝伪造、旧请求、过期、重放及未明确授权的跨owner操作。
def validate_write_authorization(authorization, context):
    _validate_write_context(context)
    if type(authorization) is not AgentsWriteAuthorization:
        raise AgentsContractError("AGENTS_WRITE_AUTHORIZATION_REQUIRED", "authorization")
    _validate_authorization_shape(authorization)
    if context.origin_kind != "user_dialogue" or context.host_authorization is None:
        raise AgentsContractError("AGENTS_WRITE_NOT_USER_REQUEST", "origin_kind")
    if authorization != context.host_authorization:
        raise AgentsContractError("AGENTS_WRITE_NOT_HOST_ISSUED", "authorization_id")
    _validate_current_binding(authorization, context)
    if authorization.authorization_id in context.consumed_authorization_ids:
        raise AgentsContractError("AGENTS_WRITE_REPLAYED", "authorization_id")
    if not authorization.issued_at <= context.now < authorization.expires_at:
        raise AgentsContractError("AGENTS_WRITE_EXPIRED", "expires_at")
    _validate_owner_grant(authorization, context)
    return authorization


# LLM: 同时绑定旧来源CAS和拟写内容/新来源摘要；不打开定位，不代替权限核验。
# 函数用途: 验证身份、旧目标和操作意图；配置恢复允许旧内容摘要为空。
def _validate_binding_fields(record) -> None:
    for field in ("owner", "actor", "thread_id", "turn_id", "request_id"):
        require_text(getattr(record, field), field)
    if record.action not in {"write", "set_source"}:
        raise AgentsContractError("AGENTS_WRITE_ACTION_UNKNOWN", "action")
    cas = record.target_cas
    for text in cas[:3]:
        require_text(text, "target_cas")
    require_counter(cas[3], "target_cas")
    require_counter(cas[4], "target_cas", allow_zero=True)
    if cas[0] != record.owner or cas[1] not in {"owner_default", "local_path", "url"}:
        raise AgentsContractError("AGENTS_WRITE_BINDING_MISMATCH", "target_cas")
    _validate_locator(cas[1], cas[2])
    _validate_write_target(record)
    require_digest(record.intent_sha256, "intent_sha256")


# LLM: set_source修改owner内配置而非网络正文；内容不可用不阻断显式修复配置。
# 函数用途: 区分只读网络正文和配置写入的前像摘要要求。
def _validate_write_target(record) -> None:
    if record.action == "write" and record.target_cas[1] == "url":
        raise AgentsContractError("AGENTS_WRITE_SOURCE_READ_ONLY", "target_cas")
    if record.action == "set_source" and record.content_sha256 == "":
        return
    require_digest(record.content_sha256, "content_sha256")


# LLM: 所有入口均精确授权类型；时间戳来自宿主，不让子类/JSON别名参与比较。
# 函数用途: 核对授权投影完整形状与有效期，不验证其发行者。
def _validate_authorization_shape(authorization) -> None:
    if type(authorization) is not AgentsWriteAuthorization:
        raise AgentsContractError("AGENTS_CONTRACT_OBJECT_REQUIRED", "authorization")
    validate_scalars(authorization)
    _validate_binding_fields(authorization)
    require_text(authorization.authorization_id, "authorization_id")
    require_text(authorization.host_request_ref, "host_request_ref")
    require_text(authorization.admin_target_owner, "admin_target_owner", allow_empty=True)
    if authorization.issued_at <= 0 or authorization.expires_at <= authorization.issued_at:
        raise AgentsContractError("AGENTS_CONTRACT_FIELD_INVALID", "expires_at")


# LLM: context不能由JSON或任意类替代；验形状不意味着模型提交的native对象可信。
# 函数用途: 验证当前请求、权限事实、消费记录及原授权结构。
def _validate_write_context(context) -> None:
    if type(context) is not AgentsWriteContext:
        raise AgentsContractError("AGENTS_WRITE_CONTEXT_REQUIRED", "context")
    validate_scalars(context)
    _validate_binding_fields(context)
    require_text(context.origin_kind, "origin_kind")
    require_text(context.admin_target_owner, "admin_target_owner", allow_empty=True)
    if context.now <= 0:
        raise AgentsContractError("AGENTS_CONTRACT_FIELD_INVALID", "now")
    for identifier in context.consumed_authorization_ids:
        require_text(identifier, "consumed_authorization_ids")
    if context.host_authorization is not None:
        _validate_authorization_shape(context.host_authorization)


# LLM: C须从实际拟写正文/新来源计算意图摘要；不接受模型随包提交的digest。
# 函数用途: 核对完整当前绑定及操作意图，旧CAS一致也不能替换新值。
def _validate_current_binding(authorization, context) -> None:
    names = ("owner", "actor", "thread_id", "turn_id", "request_id", "action",
             "target_cas", "content_sha256", "intent_sha256")
    if any(getattr(authorization, name) != getattr(context, name) for name in names):
        raise AgentsContractError("AGENTS_WRITE_BINDING_MISMATCH", "request_id")


# LLM: actor为规范操作者owner；管理员默认不跨owner，full_access或admin旗标单独无效。
# 函数用途: 跨owner须管理员、full access、原授权和当前宿主的精确目标同时成立。
def _validate_owner_grant(authorization, context) -> None:
    if authorization.owner == authorization.actor:
        if authorization.admin_target_owner or context.admin_target_owner:
            raise AgentsContractError("AGENTS_WRITE_ADMIN_TARGET_UNEXPECTED", "admin_target_owner")
        return
    if not (context.actor_is_admin and context.full_access):
        raise AgentsContractError("AGENTS_WRITE_CROSS_OWNER_FORBIDDEN", "owner")
    if authorization.admin_target_owner != authorization.owner or context.admin_target_owner != authorization.owner:
        raise AgentsContractError("AGENTS_WRITE_ADMIN_TARGET_REQUIRED", "admin_target_owner")


# LLM: C从实际待写参数计算并与原用户授权绑定；纯函数，不读权限或写正文。
# 函数用途: 统一拟写正文和拟设新来源的摘要配方，不静默改文本或定位。
def write_intent_digest(action, value) -> str:
    require_text(action, "action")
    if action not in {"write", "set_source"}:
        raise AgentsContractError("AGENTS_WRITE_ACTION_UNKNOWN", "action")
    text = value if action == "write" else _source_intent_json(value)
    if type(text) is not str:
        raise AgentsContractError("AGENTS_CONTRACT_FIELD_INVALID", "intent_value")
    try:
        return sha256(text.encode("utf-8")).hexdigest()
    except UnicodeError as error:
        raise AgentsContractError("AGENTS_CONTRACT_FIELD_INVALID", "intent_value") from error


# LLM: 先捕获新来源副本，严格原生键且规范词法校验；C仍须提交同一冻结值，无I/O。
# 函数用途: 生成键排序、无额外空格、不转义中文的规范JSON意图。
def _source_intent_json(value) -> str:
    if type(value) is not dict:
        raise AgentsContractError("AGENTS_CONTRACT_FIELD_INVALID", "intent_value")
    value = value.copy()
    if not all(type(key) is str for key in value) or set(value) != {"kind", "locator"}:
        raise AgentsContractError("AGENTS_CONTRACT_FIELD_INVALID", "intent_value")
    require_text(value["kind"], "kind")
    if value["kind"] not in {"owner_default", "local_path", "url"}:
        raise AgentsContractError("AGENTS_SOURCE_KIND_UNKNOWN", "kind")
    _validate_locator(value["kind"], value["locator"])
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
