# LLM: 投递错误与协议值是公开服务、工具及命令共同合同；身份必须由可信调用方绑定，不从资产正文授权。
# 模块用途: 定义跨用户投递协议版本、配额及不携带秘密的结构化错误。
from __future__ import annotations

from dataclasses import dataclass, fields

ENVELOPE_SCHEMA = 'asset_delivery_envelope.v1'
RECEIPT_SCHEMA = 'asset_delivery_receipt.v1'
BLOCKLIST_SCHEMA = 'asset_delivery_blocklist.v1'
MAX_PENDING_DELIVERIES = 50
MAX_BUNDLE_BYTES = 10 * 1024 * 1024


# LLM: 按原六个可选参数的顺序/默认值冻结，重复及未知参数拒绝；这不是管理员身份授权来源。
# 类用途: 将发送宽参数归并为值对象，保持原位置及命名调用的行为合同。
@dataclass(frozen=True)
class DeliverySendOptions:
    note: str = ''
    include_dependencies: bool = True
    allow_secret_placeholder_replace: bool = False
    auto_accept: bool = False
    auto_enable: bool = False
    sender_role: str = 'user'

    # LLM: 纯参数绑定，不读写目录或根据角色文本授予权限。
    # 函数用途: 验证旧尾随位置参数及命名选项并构造发送值对象。
    @classmethod
    def from_call(cls, positional, options):
        names = [field.name for field in fields(cls)]
        if len(positional) > len(names) or set(options) - set(names):
            raise TypeError('未知或过多的发送参数')
        values = dict(zip(names, positional, strict=False))
        if values.keys() & options.keys():
            raise TypeError('发送参数重复赋值')
        return cls(**values, **options)


# LLM: error_code及details是唯一机器读口；调用方只传已脱敏说明或安全ID，不得放入原始文件、令牌或异常正文。
# 类用途: 给投递和运行禁用门提供可统一转换成工具回执的错误。
class AssetDeliveryError(ValueError):
    # LLM: 不读写文件、不根据文案分类；details由抛出点构造且保持结构化。
    # 函数用途: 保存机器错误码及安全诊断字段。
    def __init__(self, error_code: str, message: str = '资产操作未完成', **details):
        self.error_code = error_code
        self.details = details
        super().__init__(message)

    # LLM: 仅返回显式错误字段，不包含异常链或调用栈中的秘密。
    # 函数用途: 生成服务调用方可直接使用的失败JSON。
    def to_dict(self):
        return {'ok': False, 'error_code': self.error_code, 'message': str(self), **self.details}


# LLM: 仅表示Owner明确关闭的资产，不允许执行器将其降成可跳过、可分析兜底的脚本故障。
# 类用途: 统一工作流、脚本及Skill的真实运行禁用码。
class AssetDisabledError(AssetDeliveryError):
    # LLM: 只保存资产逻辑身份，不携带正文或宿主路径。
    # 函数用途: 构造接收后尚未开启资产的硬拒绝。
    def __init__(self, asset_kind: str, asset_id: str):
        super().__init__('ASSET_DISABLED_BY_OWNER', '资产已停用，请由本人先开启',
            asset_kind=asset_kind, asset_id=asset_id)
