# LLM: 唯一Host目录保持闭合；摘要两别名只核字符串，格式/匹配交领域统一错误码；debug仍严格布尔安全默认。
# 模块用途: 声明包管理、安全调试及认证参数，不把静态声明当执行许可。
from __future__ import annotations

from .action_specs import _spec, id_schema
from .schemas import closed, integer, text


# LLM: 认证不进幂等缓存；SHA字符串不在schema检查hex/长度，领域须检查两个别名全部值；debug许可不扩权。
# 函数用途: 生成闭合底座参数及权限，将摘要内容错误留给PACKAGE_SHA256_MISMATCH合同。
def substrate_action_specs():
    uid = {'user_id': text(64, 3, r'^[a-z][a-z0-9_-]{2,63}$')}
    code = {'code': text(64, 1)}
    binding = {'browser_binding': text(1024, 16)}
    return [
        _spec('plugins.packages.install', 'write', 'plugins.manage', closed({}, {
            'package_base64': text(65536, 1), 'package_path': text(4096, 1), 'artifact_ref': id_schema(),
            'expected_revision': integer(2**63 - 1, 0), 'sha256': {'type': 'string'}, 'expected_sha256': {'type': 'string'},
            'plugin_id': id_schema()}), 'admin staging_or_artifact'),
        _spec('plugins.packages.lifecycle', 'execute', 'plugins.manage', closed({'plugin_id': id_schema(),
            'action': {'enum': ['enable', 'disable', 'reload', 'uninstall', 'status']}}), 'admin'),
        _spec('workflows.steps.debug', 'execute', 'workflows.debug', closed({'workflow_id': id_schema(), 'step_id': id_schema()}, {
            'mock_inputs': {'type': 'object'}, 'context_overrides': {'type': 'object'},
            'allow_side_effects': {'type': 'boolean'},
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
