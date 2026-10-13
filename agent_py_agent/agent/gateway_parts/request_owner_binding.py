# LLM: 普通请求的 owner 只由认证通道解析并在首次落盘前冻结；请求正文和模型自报身份不能设置这份宿主字段。
# 模块用途: 为原 Gateway 队列和适配器入站记录保存明确归属，重放不因管理员绑定变化串户。
from __future__ import annotations

from dataclasses import asdict

from ..runtime_errors import DataCorruptionError
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home

REQUEST_OWNER_KEY = 'host_request_owner.v1'


# LLM: 冻结记录必须完整且规范；safe_path_segment 的清洗不能默默把另一个 ID 当成原身份。
# 函数用途: 从宿主请求记录恢复准确 owner，损坏时失败关闭。
def frozen_request_owner(payload: dict) -> OwnerIdentity | None:
    raw = payload.get(REQUEST_OWNER_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {'provider', 'owner_kind', 'owner_id'}:
        raise DataCorruptionError('请求 owner 绑定损坏')
    if any(not isinstance(x, str) or not x for x in raw.values()):
        raise DataCorruptionError('请求 owner 绑定缺少身份')
    if raw['provider'] == 'local' and raw['owner_kind'] == 'main' and raw['owner_id'] == 'main':
        result = OwnerIdentity.local_main()
    elif raw['provider'] != 'local' and raw['owner_kind'] in {'user', 'group'}:
        factory = OwnerIdentity.provider_user if raw['owner_kind'] == 'user' else OwnerIdentity.provider_group
        result = factory(raw['provider'], raw['owner_id'])
    else:
        raise DataCorruptionError('请求 owner 绑定不规范')
    if asdict(result) != raw:
        raise DataCorruptionError('请求 owner 绑定越界')
    return result


# LLM: 仅首次认证入口调用；纯队列替身无 Agent 不做身份裁决，生产宿主必须存在。已有冻结字段不能改绑。
# 函数用途: 在原载荷添加准确 owner 与规范目录编号，不创建用户或会话。
def bind_request_owner(agent: object, payload: dict) -> dict:
    if agent is None:
        return payload
    from .request_worker import _resolve_request_owner_identity
    owner = _resolve_request_owner_identity(agent, payload)
    encoded = asdict(owner)
    expected = (payload.get('metadata') or {}).get(REQUEST_OWNER_KEY)
    if expected is not None and expected != encoded:
        raise DataCorruptionError('入站与 Gateway owner 归属不一致')
    return {**payload, REQUEST_OWNER_KEY: encoded, 'owner_id': resolve_owner_home(agent.home_paths.root, owner).owner_id}
