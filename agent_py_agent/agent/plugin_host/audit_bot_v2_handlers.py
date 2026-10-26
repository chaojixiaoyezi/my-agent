# LLM: 仅已具备真实领域的Bot动作报告就绪；缺AG1及生产工具接缝明确503，不使用替身成功回执或main执行器。
# 模块用途: 将当前owner的Bot槽位、非打断轮换和历史接到HostComposition。
from ..conversation.bot_sessions import BotSessionManager
from ..user_space.local_user_store import LocalUserStore
from ..user_space.owner_resolver import OwnerIdentity
from .action_specs import action_spec, required_capabilities
from .action_specs_audit_bots import AUDIT_BOT_ACTIONS
from .contracts import DomainResult
from .conversations import read_context
from .errors import HostAPIError
from .schemas import thaw

BOT_READY_ACTIONS = frozenset({"conversations.bots.ensure", "conversations.bots.rollover",
                               "conversations.bots.history.list"})


# LLM: 每次入口、回执重放及返回前冷核本owner；不把安装管理员当当前调用者或接受正文身份。
# 函数用途: 复验当前用户没有停用，并取得同一原上下文。
def _check_caller(authorized, expected):
    context = read_context(authorized)
    if context.identity != expected.identity or context.owner_home.home_dir != expected.owner_home.home_dir:
        raise HostAPIError("RESOURCE_NOT_FOUND", 404)
    users = LocalUserStore(context.owner_home.root)
    if context.identity != OwnerIdentity.local_main():
        users.require_enabled(context.identity.owner_id)
    else:
        parts = context.owner_home.home_dir.relative_to(users.root).parts
        status = users.read_json((*parts, "owner_status.json"))
        if status is not None and status.get("status") != "active":
            raise HostAPIError("USER_DISABLED", 403)
    return context


# LLM: discovery不能把动作登记视为业务已接通；六审计动作和submit待依赖协调，明确不可用。
# 函数用途: 返回当前组合根可真实执行的Bot动作集合。
def audit_bots_ready(action, ports):
    return (action in BOT_READY_ACTIONS and callable(getattr(ports.conversation, "store_resolver", None))
            and getattr(ports.conversation, "cursor_codec", None) is not None)


# LLM: 使用原授权/闭合schema/owner路径；不在缺AG1或执行接缝时接受调用，更不把203/202伪装为任务完成。
# 函数用途: 直接分发已就绪的ensure、rollover和history，缺依赖动作明确拒绝。
def call_audit_bots(authorized, ports):
    action = authorized.call.action
    if action not in AUDIT_BOT_ACTIONS:
        raise HostAPIError("UNSUPPORTED_ACTION")
    context = read_context(authorized)
    _check_caller(authorized, context)
    if not required_capabilities(action, authorized.call.payload) <= authorized.binding.capabilities:
        raise HostAPIError("CAPABILITY_DENIED", 403)
    if not audit_bots_ready(action, ports):
        raise HostAPIError("HOST_NOT_READY", 503)
    payload = action_spec(action).validate_payload(thaw(authorized.call.payload))
    domain = ports.conversation

    # LLM: 回执重放也调用此门，不能沿旧对象缓存enabled或owner状态。
    # 函数用途: 给Bot事务注入当前真实调用的鲜活账号检查。
    def check():
        _check_caller(authorized, context)

    manager = BotSessionManager(context.owner_home, domain.store_resolver(context), check,
                                installation_ref=authorized.binding.installation_ref)
    if action == "conversations.bots.history.list":
        data = manager.history(payload, authorized, context, domain.cursor_codec)
    elif action == "conversations.bots.ensure":
        data = manager.ensure(payload, authorized.call.idempotency_key)
    else:
        data = manager.rollover(payload, authorized.call.idempotency_key)
    check()
    return DomainResult(200, data)
