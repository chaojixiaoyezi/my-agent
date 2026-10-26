# LLM: 源码只经匿名stdin送给独立沙箱进程，不落caller可读文件/argv/env；无沙箱必须拒绝执行。
# 模块用途: 在调用者workspace执行冻结脚本并只接收有界JSON，不泄露stderr和源码。
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

from ..attempt.sandbox import (
    AttemptExecutionSandbox,
    AttemptSandboxSpec,
    system_read_roots_for_platform,
)
from ..common.cancellation import raise_if_cancelled
from ..plugin_sandbox import resolve_python_runtime_prefixes
from ..tooling.process_output_capture import ProcessOutputCapture
from ..workflows.script_runner import _cleanup_process
from .access_policy import AuditGroupError

_BOOTSTRAP = """import io,json,os,sys
_cfg=json.load(sys.stdin)
os.close(0)
sys.stdin=io.StringIO(json.dumps(_cfg['inputs']))
_code=compile(_cfg.pop('code'),'<audit-group-unit>','exec')
_scope={'__name__':'__main__','__file__':'<audit-group-unit>'}
del _cfg
exec(_code,_scope)
"""
MAX_RESULT_BYTES = 1024 * 1024


# LLM: 整个进程无文件写根，只读系统/解释器/caller工作区；结果仅经匿名管道交宿主审核发表。
# 函数用途: 构造强制allowlist平台沙箱，网络固定关闭。
def execution_sandbox(workspace, staging):
    roots = resolve_python_runtime_prefixes(executable=sys.executable, strict=False)
    return AttemptExecutionSandbox(AttemptSandboxSpec(attempt_view=workspace, staging_root=staging,
        shared_workspace=workspace, owner_home=workspace, network_access=False,
        read_only_root=True, read_mode="allowlist", implicit_attempt_write_roots=False,
        extra_write_roots=(), public_read_roots=(*roots, workspace, staging),
        system_read_roots=system_read_roots_for_platform()))


# LLM: 独立进程只看匿名代码流和明确inputs；不组装主头像Prompt、不加载source配置或凭据。
# 类用途: 提供脚本及结构化技能共用的宿主隔离执行单元。
class IsolatedScriptUnit:
    # LLM: 原生沙箱必须require_ready，异常不能降级到宿主执行；code只进匿名临时FD。
    # 函数用途: 在准确调用者目录启动一次有界脚本，返回JSON对象。
    def execute(self, code, inputs, *, workspace, staging, timeout_s):
        sandbox = execution_sandbox(workspace, staging)
        argv = sandbox.build_argv([sys.executable, "-I", "-B", "-c", _BOOTSTRAP])
        payload = json.dumps({"code": code, "inputs": inputs}, ensure_ascii=False, allow_nan=False).encode()
        environment = {"PATH": os.defpath, "HOME": str(workspace), "TMPDIR": str(staging),
                       "PYTHONIOENCODING": "utf-8", "MY_AGENT_SCRIPT_INPUT_JSON": json.dumps(inputs, ensure_ascii=False)}
        with tempfile.TemporaryFile(dir=staging) as stdin:
            stdin.write(payload)
            stdin.seek(0)
            process = subprocess.Popen(argv, cwd=workspace, env=environment, stdin=stdin,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            capture = None
            try:
                capture = ProcessOutputCapture(process, max_bytes=MAX_RESULT_BYTES)
                _wait(process, capture, timeout_s)
            finally:
                cleanup = _cleanup_process(process)
                if capture is not None:
                    capture.finish(1)
                if cleanup:
                    raise AuditGroupError("AUDIT_GROUP_EXECUTION_CLEANUP_UNVERIFIED", status_code=503)
            stdout, _stderr, facts = capture.result()
            if not facts["complete"] or facts["streams_invalid_utf8"]["stdout"]:
                raise AuditGroupError("AUDIT_GROUP_OUTPUT_LIMIT", status_code=409)
            try:
                result = json.loads(stdout)
                if type(result) is not dict:
                    raise ValueError()
                return result
            except (ValueError, UnicodeError, RecursionError):
                raise AuditGroupError("AUDIT_GROUP_INVALID_OUTPUT", status_code=409) from None


# LLM: 等待期间持续检查取消/输出预算，总期限使用单调钟；只回收本次创建的进程组。
# 函数用途: 等待脚本退出，超时/失败仅返回固定安全错误码。
def _wait(process, capture, timeout_s):
    deadline = time.monotonic() + timeout_s
    while process.poll() is None:
        raise_if_cancelled()
        if sum(capture.result()[2]["bytes_seen"].values()) > MAX_RESULT_BYTES:
            raise AuditGroupError("AUDIT_GROUP_OUTPUT_LIMIT", status_code=409)
        if time.monotonic() >= deadline:
            raise AuditGroupError("AUDIT_GROUP_TIMEOUT", status_code=408)
        time.sleep(0.01)
    if process.returncode:
        raise AuditGroupError("AUDIT_GROUP_EXECUTION_FAILED", status_code=409)
