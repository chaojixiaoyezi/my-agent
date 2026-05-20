from __future__ import annotations

from pathlib import Path


class FakeToolRunner:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
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
