# LLM: 跨实例历史只输出无授权的role/content快照；复用宿主秘密净化，路径不变成远端文件读取引用。
# 模块用途: 为单向推送导出有界、脱敏的会话上下文，不复制配置、人格或执行身份。
from __future__ import annotations

import json
import re

from ..common.log_redaction import redact_sensitive_value

_PATH = re.compile(r"(?<![\w:/])(?:file://|~[/\\]|[A-Za-z]:[/\\]|\\\\|//|/(?!/))[^\s\"'<>]*")


# LLM: 先使用公共凭据字段/PEM/Token规则，再移除路径及当前已知秘密；无磁盘或网络副作用。
# 函数用途: 给即将跨实例发送的JSON值生成脱敏副本。
def sanitize_remote_context(value, *, secrets=()):
    clean = redact_sensitive_value(value)
    # LLM: 遍历已净化JSON只处理文本，字段名同样不能泄露绝对路径；不解释内容为权限。
    # 函数用途: 移除嵌套值与键中的路径和本次宿主凭据。
    def scrub(item):
        if isinstance(item, str):
            for secret in secrets:
                if secret:
                    item = item.replace(secret, "<redacted>")
            return _PATH.sub("<host-path>", item)
        if isinstance(item, list):
            return [scrub(child) for child in item]
        if isinstance(item, dict):
            return {scrub(key): scrub(child) for key, child in item.items()}
        return item

    return scrub(clean)


# LLM: source_thread_id只能由当前owner的实际库解析；管理员例外仍须显式传入已授权库，不枚举磁盘猜线程。
# 函数用途: 导出原会话的普通文本历史，拒绝跨用户、损坏和超大上下文。
def export_remote_context(store, source_thread_id, caller_owner):
    thread = store.threads.require(source_thread_id)
    if thread.owner_id != caller_owner and caller_owner != "local/main":
        raise ValueError("REMOTE_PUSH_SOURCE_THREAD_DENIED")
    rows, errors = store.messages.recent_report(source_thread_id, limit=0)
    if errors or any(row.thread_id != source_thread_id for row in rows):
        raise ValueError("REMOTE_PUSH_SOURCE_THREAD_DENIED")
    rows = [row for row in rows if row.role in {"user", "assistant", "tool"}]
    if len(rows) > 256:
        raise ValueError("REMOTE_PUSH_CONTEXT_TOO_LARGE")
    snapshot = sanitize_remote_context([{"role": row.role, "content": row.content} for row in rows])
    if len(json.dumps(snapshot, ensure_ascii=False).encode()) > 49152:
        raise ValueError("REMOTE_PUSH_CONTEXT_TOO_LARGE")
    return snapshot
