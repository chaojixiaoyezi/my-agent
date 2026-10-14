# LLM: Gateway认证配置只有此构造/部署检查入口，CLI恢复预检和真实HTTP监听必须共用；不读取秘密或创建监听/账本。
# 模块用途: 防止漏接middleware或IM关闭owner分流形成管理员共享入口，只保留明确无多owner、无IM声明的单机模式。
from __future__ import annotations

from .manager import AuthManager
from .middleware import AuthMiddleware


# LLM: 错误只带固定结构码/原因，不回显配置值、credential、私人路径；调用方必须在恢复/线程/网络副作用前拒绝。
# 类用途: 表示认证或owner部署配置不能满足当前多用户、IM与原中间件的隔离要求。
class AuthConfigurationError(RuntimeError):
    error_code = 'AUTH_CONFIGURATION_UNSAFE'

    # LLM: reason只由本模块结构化配置事实产生，不能把任意模型或供应商正文塞进诊断。
    # 函数用途: 保存可观察原因并告诉管理员应启用认证或使用真正的本机单用户模式。
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__('Gateway配置不可安全启动（AUTH_CONFIGURATION_UNSAFE）。请启用认证并保持中间件一致；IM通道还须启用多owner隔离。纯本机单用户模式须关闭多owner并移除IM通道声明。')


# LLM: 仅字面False表示明确关闭认证，缺配置默认保护；声明与当前凭据可用性分离，不解析消息或解引用secret。
#   显式依赖不得用disabled manager抵触启用配置，但合法调用方的token和manager不被替换。
#   IM关闭分流会把USER请求赋给管理员owner，必须在恢复/绑定前拒；scoping/manager真值沿实际消费者，单机例外仍须字面False。
# 函数用途: 检查原部署配置与注入依赖是否满足认证边界，返回是否需原middleware；失败不写盘、不恢复任务。
def validate_gateway_auth_configuration(config: object, middleware: AuthMiddleware | None = None) -> bool:
    enabled = getattr(config, 'auth_enabled', True) is not False
    scope_value = getattr(config, 'gateway_per_user_owner_scoping', True)
    scoped = bool(scope_value)
    fields = ('feishu_app_id', 'feishu_app_secret', 'feishu_verification_token', 'feishu_encrypt_key',
              'qq_app_id', 'qq_app_secret')
    declared_im = any(str(getattr(config, field, '') or '').strip() for field in fields)
    if declared_im and not scoped:
        raise AuthConfigurationError('im_without_owner_scoping')
    if not enabled:
        if scope_value is not False:
            raise AuthConfigurationError('multiowner_without_auth')
        return False
    if middleware is not None and not bool(getattr(middleware.auth_manager, 'auth_enabled', True)):
        raise AuthConfigurationError('middleware_disabled')
    return True


# LLM: 这是原CLI配置→middleware的唯一构造点；调用方漏依赖时可重用它，既有角色/部署token/G1绑定仍原样。
# 函数用途: 按原配置创建受保护middleware，明确合法单机返回None，不提前读凭据或建立另一身份协议。
def build_gateway_auth_middleware(config: object) -> AuthMiddleware | None:
    if not validate_gateway_auth_configuration(config):
        return None
    manager = AuthManager(admin_user_id=getattr(config, 'admin_user_id', 'admin'), auth_enabled=True)
    return AuthMiddleware(manager, auth_token=getattr(config, 'gateway_auth_token', ''))
