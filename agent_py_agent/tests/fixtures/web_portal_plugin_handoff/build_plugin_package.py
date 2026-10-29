# LLM: 构建符合 plugin_package.v9 规范的真实可执行插件压缩包，包含完整 SHA-256 清单、16 项 Host v2 能力声明与标准 MCP JSON-RPC 2.0 入口脚本。
# 模块用途: 生成并校验 host-workbench.zip（管理员全能力版）与 host-workbench-private.zip（普通用户私有版）插件包。
from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from dataclasses import asdict
from pathlib import Path

# Use the explicitly selected build cwd, otherwise the installed framework. Never probe other trees.
REPO_ROOT = Path.cwd()
if (REPO_ROOT / "agent_py_agent/__init__.py").is_file() and str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_py_agent.agent.command_arguments import ArgumentSpec, CommandActionSpec  # noqa: E402
from agent_py_agent.agent.plugin_manifest_host_v9 import host_capabilities  # noqa: E402
from agent_py_agent.agent.plugin_package import (  # noqa: E402
    PluginPackageSnapshot,
    inspect_plugin_package,
)

PLUGIN_DIR = Path(__file__).resolve().parent
DEFAULT_ZIP_PATH = PLUGIN_DIR / "host-workbench.zip"
PRIVATE_ZIP_PATH = PLUGIN_DIR / "host-workbench-private.zip"
PENTEST_ZIP_PATH = PLUGIN_DIR / "web-pentest-portal.zip"
SECOPS_ZIP_PATH = PLUGIN_DIR / "dist/my-agent-web-portal-plugin.zip"

TOOL_SPEC = {
    "name": "workbench_status",
    "description": "查询插件 MCP 状态及配置的 HTTP 地址；HTTP 后端须通过 --serve-http 显式启动，地址不表示已监听",
    "requested_effect": "read_only",
    "input_schema": {
        "type": "object",
        "properties": {
            "port": {"type": "integer"},
        },
        "additionalProperties": False,
    },
}

PLUGIN_MAIN_SCRIPT = """#!/usr/bin/env python3
# LLM: 插件标准 MCP stdio 服务端入口，支持 initialize、tools/list 与 tools/call，并可在需要时返回或拉起本地工作台 HTTP 服务。
# 模块用途: 作为 plugin_package.v9 的 bin/plugin_main.py 可执行入口，供 PluginInstallService 和 MCP 客户端真实握手调用。
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

TOOLS = __TOOLS_JSON__


# LLM: 处理单条 MCP JSON-RPC 请求并返回结果字典，不修改宿主全局状态。
# 函数用途: 响应 initialize、tools/list 和 tools/call(workbench_status) 请求。
def handle_rpc_request(request: dict) -> dict:
    method = request.get("method", "")
    params = request.get("params") or {}
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "host-workbench", "version": "1.0.0"},
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        arguments = params.get("arguments") or {}
        port = int(arguments.get("port") or os.environ.get("MY_AGENT_WORKBENCH_PORT") or 18429)
        payload = {
            "plugin_id": "host-workbench",
            "schema_version": "plugin_package.v9",
            "workbench_url": f"http://127.0.0.1:{port}/",
            "host_api_url": os.environ.get("MY_AGENT_HOST_API_URL", ""),
            "data_dir": os.environ.get("MY_AGENT_PLUGIN_DATA_DIR", ""),
            "status": "ready",
            "http_status": "not_checked",
        }
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload, ensure_ascii=False),
                }
            ]
        }
    if method == "ping":
        return {}
    raise RuntimeError(f"unsupported MCP method: {method}")


# LLM: Read MCP stdio; only explicit --serve-http starts the standalone backend with its own argv.
# 函数用途: 启动 MCP 主循环或显式 HTTP 后端，在插件私有目录记录启动标记。
def main() -> int:
    data_dir = os.environ.get("MY_AGENT_PLUGIN_DATA_DIR")
    if data_dir:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        Path(data_dir, "started").write_text("1", encoding="utf-8")
    if "--serve-http" in sys.argv:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from workbench_server import main as server_main

        return server_main([arg for arg in sys.argv[1:] if arg not in {"--serve-http", "--stdio"}])
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        request = json.loads(line)
        if "id" not in request:
            continue
        result = handle_rpc_request(request)
        response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""".replace(
    "__TOOLS_JSON__",
    repr(
        [
            {
                "name": TOOL_SPEC["name"],
                "description": TOOL_SPEC["description"],
                "inputSchema": TOOL_SPEC["input_schema"],
            }
        ]
    ),
)


# LLM: Build a self-contained source bundle against the selected framework; inspect bytes before atomic publication.
# 函数用途: 打包实际服务与本地模块，逐文件登记指纹，不安装或激活插件。
def build_workbench_plugin_zip(
    output_path: Path = DEFAULT_ZIP_PATH,
    *,
    plugin_id: str = "host-workbench",
    capabilities: list[str] | tuple[str, ...] | None = None,
    summary: str = "My-Agent Host v2 全量契约与多用户权限验证工作台插件",
) -> tuple[Path, PluginPackageSnapshot]:
    chosen_capabilities = list(capabilities) if capabilities is not None else list(host_capabilities())
    action = CommandActionSpec(
        "open_workbench",
        "启动并返回 Host v2 契约验证工作台地址",
        (ArgumentSpec("port", "监听端口", required=False),),
        kind="tool",
        target="workbench_status",
    )
    script_bytes = PLUGIN_MAIN_SCRIPT.replace('"host-workbench"', json.dumps(plugin_id)).encode("utf-8")
    script_sha256 = hashlib.sha256(script_bytes).hexdigest()

    metadata_bytes = json.dumps(
        {
            "plugin_id": plugin_id,
            "version": "1.0.0",
            "capabilities": chosen_capabilities,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    metadata_sha256 = hashlib.sha256(metadata_bytes).hexdigest()

    manifest_dict = {
        "schema_version": "plugin_package.v9",
        "plugin_id": plugin_id,
        "version": "1.0.0",
        "summary": summary,
        "entry": {
            "kind": "executable",
            "command": "bin/plugin_main.py",
            "args": ["--stdio"],
        },
        "files": [
            {
                "path": "bin/plugin_main.py",
                "sha256": script_sha256,
                "executable": True,
            },
            {
                "path": "assets/workbench_meta.json",
                "sha256": metadata_sha256,
                "executable": False,
            },
        ],
        "platforms": ["any"],
        "actions": [json.loads(json.dumps(asdict(action)))],
        "default_action": "open_workbench",
        "tools": [TOOL_SPEC],
        "settings_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "panels": [],
        "skills": [],
        "host_api": chosen_capabilities,
    }

    bundled = {
        p.name: p.read_bytes()
        for p in sorted(PLUGIN_DIR.glob("*.py"))
        if p.is_file() and not p.is_symlink() and not p.name.startswith("test_")
    }
    if "workbench_server.py" not in bundled:
        raise ValueError("missing workbench_server.py")
    manifest_dict["files"].extend({
        "path": name, "sha256": hashlib.sha256(content).hexdigest(), "executable": False,
    } for name, content in bundled.items())
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugin.json", json.dumps(manifest_dict, ensure_ascii=False, indent=2).encode("utf-8"))
        info_exec = zipfile.ZipInfo("bin/plugin_main.py")
        info_exec.external_attr = 0o755 << 16
        info_exec.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info_exec, script_bytes)
        archive.writestr("assets/workbench_meta.json", metadata_bytes)
        for name, content in bundled.items():
            archive.writestr(name, content)

    zip_bytes = buffer.getvalue()
    snapshot = inspect_plugin_package(zip_bytes)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = output_path.with_suffix(".zip.tmp")
    staging.write_bytes(zip_bytes)
    staging.replace(output_path)
    return output_path, snapshot


# LLM: Publish only the explicitly authorized A3 dist artifact, with the inspector receipt; never install it.
# 函数用途: 命令行重建 SecOps 插件 ZIP 并报告真实校验指纹。
def main() -> int:
    path, snapshot = build_workbench_plugin_zip(
        SECOPS_ZIP_PATH, plugin_id="my-agent-web-portal", capabilities=list(host_capabilities()),
        summary="SecOps Portal: persistent API engines with optional generic Host bridges",
    )
    print(json.dumps({"path": str(path), "sha256": snapshot.sha256,
                      "plugin_id": snapshot.manifest.plugin_id, "inspection": "passed"}))
    return 0


# LLM: Keep legacy packaging callable explicitly, but do not regenerate unrelated artifacts in A3.
# 函数用途: 保留旧版三个演示包的显式构建函数。
def build_legacy_packages() -> int:
    pub_path, pub_snap = build_workbench_plugin_zip(
        DEFAULT_ZIP_PATH,
        plugin_id="host-workbench",
        capabilities=list(host_capabilities()),
        summary="My-Agent Host v2 全量契约与多用户权限验证工作台插件（管理员公共版）",
    )
    private_caps = [cap for cap in host_capabilities() if cap not in {"users.manage", "act_as_user"}]
    priv_path, priv_snap = build_workbench_plugin_zip(
        PRIVATE_ZIP_PATH,
        plugin_id="user-private-workbench",
        capabilities=private_caps,
        summary="My-Agent Host v2 普通用户私有工作台插件（不含跨用户与账号治理能力）",
    )
    pentest_path, pentest_snap = build_workbench_plugin_zip(
        PENTEST_ZIP_PATH,
        plugin_id="web-pentest-portal",
        capabilities=list(host_capabilities()),
        summary="REDOPS // Web 渗透测试控制台插件（管理员专属准入 · 独立端口 18431）",
    )
    print(
        json.dumps(
            {
                "public_package": {
                    "path": str(pub_path),
                    "plugin_id": pub_snap.manifest.plugin_id,
                    "schema_version": pub_snap.manifest.to_payload()["schema_version"],
                    "host_api_v9": pub_snap.manifest.host_api_v9,
                    "sha256": pub_snap.sha256,
                    "host_api_count": len(pub_snap.manifest.host_api),
                    "host_api": list(pub_snap.manifest.host_api),
                },
                "private_package": {
                    "path": str(priv_path),
                    "plugin_id": priv_snap.manifest.plugin_id,
                    "schema_version": priv_snap.manifest.to_payload()["schema_version"],
                    "host_api_v9": priv_snap.manifest.host_api_v9,
                    "sha256": priv_snap.sha256,
                    "host_api_count": len(priv_snap.manifest.host_api),
                    "host_api": list(priv_snap.manifest.host_api),
                },
                "pentest_package": {
                    "path": str(pentest_path),
                    "plugin_id": pentest_snap.manifest.plugin_id,
                    "schema_version": pentest_snap.manifest.to_payload()["schema_version"],
                    "host_api_v9": pentest_snap.manifest.host_api_v9,
                    "sha256": pentest_snap.sha256,
                    "host_api_count": len(pentest_snap.manifest.host_api),
                    "host_api": list(pentest_snap.manifest.host_api),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
