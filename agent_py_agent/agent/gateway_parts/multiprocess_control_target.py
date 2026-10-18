# LLM: C只读认证owner原canonical Store；冷路径显式config/home和资源约束，不造Agent替身；默认ready读取与目标算法保持。
# 模块用途: 异步stop/btw首次入队固定真实执行目标，无法固定时明确拒绝。
from dataclasses import replace

from ..conversation.control_commands import ConversationControlResult
from ..runtime_errors import DataCorruptionError
from ..user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from .execution_target import GatewayExecutionTarget, read_gateway_execution_target
from .owner_conversation_store import owner_conversation_store

TARGETED_CONTROL_KINDS = frozenset({'stop', 'steer'})


# LLM: 客户端原目标只做结构核验，W执行前仍验证当前claim；无客户端目标则按认证通道读取原owner。
#   不改input_digest绑定的原输入：服务端目标存入dispatch，重试永远复用第一次冻结事实。
#   冷C显式配置/根复用下方只读叶子，blocking/deadline只限制资源，不改变目标资格；默认调用不增加读取。
# 函数用途: 为异步控制取一次不可变执行快照；坏目标、坏账或冷目录缺失均不改选未来任务。
def freeze_control_execution(agent, paths, receipt, *, config=None, home_root=None, blocking=True, deadline=None):
    if receipt.input_digest_version >= 4:
        target = GatewayExecutionTarget.from_dict(receipt.expected_execution)
    elif config is not None and home_root is not None:
        target = _read_authenticated_execution_from_config(config, home_root, paths, receipt,
            blocking=blocking, deadline=deadline)
    else:
        target = _read_authenticated_execution(agent, paths, receipt)
    if target is None or (receipt.expected_turn_id and receipt.expected_turn_id != target.turn_id):
        return None
    return target.to_dict()


# LLM: 明确config/home沿原规范owner与runtime_paths规则；foreign只清原覆盖项，initialize=False不建目录；所有写锁均不进入此叶子。
# 函数用途: 无Agent时从已有绑定读legacy真实目标，忙或到期原样交给运输层，不改选latest或未来任务。
def _read_authenticated_execution_from_config(config, home_root, paths, receipt, *, blocking, deadline):
    from ..common.json_io import check_io_deadline
    from ..user_space.home_layout import home_paths
    from ..user_space.owner_resolver import owner_identity_from_config
    from ..user_space.runtime_paths import resolve_runtime_paths_for_agent
    from .control_operation_service import _receipt_owner_identity
    from .owner_conversation_store import owner_conversation_store_from_config

    check_io_deadline(deadline)
    base = home_paths(home_root)
    owner = _receipt_owner_identity(receipt)
    home = home_paths_with_owner(base, resolve_owner_home(home_root, owner))
    selected = config
    if owner != owner_identity_from_config(config):
        fields = resolve_runtime_paths_for_agent(config, base.owner_home_dir, base).paths
        selected = replace(config, **{key: '' for key in fields if hasattr(config, key)})
    store = owner_conversation_store_from_config(selected, home, initialize=False)
    check_io_deadline(deadline)
    thread, error = store.threads.resolve_report(channel=receipt.channel,
        channel_conversation_id=receipt.conversation_id, channel_user_id=receipt.user_id)
    check_io_deadline(deadline)
    if error is not None or thread is None:
        return None
    return read_gateway_execution_target(paths, store, thread.thread_id, blocking=blocking, deadline=deadline)


# LLM: 认证身份已在原回执冻结；home解析及initialize=False只打开原位置，任何解析失败均无资格。
# 函数用途: 从认证owner原通道绑定找真实执行者，不触发Agent加载和owner目录初始化。
def _read_authenticated_execution(agent, paths, receipt):
    from .control_operation_service import _receipt_owner_identity

    try:
        home = resolve_owner_home(agent.home_paths.root, _receipt_owner_identity(receipt))
        scoped_home = home_paths_with_owner(agent.home_paths, home)
        store = owner_conversation_store(agent, scoped_home, initialize=False)
        thread, error = store.threads.resolve_report(channel=receipt.channel,
            channel_conversation_id=receipt.conversation_id, channel_user_id=receipt.user_id)
        if error is not None or thread is None:
            return None
        return read_gateway_execution_target(paths, store, thread.thread_id)
    except (AttributeError, OSError, RuntimeError, ValueError, TypeError, KeyError, DataCorruptionError):
        return None


# LLM: 原prepared回执提交明确completed拒绝，不创建dispatch或产生owner副作用；调用方持原控制锁/C门。
# 函数用途: 将无法安全冻结目标的控制封成可重试读取的失败结果。
def refuse_unfreezable_control(receipt, current):
    result = ConversationControlResult(receipt.command_kind, False,
        '未能固定原任务的执行目标，控制未入队。', error_code='CONTROL_EXECUTION_TARGET_REQUIRED')
    return replace(receipt, state='completed', result=result.to_dict(), error={}, updated_at=current)
