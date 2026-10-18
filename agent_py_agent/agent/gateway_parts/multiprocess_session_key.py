# LLM: 准入只投影现有顺序键，不读取 owner 的会话仓库；规范版本独立于预留记录。
# 模块用途: 冻结 C 与 W 共用的纯会话键投影接口。
from __future__ import annotations

import hashlib

from .multiprocess_records import MultiprocessRecordError, _canonical_bytes, _sha, _shape


# LLM: 入参来自现有用户/渠道/外部会话顺序键，空外部会话不制造串行占位。
# 函数用途: 返回版本化会话摘要，不物化线程或写文件。
def project_admission_session(user_key: str, channel: str, channel_conversation_id: str) -> dict:
    for value in (user_key, channel, channel_conversation_id):
        session_text(value)
    version = "multiprocess_session_scope.v1"
    scope = {"schema_version": version, "key_sha256": None}
    if channel_conversation_id:
        canonical = _canonical_bytes({"schema_version": version, "user_key": user_key,
                                      "channel": channel, "channel_conversation_id": channel_conversation_id})
        scope["key_sha256"] = hashlib.sha256(canonical).hexdigest()
    return scope


# LLM: 外部会话文本不是路径标识；只认 UTF-8 字符串，不截断、清洗、回显或作默认转换。
# 函数用途: 支持斜杠、中文、长会话号及显式空会话。
def session_text(value: object) -> None:
    if type(value) is not str:
        raise MultiprocessRecordError("invalid_session_text", "session_scope")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise MultiprocessRecordError("invalid_session_text", "session_scope") from exc


# LLM: v2 预留只接受此独立版本的规范摘要，不接受原始会话正文或 thread 默认值。
# 函数用途: 校验唯一会话键投影；null 表示不额外串行但仍计 owner 准入占用。
def validate_session_scope(value: object) -> None:
    _shape(value, ("schema_version", "key_sha256"), "session_scope")
    if value["schema_version"] != "multiprocess_session_scope.v1":
        raise MultiprocessRecordError("unsupported_session_scope", "session_scope.schema_version")
    if value["key_sha256"] is not None:
        _sha(value["key_sha256"], "session_scope.key_sha256")
