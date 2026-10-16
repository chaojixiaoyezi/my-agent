# LLM: 宿主单次CLI运输只接准确官方路径；认证经stdin，目录/日志/模型不接触登录缓存。
# 模块用途: 将已授权云操作交给随包官方CLI扩展后端，保留原HTTP和未知写入事实。
from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

PROTOCOL = "my-agent.feishu-cli.v1"
# 飞书CLI标准输出最多接收的字节数，超过预算不解析为成功结果。
MAX_BYTES = 4 * 1024 * 1024


# LLM: 仅用于当前调用的本人身份，原权限证明和保护读取不能误用共享应用token；不进入模型或持久账。
# 类用途: 为既有云权限接口提供宿主已验证的本人凭据，不改动原适配器登录状态。
@dataclass(frozen=True)
class UserCloudIdentity:
    app_id: str
    app_secret: str = field(repr=False)
    token: str = field(repr=False)
    actor_id: str

    # LLM: 原HTTP权限查询使用这个宿主凭据；不得返回到工具输出、调用参数或repr。
    # 函数用途: 在本人模式下使元数据与正式业务操作使用同一身份。
    def _get_tenant_access_token(self):
        return self.token


# LLM: 固定随包文件不查PATH、工作区或用户指定程序；无后端时明确报未安装。
# 函数用途: 查找当前系统的受控CLI。
def cli_binary() -> Path:
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(platform.machine().lower(), platform.machine().lower())
    name = f"feishu-cli-host.{platform.system().lower()}-{machine}"
    path = Path(__file__).resolve().parents[2] / "vendor" / "bin" / name
    if not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK):
        raise FileNotFoundError("随包飞书 CLI 后端未安装。")
    return path


# LLM: 清理只针对本次新建子进程组；不能按名称或PID列表杀其它任务。
# 函数用途: 在超时/取消后确保CLI子进程退出。
def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


# LLM: auth只在私有stdin和进程内；独立临时HOME/config阻止共享登录缓存，stderr不进入异常/工具记录。
# 函数用途: 启动一次有界CLI请求，返回原始运输协议。
def execute_cli_request(payload: dict, *, cancellation_token=None, timeout: float = 25) -> dict:
    binary = cli_binary()
    with tempfile.TemporaryDirectory(prefix="my-agent-feishu-cli-") as directory:
        env = {"PATH": os.defpath, "HOME": directory, "LARKSUITE_CLI_CONFIG_DIR": directory,
               "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=env, cwd=directory,
                                   start_new_session=os.name == "posix")
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        deadline = time.monotonic() + timeout
        try:
            while True:
                from ..concurrency.interrupt import is_interrupted
                if is_interrupted():
                    raise InterruptedError("飞书CLI调用已取消。")
                if cancellation_token is not None and bool(getattr(cancellation_token, "cancelled", False)):
                    raise InterruptedError("飞书CLI调用已取消。")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("飞书CLI调用超时。")
                try:
                    stdout, _ = process.communicate(input=data, timeout=min(0.2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    data = None
            if process.returncode or len(stdout) > MAX_BYTES:
                raise ValueError("飞书CLI未返回可验证结果。")
            result = json.loads(stdout)
            if not isinstance(result, dict) or result.get("schema") != PROTOCOL:
                raise ValueError("飞书CLI返回协议无效。")
            return result
        finally:
            _stop_process(process)


# LLM: token只由宿主读取；先通过owner/群/资源门，HTTP/API状态和明确拒绝交原共享分类器，未知写入仍不重放。
# 函数用途: 通过官方CLI后端执行固定云请求，保持未知写入不能重放。
def request_cli_cloud_api(adapter, method: str, path: str, body=None, *, read_only=False, cancellation_token=None, user_token=""):
    from ..common.log_redaction import redact_sensitive_value
    from .feishu_cloud import (
        FeishuCloudError,
        _classified_error,
        _clean_response,
        _provider_rejection_known,
        _public_identifiers,
    )

    token = user_token or adapter._get_tenant_access_token()
    if not token:
        raise FeishuCloudError("FEISHU_CREDENTIAL_UNAVAILABLE", "宿主飞书应用凭据不可用。")
    mutating = method != "GET" and not read_only
    actor_id = str(getattr(adapter, 'actor_id', '') or '') if user_token else ''
    if user_token and not actor_id:
        raise FeishuCloudError('FEISHU_USER_AUTH_REQUIRED', '本人飞书身份无法验证，未执行云操作。')
    payload = {"schema": PROTOCOL, "method": method, "path": path, "body": body,
               "app_id": str(getattr(adapter, "app_id", "") or ""), "token": token,
               "identity": "user" if user_token else "bot", "actor_id": actor_id}
    try:
        result = execute_cli_request(payload, cancellation_token=cancellation_token)
    except InterruptedError:
        raise
    except FileNotFoundError:
        raise FeishuCloudError("FEISHU_CLI_UNAVAILABLE", "随包飞书 CLI 后端未安装，请更新完整安装包。", reason="cli_not_installed") from None
    except (OSError, ValueError, TypeError, RecursionError):
        raise FeishuCloudError("FEISHU_CLOUD_RESPONSE_UNKNOWN", "飞书 CLI 未返回可验证结果；写操作请先读回核对。",
                               unknown=mutating, reason="cli_response_unknown") from None
    status = result.get("http_status")
    error = result.get("error_code")
    response = result.get("response")
    if error == 'identity_unverified' and result.get('request_sent') is False:
        raise FeishuCloudError('FEISHU_USER_AUTH_REQUIRED', '本人飞书token与账号无法验证，未执行云操作。')
    if type(status) is not int or type(result.get("request_sent")) is not bool:
        raise FeishuCloudError("FEISHU_CLOUD_RESPONSE_UNKNOWN", "飞书 CLI 返回协议无法验证。", unknown=mutating)
    code = response.get("code") if isinstance(response, dict) else None
    if status >= 300 and status < 400:
        raise _classified_error(status, None)
    if status >= 400:
        raise _classified_error(status, code if type(code) is int else None, unknown=mutating and (status >= 500 or status == 429) and not _provider_rejection_known(code, path))
    if error or not 200 <= status < 300 or not result["request_sent"] or not isinstance(response, dict) or type(code) is not int:
        raise FeishuCloudError("FEISHU_CLOUD_RESPONSE_UNKNOWN", "飞书 CLI 返回结果无法验证；写操作请先读回核对。",
                               unknown=mutating and result["request_sent"], reason="cli_response_invalid")
    if code != 0:
        raise _classified_error(status, code, unknown=mutating and not _provider_rejection_known(code, path))
    data = response.get("data")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise FeishuCloudError("FEISHU_CLOUD_RESPONSE_UNKNOWN", "飞书 CLI 响应载荷无法验证。", unknown=mutating)
    return _clean_response(redact_sensitive_value(_public_identifiers(data)),
                           (token, str(getattr(adapter, "app_secret", "") or "")))
