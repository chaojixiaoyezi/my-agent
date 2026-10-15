# LLM: 此模块只包装原凭据存储错误；不创建第二存储或复制异常正文，调用方必须提供准确副作用阶段。
# 模块用途: 为凭据链返回安全、可追踪的失败事实，防止未发送被混记为发送未知。
import json
import logging
from contextlib import contextmanager

from ..common.nofollow_fs import read_text_beneath
from .owner_quota import OwnerQuotaExceeded, OwnerQuotaLockContention, OwnerQuotaUnavailable


# LLM: 保持 ValueError 调用兼容；诊断仅允许异常类型和整数 errno，不能携带秘密或异常任意正文。
# 类用途: 表达凭据请求失败阶段、错误码和已知发送事实。
class CredentialInputError(ValueError):
    # LLM: 固定消息来自宿主；诊断用于工具账而非权限判断，修改需同步工具与故障测试。
    # 函数用途: 保存错误事实，不读取或写入任何存储。
    def __init__(self, code, message, *, stage='preflight', effect_outcome='not_started', diagnostic=None):
        super().__init__(message)
        self.code, self.stage, self.effect_outcome = code, stage, effect_outcome
        self.diagnostic = diagnostic or {}
        logging.getLogger(__name__).warning('credential_failure code=%s stage=%s effect_outcome=%s diagnostic=%s',
            code, stage, effect_outcome, self.diagnostic)


# LLM: 只包裹调用方明确命名的阶段；已有 typed 错误原样保留，密钥校验 ValueError 只在 key 阶段分类。
# 函数用途: 将原存储、配额、依赖故障转成安全的结构化错误，不重试、不改权限或重置密钥。
@contextmanager
def credential_stage(stage, *, effect_outcome='not_started', key_validation=False, diagnostic=None):
    try:
        yield
    except CredentialInputError:
        raise
    except (OSError, RuntimeError, ImportError, ValueError) as exc:
        if isinstance(exc, ValueError) and not key_validation:
            raise
        code = _storage_error_code(exc)
        diagnostic = {**(diagnostic or {}), 'exception_type': type(exc).__name__}
        if isinstance(exc, OSError) and isinstance(exc.errno, int):
            diagnostic['errno'] = exc.errno
        raise CredentialInputError(code, '凭据操作失败，请按失败阶段检查受管存储和原卡片；不要在聊天中发送密码。',
            stage=stage, effect_outcome=effect_outcome, diagnostic=diagnostic) from None


# LLM: 分类只看结构化异常类型，禁止解析正文或把配额错误当磁盘权限问题。
# 函数用途: 选出安全错误码，保持存储阶段包装简短。
def _storage_error_code(exc):
    if isinstance(exc, OwnerQuotaLockContention):
        return 'CREDENTIAL_QUOTA_BUSY'
    if isinstance(exc, OwnerQuotaExceeded):
        return 'CREDENTIAL_QUOTA_EXCEEDED'
    if isinstance(exc, OwnerQuotaUnavailable):
        return 'CREDENTIAL_QUOTA_UNAVAILABLE'
    if isinstance(exc, ImportError):
        return 'CREDENTIAL_ENCRYPTION_UNAVAILABLE'
    if isinstance(exc, ValueError):
        return 'CREDENTIAL_KEY_INVALID'
    return 'CREDENTIAL_STORAGE_UNAVAILABLE'


# LLM: 读取原 canonical 凭据文件，不把读取失败当缺失；无新目录或索引，异常正文不进入模型或日志。
# 函数用途: 为请求、回滚和结果读取保留安全 errno，并拒绝损坏记录覆盖。
def read_credential_row(path, *, stage, effect_outcome='not_started'):
    with credential_stage(stage, effect_outcome=effect_outcome):
        try:
            text = read_text_beneath(path.parent, (path.name,))
            if text is None:
                return {}
        except FileNotFoundError:
            return {}
        except UnicodeError as exc:
            raise CredentialInputError('CREDENTIAL_RECORD_INVALID', '凭据记录编码无效，未重建或覆盖原记录。',
                stage=stage, effect_outcome=effect_outcome, diagnostic={'exception_type': type(exc).__name__}) from None
        except ValueError as exc:
            raise CredentialInputError('CREDENTIAL_STORAGE_INVALID', '凭据存储路径不满足受管文件安全约束。',
                stage=stage, effect_outcome=effect_outcome, diagnostic={'exception_type': type(exc).__name__}) from None
        try:
            row = json.loads(text)
        except (ValueError, UnicodeError) as exc:
            raise CredentialInputError('CREDENTIAL_RECORD_INVALID', '凭据记录损坏，未重建或覆盖原记录。',
                stage=stage, effect_outcome=effect_outcome, diagnostic={'exception_type': type(exc).__name__}) from None
        if not isinstance(row, dict):
            raise CredentialInputError('CREDENTIAL_RECORD_INVALID', '凭据记录格式无效，未重建或覆盖原记录。',
                stage=stage, effect_outcome=effect_outcome)
        return row
