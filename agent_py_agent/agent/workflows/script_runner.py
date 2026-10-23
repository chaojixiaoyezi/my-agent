# LLM: 仅执行宿主授权脚本的nofollow代码快照；严格模式强制平台沙箱，兼容模式可退回隔离cwd，不冒称内核隔离。
# 模块用途: 冻结并执行Owner脚本，清洗环境、生成脱敏修复上下文并回写运行摘要。
from __future__ import annotations

import inspect
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from ..assets.descriptor import access_target_requires_network
from ..assets.script_store import ScriptStore
from ..attempt.sandbox import AttemptExecutionSandbox, AttemptSandboxSpec, SandboxUnavailableError
from ..common.log_redaction import redact_sensitive_value
from ..common.nofollow_fs import ensure_private_dir, read_text_beneath, write_text_atomic_beneath
from ..plugin_sandbox import resolve_python_runtime_prefixes
from ..tooling._subprocess_env_scrub import scrub_subprocess_env
from .models import ScriptExecutionResult, payload, safe_path

# 原生工作流脚本标准输出与错误流保留的最大字节数。
MAX_OUTPUT_BYTES = 1024 * 1024
# 原生工作流步骤回执保留的输出预览最大字符数。
PREVIEW_CHARS = 4096
_JSON_OUTPUT_TYPES = {
    'string': (str,), 'integer': (int,), 'number': (int, float),
    'boolean': (bool,), 'object': (dict,), 'array': (list,), 'null': (type(None),),
}


# LLM: 此异常只表示冻结代码为空或缺失，不把路径违规或坏描述符降级为旧格式。
# 类用途: 让缺失入口返回明确的可修复错误码。
class _EntrypointMissing(ValueError):
    pass


# LLM: 权威入口先走Store恢复及nofollow；仅None可读旧meta.json，旧Store的明确缺代码错误只分类、不绕过验证。
# 函数用途: 取得脚本配置和冻结代码，返回目录仅供兼容调用方展示。
def load_script(owner_home, script_id):
    folder = safe_path(owner_home, 'scripts', script_id)
    try:
        asset = ScriptStore(owner_home).get_script(script_id)
    except ValueError as exc:
        if str(exc) == '脚本代码文件缺失':
            raise _EntrypointMissing(str(exc)) from exc
        raise
    if asset is None:
        raw = read_text_beneath(owner_home, ('scripts', script_id, 'meta.json'))
        if raw is None:
            raise _EntrypointMissing('脚本不存在')
        data = json.loads(raw)
        safe_path(folder, data['entrypoint'])
        data['code'] = read_text_beneath(owner_home, ('scripts', script_id, data['entrypoint']))
    else:
        data = payload(asset)
    metadata = {**data.get('descriptor', {}), **data}
    if not metadata.get('code'):
        raise _EntrypointMissing('脚本入口为空或缺失')
    return folder, metadata


# LLM: 仅识别macOS外层Seatbelt已生效时嵌套sandbox_apply返回71的精确内核事实；其它错误一律按未就绪失败关闭。
# 函数用途: 判断当前进程是否已处于禁止再次sandbox_apply的外层macOS Seatbelt沙箱内。
@lru_cache(maxsize=1)
def _outer_seatbelt_active():
    if sys.platform != 'darwin':
        return False
    try:
        res = subprocess.run([shutil.which('sandbox-exec') or '/usr/bin/sandbox-exec', '-p',
                              '(version 1)(allow default)', '/usr/bin/true'],
                             capture_output=True, timeout=5, check=False)
        return res.returncode == 71 and b'sandbox_apply: Operation not permitted' in (res.stderr or b'')
    except (OSError, subprocess.SubprocessError):
        return False


# LLM: 启用沙箱时必须先通过require_ready（未就绪直接抛SandboxUnavailableError失败关闭）；仅外层Seatbelt已锁死71且非strict时复用外层隔离。
# 函数用途: 校验平台沙箱就绪并决定是否需要再套一层子进程沙箱包装。
def _can_apply_sandbox(sandbox: AttemptExecutionSandbox, *, strict: bool = False) -> bool:
    if not strict and _outer_seatbelt_active():
        return False
    sandbox.require_ready()
    return True


# LLM: 分类不授予资源或凭据，显式覆盖及空声明既有行为不变。
# 函数用途: 按共享目标type或旧kind推导网络开关。
def _network_access(metadata, override):
    if override is not None:
        if type(override) is not bool:
            raise ValueError('network_access必须是布尔值')
        return override
    targets = metadata.get('access_targets', ())
    return not targets or any(access_target_requires_network(target) for target in targets)


# LLM: 从宿主副本清洗凭据，仅添加引用占位符；工作目录、HOME与TMPDIR均指向本次冻结目录。
# 函数用途: 准备脚本输入和不携带宿主密钥的环境。
def _environment(metadata, folder, input_json, sources):
    environment = scrub_subprocess_env(dict(os.environ))
    environment.update(PYTHONIOENCODING='utf-8', PAGER='cat', GIT_PAGER='cat', HOME=str(folder), TMPDIR=str(folder),
                       MY_AGENT_SCRIPT_INPUT_JSON=input_json,
                       MY_AGENT_DATA_SOURCES_JSON=json.dumps(sources, ensure_ascii=False, allow_nan=False))
    for ref in metadata.get('credential_refs', ()):
        if ref.get('credential_id') and ref.get('target'):
            environment['CREDENTIAL_REF_' + ref['credential_id'].upper()] = ref['target']
    return environment


# LLM: 摘要失败不能覆盖执行事实；按真实签名兼容并行Store的run_at和新增last_error，不重试已经进入的写操作。
# 函数用途: 尽力将成功、失败及超时累计到脚本摘要。
def _record_summary(owner_home, result):
    try:
        record = ScriptStore(owner_home).record_run_summary
        parameters = inspect.signature(record).parameters
        status = 'succeeded' if result.ok else 'timed_out' if result.error_code == 'SCRIPT_TIMEOUT' else 'failed'
        options = {'status': status, 'duration_ms': result.duration_ms}
        if 'run_at' in parameters:
            options['run_at'] = datetime.fromtimestamp(result.ended_at, timezone.utc).isoformat()
        if 'last_error' in parameters or any(p.kind == p.VAR_KEYWORD for p in parameters.values()):
            options['last_error'] = redact_sensitive_value(result.error_code or result.stderr_preview[:200])
        record(result.script_id, **options)
    except Exception:
        pass


# LLM: 只能由本模块处理其亲自创建的进程组，退出后也清理仍留存的同组子进程。
# 函数用途: 终止脚本进程树并回收直接子进程，避免超时后留下后台脚本。
def _terminate(process):
    try:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass
    process.wait()


# LLM: 清理拒绝不授予任何新信号权限；只尝试有限回收并保留原异常及进程组未验证事实。
# 函数用途: 清理自己创建的子进程，返回明确的次级错误而不是覆盖业务失败。
def _cleanup_process(process):
    try:
        _terminate(process)
    except OSError as exc:
        try:
            process.wait(timeout=0.1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return {'error_code': 'SCRIPT_CLEANUP_ERROR', 'exception_type': type(exc).__name__,
                'errno': exc.errno, 'message': str(exc), 'leader_exited': process.returncode is not None,
                'group_cleanup_verified': False}
    return {}


# LLM: 原始失败及清理事实写入调用方持有的 capture，输出读取或关闭异常也不能丢失硬失败。
# 函数用途: 真正启动脚本并返回退出码、输出及超时或输出超限标记。
def _execute(command, folder, environment, input_json, timeout, capture):
    with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        stdin.write(input_json.encode('utf-8'))
        stdin.seek(0)
        process = subprocess.Popen(command, cwd=folder, env=environment, stdin=stdin,
                                   stdout=stdout, stderr=stderr, start_new_session=os.name == 'posix')
        deadline, error = time.monotonic() + timeout, ''
        try:
            while process.poll() is None:
                if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > MAX_OUTPUT_BYTES:
                    error = 'SCRIPT_OUTPUT_LIMIT'
                    break
                if time.monotonic() >= deadline:
                    error = 'SCRIPT_TIMEOUT'
                    break
                time.sleep(0.01)
        finally:
            capture['error_code'] = error
            cleanup = _cleanup_process(process)
            capture.update(cleanup_error=cleanup, exit_code=process.returncode)
        if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > MAX_OUTPUT_BYTES:
            error = error or 'SCRIPT_OUTPUT_LIMIT'
        capture['error_code'] = error
        stdout.seek(0)
        stderr.seek(0)
        return process.returncode, stdout.read(MAX_OUTPUT_BYTES).decode('utf-8', 'replace'), stderr.read(PREVIEW_CHARS * 4).decode('utf-8', 'replace'), error, cleanup


# LLM: 解析前限制 JSON 结构深度，忽略字符串中的括号；不依赖解释器不稳定的 C 栈上限。
# 函数用途: 拒绝过深的脚本输出，避免解析与序列化递归失控。
def _check_json_depth(text):
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            previously_escaped = escaped
            escaped = not escaped and char == '\\'
            quoted = previously_escaped or char != '"'
            continue
        if char == '"':
            quoted = True
            continue
        depth += int(char in '[{') - int(char in ']}')
        if depth > 128:
            raise ValueError('JSON 输出结构过深')


# LLM: 只接受JSON类型名，可空与字段存在性分开处理，不隐式增加必选字段。
# 函数用途: 校验输出声明并返回必选字段、可空字段和类型表。
def _output_contract_fields(contract):
    if not isinstance(contract, dict):
        raise ValueError('输出合同必须为对象')
    expected = contract.get('expected_output_keys', contract.get('outputs', ()))
    nullable = contract.get('nullable_outputs', ())
    output_types = contract.get('output_types', {})
    if not isinstance(expected, (list, tuple)) or not all(isinstance(key, str) for key in expected):
        raise ValueError('必选输出字段必须为字符串列表')
    if not isinstance(nullable, (list, tuple)) or not all(isinstance(key, str) for key in nullable):
        raise ValueError('可空输出字段必须为字符串列表')
    if not isinstance(output_types, dict) or any(
        not isinstance(key, str) or not isinstance(name, str) or name not in _JSON_OUTPUT_TYPES
        for key, name in output_types.items()
    ):
        raise ValueError('输出类型必须为字段名到JSON类型名的对象')
    return expected, nullable, output_types


# LLM: 正常与恢复输出共用严格校验；bool不是数字，显式nullable只豁免null值。
# 函数用途: 解析标准输出并核对字段存在性、非空和可选类型。
def parse_script_output(stdout, contract):
    try:
        _check_json_depth(stdout)
        output = payload(json.loads(stdout))
        if not isinstance(output, dict):
            return {}, 'SCRIPT_INVALID_JSON'
    except (ValueError, TypeError, RecursionError):
        return {}, 'SCRIPT_INVALID_JSON'
    expected, nullable, output_types = _output_contract_fields(contract)
    if any(key not in output for key in expected):
        return output, 'SCRIPT_MISSING_OUTPUT_KEYS'
    if any(output[key] is None and key not in nullable for key in expected):
        return output, 'SCRIPT_NULL_OUTPUT_VALUE'
    for key, name in output_types.items():
        if key not in output or output[key] is None and key in nullable:
            continue
        if type(output[key]) not in _JSON_OUTPUT_TYPES[name]:
            return output, 'SCRIPT_OUTPUT_TYPE_MISMATCH'
    return output, ''


# LLM: 共享前缀保留别名与realpath；runner跳过缺失目录，额外请求其原有解释器安装根。
# 函数用途: 选择venv或便携Python启动路径及只读根，不改沙箱策略。
def _interpreter_runtime(interpreter: str) -> tuple[str | None, tuple[Path, ...]]:
    if interpreter != 'python3':
        return shutil.which(interpreter, path=os.defpath), ()
    if not sys.executable:
        return None, ()
    resolved_exec = Path(sys.executable).expanduser().resolve(strict=False)
    prefix_dir = Path(sys.prefix).expanduser().resolve(strict=False)
    roots = resolve_python_runtime_prefixes(executable=sys.executable, strict=False)
    candidate = prefix_dir / 'bin' / Path(sys.executable).name
    executable = str(resolved_exec)
    if candidate.is_file() and not candidate.is_symlink():
        executable = str(candidate)
    elif candidate.is_symlink():
        target = Path(os.path.normpath(os.path.join(str(candidate.parent), os.readlink(candidate))))
        if any(target.is_relative_to(r) for r in roots):
            executable = str(candidate)
    return executable, roots


# LLM: 固化脚本仅接收 JSON；宿主必须在调用前完成 Owner、审批和工具权限检查。
# 类用途: 按 Owner 的脚本配置执行 Python、bash 或 sh，且不调用任何模型。
class ScriptRunner:
    # LLM: 初始化只绑定宿主选定目录与沙箱策略，不执行脚本；strict在启用沙箱时禁止退回。
    # 函数用途: 保存Owner路径和默认启用的沙箱选项。
    def __init__(
        self,
        owner_home: str | Path,
        *,
        use_sandbox: bool = True,
        strict_sandbox: bool = False,
        extra_write_roots: tuple[str | Path, ...] = (),
        public_read_roots: tuple[str | Path, ...] = (),
    ):
        self.owner_home = Path(owner_home).absolute()
        self.scripts_root = self.owner_home / 'scripts'
        self.use_sandbox, self.strict_sandbox = use_sandbox, strict_sandbox
        self.extra_write_roots = tuple(Path(p).expanduser().resolve(strict=False) for p in extra_write_roots)
        self.public_read_roots = tuple(Path(p).expanduser().resolve(strict=False) for p in public_read_roots)

    # LLM: 冻结文件只在独立临时目录执行；包括沙箱包装失败和中断在内的所有出口都清理，不执行原资产路径。
    # 函数用途: 冻结代码、构造环境与沙箱命令，并在执行结束后删除临时目录。
    @contextmanager
    def _prepare(self, script_id, inputs, timeout_seconds, override_data_sources, network_access):
        _, metadata = load_script(self.owner_home, script_id)
        interpreter = metadata.get('interpreter', 'python3')
        if interpreter not in ('python3', 'bash', 'sh'):
            raise ValueError('仅支持 python3、bash、sh')
        executable, runtime_read_roots = _interpreter_runtime(interpreter)
        if not executable:
            raise ValueError('解释器不可用')
        timeout = float(metadata.get('timeout_seconds', 30) if timeout_seconds is None else timeout_seconds)
        if not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise ValueError('脚本超时必须为 (0, 3600] 秒')
        sources = payload(metadata.get('data_sources', {}) if override_data_sources is None else override_data_sources)
        input_json = json.dumps(payload(inputs), ensure_ascii=False, allow_nan=False)
        root = self.owner_home / 'data' / 'script_exec'
        ensure_private_dir(root, canonical_root=self.owner_home)
        folder = Path(tempfile.mkdtemp(prefix=f'{script_id}_', dir=root))
        try:
            entrypoint = safe_path(folder, metadata.get('entrypoint', 'main.py'))
            write_text_atomic_beneath(self.owner_home, (*folder.relative_to(self.owner_home).parts, entrypoint.name),
                                      metadata['code'], file_mode=0o700)
            command = [executable, '-I', '-B', str(entrypoint)] if interpreter == 'python3' else [executable, str(entrypoint)]
            if self.use_sandbox:
                sandbox = AttemptExecutionSandbox(AttemptSandboxSpec(
                    attempt_view=folder, staging_root=folder, shared_workspace=self.owner_home, owner_home=self.owner_home,
                    extra_write_roots=(folder, *self.extra_write_roots),
                    public_read_roots=tuple(dict.fromkeys((*runtime_read_roots, *self.public_read_roots))),
                    implicit_attempt_write_roots=True,
                    network_access=_network_access(metadata, network_access)))
                if _can_apply_sandbox(sandbox, strict=self.strict_sandbox):
                    command = sandbox.build_argv(command)
            yield metadata, sources, (command, str(folder), _environment(metadata, folder, input_json, sources), input_json, timeout)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    # LLM: 单次覆盖不写资产；业务错误和清理拒绝分别保存，清理未验证时绝不返回 ok=True。
    # 函数用途: 执行一次脚本，精确记录时间并输出结构化成功或失败回执。
    def run_script(self, script_id, inputs=None, *, timeout_seconds: int | float | None = None,
                   override_data_sources=None, network_access: bool | None = None):
        started_at, timer = time.time(), time.monotonic()
        metadata, sources, output, stdout, stderr, error, exit_code = {}, {}, {}, '', '', '', None
        cleanup, capture, execution_error = {}, {}, {}
        try:
            inputs = payload({} if inputs is None else inputs)
            with self._prepare(script_id, inputs, timeout_seconds, override_data_sources, network_access) as prepared:
                metadata, sources, execution = prepared
                exit_code, stdout, stderr, error, cleanup = _execute(*execution, capture)
            if cleanup:
                if exit_code is None and not error:
                    error = 'SCRIPT_CLEANUP_ERROR'
            if not error and exit_code != 0:
                error = 'SCRIPT_EXIT_NONZERO'
            if not error:
                output, error = parse_script_output(stdout, metadata.get('io_contract', {}))
            if cleanup:
                error = error or 'SCRIPT_CLEANUP_ERROR'
        except SandboxUnavailableError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            cleanup = capture.get('cleanup_error', cleanup)
            exit_code = capture.get('exit_code', exit_code)
            error = capture.get('error_code') or ('SCRIPT_CLEANUP_ERROR' if cleanup else 'SCRIPT_CONFIGURATION_ERROR')
            if isinstance(exc, _EntrypointMissing):
                error = 'ENTRYPOINT_MISSING'
            stderr = str(exc)
            execution_error = {'exception_type': type(exc).__name__, 'errno': getattr(exc, 'errno', None), 'message': str(exc)}
        if cleanup:
            stderr += '\n进程组清理失败: ' + cleanup['message']
        ended_at, elapsed = time.time(), (time.monotonic() - timer) * 1000
        context = {'script_id': script_id, 'error_code': error, 'exit_code': exit_code,
                   'stderr_preview': stderr[:PREVIEW_CHARS], 'stdout_tail': stdout[:PREVIEW_CHARS],
                   'stderr_tail': stderr[:PREVIEW_CHARS], 'input_payload': inputs, 'data_sources': sources, 'inputs': inputs,
                   'io_contract': metadata.get('io_contract', {})}
        if cleanup:
            context['cleanup_error'] = cleanup
        if execution_error:
            context['execution_error'] = execution_error
        result = ScriptExecutionResult(script_id, not error, exit_code, elapsed, started_at, ended_at,
                                       output, stdout[:PREVIEW_CHARS], stderr[:PREVIEW_CHARS], error,
                                       bool(error), redact_sensitive_value(context) if error else {})
        _record_summary(self.owner_home, result)
        return result
