"""ACP v1：复用 MCP 的有界写帧、响应箱和精确进程树回收，不复用 MCP 的握手。

协议来源：agentclientprotocol.com/protocol/v1/{initialization,transports,session-setup,
prompt-turn,tool-calls,file-system,session-modes,elicitation}.md。
请求可并行相关；服务端审批留作异步回包，不能堵住唯一读线程或取消通知。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from copy import deepcopy

from ..plugin_host.errors import HostAPIError
from ..tooling.mcp_protocol import MCPError
from ..tooling.mcp_transport import MCPTransport


def _reject_constant(value):
    raise ValueError('非有限 JSON 数值')


# LLM: 只接受完整RPC封套；布尔ID不能冒充数字请求，非法error不能在共享响应箱中退成空成功。
# 函数用途: 有界读取并校验UTF-8行帧，拒绝会混淆请求与响应归属的封套。
def read_frame(stream, maximum):
    """逐行读取 UTF-8 帧；无换行、超限或坏封套使连接失败，不积累未界定正文。"""
    raw = stream.readline(maximum + 1)
    if not raw:
        return None
    try:
        if len(raw) > maximum or not raw.endswith(b'\n'):
            raise ValueError('帧长度或边界错误')
        value = json.loads(raw.decode('utf-8'), parse_constant=_reject_constant)
        if type(value) is not dict or value.get('jsonrpc') != '2.0':
            raise ValueError('封套错误')
        if value.get('id') is not None and type(value['id']) not in (str, int):
            raise ValueError('请求编号类型错误')
        if 'method' in value:
            if (type(value['method']) is not str or not value['method']
                    or 'result' in value or 'error' in value):
                raise ValueError('请求封套错误')
        elif 'id' not in value or ('result' in value) == ('error' in value):
            raise ValueError('响应封套错误')
        if 'error' in value:
            error = value['error']
            if (type(error) is not dict or type(error.get('code')) is not int
                    or type(error.get('message')) is not str):
                raise ValueError('错误响应内容无效')
        return value
    except (ValueError, UnicodeError, TypeError) as exc:
        raise MCPError('ACP 协议帧无效', code='MCP_PROTOCOL_ERROR') from exc


def normalize_update(update):
    """只映射协议中的结构化更新类型，不把自然语言或未知扩展当作状态。"""
    if type(update) is not dict:
        return None
    kinds = {'agent_message_chunk': 'agent_message', 'agent_thought_chunk': 'agent_thought',
             'tool_call': 'tool_call', 'tool_call_update': 'tool_call_update', 'plan': 'plan',
             'current_mode_update': 'config_state', 'config_option_update': 'config_state'}
    kind = kinds.get(update.get('sessionUpdate'))
    return (kind, deepcopy(update)) if kind else None


class AcpTransport(MCPTransport):
    """一次启动一条连接；回调永远绑定原实例，晚到响应不进入新连接。"""

    def __init__(self, process, *, timeout, max_line_chars, on_update=None, on_request=None, on_exit=None):
        super().__init__(process, 'acp', max_line_chars=max_line_chars, connect_timeout=timeout)
        self.on_update, self.on_request, self.on_exit = on_update, on_request, on_exit
        self.reader = threading.Thread(target=self._read, name='acp-reader', daemon=True)
        # stderr 仅排空，不保留可能包含会话正文的诊断副本。
        self.stderr_reader = threading.Thread(target=self._drain, name='acp-stderr', daemon=True)
        self.agent_capabilities = {}

    @classmethod
    # LLM: 启动来源由部署配置冻结，三个回调属于本连接，不接收未声明的执行选项。
    # 函数用途: 用明确的更新、请求和退出回调启动一次ACP连接并保留原进程回收边界。
    def launch(cls, argv, *, cwd, environment, timeout, max_line_chars,
               on_update=None, on_request=None, on_exit=None):
        """argv/cwd/env 仅由宿主部署配置传入；启动独立组，接管失败也清理原实例。"""
        from ..tooling.process_registry import capture_process_birth_token

        # 在已知无法取得出生身份的宿主上先拒绝启动，不能先派生再留下不可回收的资源。
        if not capture_process_birth_token(os.getpid()):
            raise MCPError('宿主进程身份观察不可用，ACP 未启动', code='MCP_ADMISSION_UNAVAILABLE',
                           effect_outcome='not_started')
        process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=os.name != 'nt',
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
        transport = cls(process, timeout=timeout, max_line_chars=max_line_chars,
                        on_update=on_update, on_request=on_request, on_exit=on_exit)
        try:
            transport.start_readers()
            return transport
        except BaseException:
            transport.terminate(grace_seconds=0)
            raise

    def initialize(self):
        result = self.request('initialize', {'protocolVersion': 1,
            'clientCapabilities': {'fs': {'readTextFile': True, 'writeTextFile': True},
                'elicitation': {'form': {}}},
            'clientInfo': {'name': 'my-agent', 'version': '1'}}, timeout=self.connect_timeout)
        if (type(result) is not dict or type(result.get('protocolVersion')) is not int
                or result['protocolVersion'] != 1 or type(result.get('agentCapabilities', {})) is not dict):
            raise MCPError('ACP 版本或能力无效', code='MCP_PROTOCOL_ERROR')
        self.agent_capabilities = deepcopy(result.get('agentCapabilities', {}))
        self.ready = True
        return result

    def request(self, method, params, *, timeout, authority_check=None):
        """只在分配 ID 时持锁，允许取消及服务端回包穿过尚未完成的 prompt。"""
        deadline = time.monotonic() + max(0.01, timeout)
        with self.inbox.condition:
            req_id = self._next_id
            self._next_id += 1
            self.inbox.pending.add(req_id)
        try:
            self.send({'jsonrpc': '2.0', 'id': req_id, 'method': method, 'params': params},
                deadline=deadline, authority_check=authority_check)
            return self.inbox.await_response(req_id, method, deadline)
        finally:
            with self.inbox.condition:
                self.inbox.pending.discard(req_id)
                self.inbox.responses.pop(req_id, None)

    def reply(self, req_id, *, result=None, error=None):
        self.send({'jsonrpc': '2.0', 'id': req_id,
                   **({'error': error} if error is not None else {'result': result})})

    def _dispatch(self, message):
        method, req_id = message.get('method'), message.get('id')
        if type(method) is not str:
            self.inbox.dispatch(message)
            return
        params = message.get('params', {})
        if req_id is None:
            if method == 'session/update' and type(params) is dict and self.on_update is not None:
                self.on_update(params)
            return
        if type(req_id) not in (str, int):
            return
        if type(params) is not dict:
            self.reply(req_id, error={'code': -32602, 'message': '参数无效'})
            return
        try:
            if self.on_request is None:
                raise LookupError
            result = self.on_request(method, params, req_id)
            # None 仅表示已登记、稍后回答；JSON 的空结果必须显式传 {}。
            if result is not None:
                self.reply(req_id, result=result)
        except LookupError:
            self.reply(req_id, error={'code': -32601, 'message': '未支持的客户端方法'})
        except (ValueError, OSError, KeyError, TypeError, HostAPIError):
            self.reply(req_id, error={'code': -32602, 'message': '参数或工作区访问无效'})

    def _read(self):
        stream = self.binding.process.stdout
        try:
            while not self.inbox.closed.is_set():
                message = read_frame(stream, self.max_line_chars)
                if message is None:
                    break
                self._dispatch(message)
        except (MCPError, OSError, ValueError, TypeError):
            pass
        finally:
            self.inbox.close()
            if stream is not None:
                stream.close()
            if self.on_exit is not None:
                self.on_exit()

    def _drain(self):
        stream = self.binding.process.stderr
        if stream is not None:
            try:
                while stream.read(4096):
                    pass
            finally:
                stream.close()
