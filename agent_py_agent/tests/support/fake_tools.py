from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.tooling.spreadsheet_builder import DataWorkbookTool


class FakeToolRunner:
    def __init__(self, run_dir: Path, *, fixtures: dict[str, object] | None = None):
        self.run_dir = run_dir
        self.fixtures = fixtures or {}
        self.trace: list[dict[str, object]] = []

    def execute(self, tool: str, params: dict[str, object]) -> dict[str, object]:
        handler = getattr(self, f"_tool_{tool}", None)
        if handler is None:
            result = {"tool": tool, "ok": False, "error_code": "TOOL_NOT_FOUND"}
        else:
            result = handler(params)
        self.trace.append({"tool": tool, "params": dict(params), "result": dict(result)})
        return result

    def _tool_write_file(self, params: dict[str, object]) -> dict[str, object]:
        path = self.run_dir / str(params.get("path") or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(params.get("content") or ""), encoding="utf-8")
        return {"tool": "write_file", "ok": True, "path": str(path)}

    def _tool_read_file(self, params: dict[str, object]) -> dict[str, object]:
        path = self.run_dir / str(params.get("path") or "")
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

    def _tool_data_to_workbook(self, params: dict[str, object]) -> dict[str, object]:
        result = DataWorkbookTool(self.run_dir).execute(params)
        return {
            "tool": "data_to_workbook",
            "ok": result.ok,
            "output": result.output,
            "error_code": result.error_code,
        }

    def _tool_dangerous_command(self, params: dict[str, object]) -> dict[str, object]:
        return {
            "tool": "dangerous_command",
            "ok": False,
            "command": str(params.get("command") or ""),
            "error_code": "APPROVAL_REQUIRED",
        }
