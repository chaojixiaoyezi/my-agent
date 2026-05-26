from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.contracts.tool_call_policy import (
    ToolCallPolicy,
    validate_tool_call_policy,
)
from agent_py_agent.tests.support.xlsx_fixtures import write_xlsx_fixture


class FakeToolRunner:
    def __init__(
        self,
        run_dir: Path,
        *,
        fixtures: dict[str, object] | None = None,
        policy: ToolCallPolicy | None = None,
    ):
        self.run_dir = run_dir
        self.fixtures = fixtures or {}
        self.policy = policy
        self.trace: list[dict[str, object]] = []

    def execute(self, tool: str, params: dict[str, object]) -> dict[str, object]:
        policy_error = self._policy_error(tool, params)
        if policy_error is not None:
            result = policy_error
        else:
            result = self._execute_allowed(tool, params)
        self.trace.append({"tool": tool, "params": dict(params), "result": dict(result)})
        return result

    def _policy_error(self, tool: str, params: dict[str, object]) -> dict[str, object] | None:
        if self.policy is None:
            return None
        decision = validate_tool_call_policy({"tool": tool, "args": params}, self.policy)
        if decision.ok:
            return None
        return {
            "tool": tool,
            "ok": False,
            "error_code": decision.error_code,
            "findings": list(decision.findings),
        }

    def _execute_allowed(self, tool: str, params: dict[str, object]) -> dict[str, object]:
        fixture_result = self._fixture_result(tool, params)
        if fixture_result is not _NO_FIXTURE:
            return _normalize_result(tool, fixture_result)
        handler = getattr(self, f"_tool_{tool}", None)
        if handler is None:
            return {"tool": tool, "ok": False, "error_code": "TOOL_NOT_FOUND"}
        return _normalize_result(tool, handler(params))

    def _tool_write_file(self, params: dict[str, object]) -> dict[str, object]:
        path = _resolve_run_path(self.run_dir, params.get("path"))
        if path is None:
            return {"tool": "write_file", "ok": False, "error_code": "PATH_OUTSIDE_RUN_DIR"}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(params.get("content") or ""), encoding="utf-8")
        return {"tool": "write_file", "ok": True, "path": str(path)}

    def _tool_read_file(self, params: dict[str, object]) -> dict[str, object]:
        path = _resolve_run_path(self.run_dir, params.get("path"))
        if path is None:
            return {"tool": "read_file", "ok": False, "error_code": "PATH_OUTSIDE_RUN_DIR"}
        if not path.exists():
            return {"tool": "read_file", "ok": False, "error_code": "PATH_NOT_FOUND"}
        return {"tool": "read_file", "ok": True, "content": path.read_text(encoding="utf-8")}

    def _tool_fetch_url(self, params: dict[str, object]) -> dict[str, object]:
        url = str(params.get("url") or "")
        fixtures = self.fixtures.get("fetch_url")
        mapping = fixtures if isinstance(fixtures, dict) else {}
        value = mapping.get(url)
        if isinstance(value, dict):
            return {"tool": "fetch_url", **value}
        return {"tool": "fetch_url", "ok": False, "error_code": "NETWORK_UNAVAILABLE"}

    def _tool_write_workbook_fixture(self, params: dict[str, object]) -> dict[str, object]:
        target = write_xlsx_fixture(self.run_dir, dict(params))
        return {
            "tool": "write_workbook_fixture",
            "ok": True,
            "path": str(target),
        }

    def _tool_dangerous_command(self, params: dict[str, object]) -> dict[str, object]:
        return {
            "tool": "dangerous_command",
            "ok": False,
            "command": str(params.get("command") or ""),
            "error_code": "APPROVAL_REQUIRED",
        }

    def _tool_send_message(self, params: dict[str, object]) -> dict[str, object]:
        target = str(params.get("target") or "").strip()
        idempotency_key = str(params.get("idempotency_key") or "").strip()
        if not target or not idempotency_key:
            return {"tool": "send_message", "ok": False, "error_code": "MESSAGE_CONTRACT_INVALID"}
        return {
            "tool": "send_message",
            "ok": True,
            "delivery_id": f"msg-{abs(hash((target, idempotency_key))) % 100000}",
            "idempotency_key": idempotency_key,
        }

    def _tool_block_ip(self, params: dict[str, object]) -> dict[str, object]:
        ip = str(params.get("ip") or "").strip()
        mode = str(params.get("mode") or "dry_run").strip()
        if not ip:
            return {"tool": "block_ip", "ok": False, "error_code": "IP_REQUIRED"}
        if mode != "dry_run" and not params.get("approval_id"):
            return {"tool": "block_ip", "ok": False, "error_code": "APPROVAL_REQUIRED", "ip": ip}
        return {"tool": "block_ip", "ok": True, "ip": ip, "mode": mode, "ticket": f"FW-{ip.replace('.', '-')}"}

    def _tool_browser_open(self, params: dict[str, object]) -> dict[str, object]:
        url = str(params.get("url") or "").strip()
        if not url:
            return {"tool": "browser_open", "ok": False, "error_code": "URL_REQUIRED"}
        return {"tool": "browser_open", "ok": True, "url": url, "dom_ref": "artifact://browser/dom-snapshot.json"}

    def _tool_create_ticket(self, params: dict[str, object]) -> dict[str, object]:
        title = str(params.get("title") or "").strip()
        if not title:
            return {"tool": "create_ticket", "ok": False, "error_code": "TICKET_TITLE_REQUIRED"}
        return {"tool": "create_ticket", "ok": True, "ticket_id": f"TICKET-{abs(hash(title)) % 100000}"}

    def _fixture_result(self, tool: str, params: dict[str, object]) -> object:
        fixture = self.fixtures.get(tool)
        if not isinstance(fixture, dict):
            return _NO_FIXTURE
        for key in _fixture_lookup_keys(params):
            if key in fixture:
                value = fixture[key]
                return {"tool": tool, **value} if isinstance(value, dict) else value
        default = fixture.get("__default__") or fixture.get("default")
        if isinstance(default, dict):
            return {"tool": tool, **default}
        return default if default is not None else _NO_FIXTURE


def _fixture_lookup_keys(params: dict[str, object]) -> list[str]:
    keys: list[str] = []
    for field in ("path", "source_json_path", "url", "command"):
        value = str(params.get(field) or "").strip()
        if value and value not in keys:
            keys.append(value)
    return keys


def _normalize_result(tool: str, value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {"tool": tool, **value}
    return {"tool": tool, "ok": False, "error_code": "TOOL_RESULT_INVALID"}


def _resolve_run_path(run_dir: Path, value: object) -> Path | None:
    raw = str(value or "").strip()
    candidate = Path(raw)
    path = candidate if candidate.is_absolute() else run_dir / candidate
    resolved = path.resolve()
    try:
        resolved.relative_to(run_dir.resolve())
        return resolved
    except ValueError:
        return None


_NO_FIXTURE = object()
