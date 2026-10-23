# LLM: MAC随机引用只承载分页快照，不承载权限；缓存有界、短命，重启后必须重新查询。
# 模块用途: 提供不暴露底层offset的游标编码及安装/owner/动作/过滤器绑定核验。
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass

from .authorization import revalidate_call
from .errors import HostAPIError
from .schemas import canonical_json, freeze, strict_json

# 单个服务允许保留的游标引用数量上限。
CURSOR_MAX_COUNT = 1024
# 游标最长寿命，单位秒。
CURSOR_TTL_SECONDS = 900
# 单个内部位置快照的JSON字节上限。
POSITION_MAX_BYTES = 8192


# LLM: 快照由宿主领域提供，解码只还原已登记且仍匹配的值。
# 类用途: 保存冻结的快照标识和内部位置。
@dataclass(frozen=True)
class CursorPosition:
    snapshot_id: str
    position: object

    # LLM: 调用方不能经别名热改缓存中的位置。
    # 函数用途: 冻结位置结构。
    def __post_init__(self) -> None:
        object.__setattr__(self, "position", freeze(self.position))


# LLM: 不保存会话正文或任务状态；过期/淘汰只使游标失效，不推断业务不存在。
# 类用途: 管理有上限的服务内分页引用与完整性密钥。
class CursorCodec:
    # LLM: 密钥仅属于本服务，测试时只替换时钟，不绕过授权。
    # 函数用途: 初始化私有密钥和有限快照缓存。
    def __init__(self, clock=None):
        self._secret = secrets.token_bytes(32)
        self._clock = clock or time.monotonic
        self._rows = OrderedDict()
        self._lock = threading.Lock()

    # LLM: 位置经有界JSON规范化，随机handle签MAC；摘要绑定全部授权和查询范围，不向插件暴露offset。
    # 函数用途: 为一个已授权查询签发游标。
    def encode(self, authorized, snapshot_id, position, filters=None, ttl_seconds=CURSOR_TTL_SECONDS):
        revalidate_call(authorized)
        if (not isinstance(snapshot_id, str) or not 1 <= len(snapshot_id.encode()) <= 128
                or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= CURSOR_TTL_SECONDS):
            raise HostAPIError("INVALID_SCHEMA")
        encoded = canonical_json(position).encode()
        parsed = strict_json(encoded, POSITION_MAX_BYTES)
        scope = _scope_digest(authorized, filters)
        handle = secrets.token_bytes(32)
        with self._lock:
            while len(self._rows) >= CURSOR_MAX_COUNT:
                self._rows.popitem(last=False)
            self._rows[handle] = (scope, self._clock() + ttl_seconds, CursorPosition(snapshot_id, parsed))
        signature = hmac.digest(self._secret, handle, "sha256")
        return base64.urlsafe_b64encode(handle + signature).rstrip(b"=").decode("ascii")

    # LLM: MAC、当前资格、过滤和owner绑定全部核对；不同服务或过期引用必须重查。
    # 函数用途: 验证opaque游标并返回原位置快照。
    def decode(self, authorized, token, filters=None) -> CursorPosition:
        revalidate_call(authorized)
        handle = self._verify_token(token)
        with self._lock:
            row = self._rows.get(handle)
            if row is None or self._clock() >= row[1]:
                self._rows.pop(handle, None)
                raise HostAPIError("CURSOR_EXPIRED", 410)
            if row[0] != _scope_digest(authorized, filters):
                raise HostAPIError("INVALID_SCHEMA")
            return row[2]

    # LLM: 严格base64形状和恒时MAC比较，不把任意文本当成底层偏移。
    # 函数用途: 验证游标完整性并取得随机引用。
    def _verify_token(self, token: str) -> bytes:
        try:
            if not isinstance(token, str) or len(token) != 86:
                raise ValueError("游标长度不符")
            raw = base64.b64decode(token.encode("ascii") + b"==", altchars=b"-_", validate=True)
            if (len(raw) != 64 or base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != token
                    or not hmac.compare_digest(raw[32:], hmac.digest(self._secret, raw[:32], "sha256"))):
                raise ValueError("游标无效")
            return raw[:32]
        except (ValueError, UnicodeError):
            raise HostAPIError("INVALID_SCHEMA") from None

    # LLM: 服务关闭只清分页投影，绝不删除原会话或任务。
    # 函数用途: 立即废弃该服务所有游标。
    def close(self) -> None:
        with self._lock:
            self._rows.clear()
            self._secret = secrets.token_bytes(32)


# LLM: 排除分页游标自身，其余请求参数和显式过滤都参与绑定，跨授权版本不可续用。
# 函数用途: 计算游标的完整查询范围摘要。
def _scope_digest(authorized, filters) -> str:
    binding = authorized.binding
    payload = {key: value for key, value in authorized.call.payload.items()
               if key not in {"cursor", "event_cursor", "reply_cursor"}}
    scope = {"installation": binding.installation_ref, "activation": binding.activation_id,
             "package": binding.package_sha256, "grant_revision": binding.grant_revision,
             "owner": asdict(authorized.effective_owner), "action": authorized.call.action,
             "user_revision": binding.user_revision, "policy_digest": binding.policy_digest,
             "payload": payload, "filters": filters}
    return hashlib.sha256(canonical_json(scope).encode()).hexdigest()
