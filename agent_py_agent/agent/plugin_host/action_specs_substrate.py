# LLM: 通用底座静态合同仍合入唯一Host目录；能力声明不授管理员身份、路径、代码执行或IdP验签权。
# 模块用途: 声明包管理、无账本调试和通用认证的十个Host v2动作。
from __future__ import annotations

from .action_specs import _spec, id_schema
from .schemas import closed, integer, text


# LLM: TOTP/SSO会改变单次状态但不写通用幂等账，防secret和登录票据被缓存；域内计数/state负责防重放。
# 函数用途: 给通用底座提供闭合参数、明确权限和原状态所属范围。
def substrate_action_specs():
    uid = {'user_id': text(64, 3, r'^[a-z][a-z0-9_-]{2,63}$')}
    code = {'code': text(64, 1)}
    binding = {'browser_binding': text(1024, 16)}
    return [
        _spec('plugins.packages.install', 'write', 'plugins.manage', closed({}, {
            'package_base64': text(65536, 1), 'package_path': text(4096, 1), 'artifact_ref': id_schema(),
            'expected_revision': integer(2**63 - 1, 0), 'sha256': text(64, 64, r'^[a-f0-9]{64}$'),
            'plugin_id': id_schema()}), 'admin staging_or_artifact'),
        _spec('plugins.packages.lifecycle', 'execute', 'plugins.manage', closed({'plugin_id': id_schema(),
            'action': {'enum': ['enable', 'disable', 'reload', 'uninstall', 'status']}}), 'admin'),
        _spec('workflows.steps.debug', 'execute', 'workflows.debug', closed({'workflow_id': id_schema(), 'step_id': id_schema()}, {
            'mock_inputs': {'type': 'object'}, 'context_overrides': {'type': 'object'},
            'timeout_seconds': {'type': 'number', 'minimum': 0.001, 'maximum': 3600}}), 'admin owner_workflow'),
        _spec('users.auth.totp.setup', 'execute', 'users.manage', closed(uid), 'manageable_user'),
        _spec('users.auth.totp.verify', 'execute', 'users.manage', closed({**uid, **code}), 'manageable_user'),
        _spec('users.auth.totp.disable', 'execute', 'users.manage', closed({**uid, **code}), 'manageable_user'),
        _spec('users.auth.sso.config.get', 'read', 'users.manage', closed({}), 'admin'),
        _spec('users.auth.sso.config.update', 'execute', 'users.manage', closed({'config': {'type': 'object'}}), 'admin'),
        _spec('users.auth.sso.initiate', 'execute', 'users.manage', closed(binding), 'admin'),
        _spec('users.auth.sso.callback', 'execute', 'users.manage', closed({**binding,
            'state': text(4096, 1), 'nonce': text(256, 1), 'response': {'type': ['object', 'string']}},
            {'totp_code': text(64, 1)}), 'admin verified_identity'),
    ]


SUBSTRATE_ACTIONS = frozenset(spec.name for spec in substrate_action_specs())
