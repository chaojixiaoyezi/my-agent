"""审计 #13(契约化部分)修复真测:审计 details 脱敏——密钥/口令/凭据不落审计文件。

原问题:AuditLogger.log 把 details 原样写入,api_key/password/凭据会落审计文件造成二次泄漏。
真起 AuditLogger 写一条含密钥的审计,断言 entry.details 已脱敏、且密钥明文不出现在落盘的 audit.jsonl。
注:把审计真正接到 shell/write/web_fetch/secret 执行路径(需穿透请求身份)是更大的 wiring,留专项。
"""

from __future__ import annotations

from agent_py_agent.agent.audit import AuditAction, AuditLogger
from agent_py_agent.agent.audit.logger import LogParams, _redact_audit_details


def test_redact_pure_function() -> None:
    out = _redact_audit_details({
        "api_key": "sk-secret",
        "password": "pw",
        "command": "ls -la",  # 非密钥:审计价值,保留
        "nested": {"access_token": "t", "name": "ok"},
    })
    assert out["api_key"] == "[REDACTED]"
    assert out["password"] == "[REDACTED]"
    assert out["command"] == "ls -la"
    assert out["nested"]["access_token"] == "[REDACTED]"  # 嵌套也脱敏
    assert out["nested"]["name"] == "ok"


class _Cfg:
    def __init__(self, path: str) -> None:
        self.audit_log_path = path
        self.audit_enabled = True


def test_secret_value_not_in_audit_file(tmp_path) -> None:
    logger = AuditLogger(_Cfg(str(tmp_path / "audit")))
    entry = logger.log(LogParams(
        action=AuditAction.CREATE_TASK,
        user_id="u",
        channel="chat",
        target_type="secret",
        target_id="x",
        details={"api_key": "super-secret-value-123", "what": "resolve"},
    ))
    assert entry.details["api_key"] == "[REDACTED]"
    audit_text = (tmp_path / "audit" / "audit.jsonl").read_text(encoding="utf-8")
    assert "super-secret-value-123" not in audit_text  # 密钥明文不落审计文件
    assert "resolve" in audit_text  # 非密钥审计信息保留
