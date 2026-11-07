"""测试专用 ACP v1 进程；只读 stdin，用显式协议消息控制交错，不访问网络。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

_lock = threading.Lock()
_pending = {}
_mode = 'ask'
_session = 'fake-session'


def send(message):
    with _lock:
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', **message}, ensure_ascii=False) + '\n')
        sys.stdout.flush()


def result(identifier, value):
    send({'id': identifier, 'result': value})


def update(kind, **values):
    send({'method': 'session/update', 'params': {'sessionId': _session,
        'update': {'sessionUpdate': kind, **values}}})


def main():
    global _mode
    if '--child' in sys.argv:
        subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if '--bad-initialize' in sys.argv:
        return 7
    for line in sys.stdin:
        msg = json.loads(line)
        method, params, identifier = msg.get('method'), msg.get('params', {}), msg.get('id')
        if method is None:
            turn = _pending.pop(identifier, None)
            if turn is not None:
                update('agent_message_chunk', content={'type': 'text', 'text': json.dumps(msg)})
                result(turn, {'stopReason': 'end_turn'})
            continue
        if method == 'initialize':
            result(identifier, {'protocolVersion': 1, 'agentCapabilities': {
                'loadSession': '--no-load' not in sys.argv}, 'agentInfo': {'name': 'fake', 'version': '1'}})
        elif method in ('session/new', 'session/load'):
            result(identifier, {'sessionId': _session, 'modes': {'currentModeId': _mode,
                'availableModes': [{'id': 'ask', 'name': 'Ask'}, {'id': 'code', 'name': 'Code'}]}})
        elif method == 'session/set_mode':
            _mode = params['modeId']
            result(identifier, {})
        elif method == 'session/cancel':
            for key, turn in list(_pending.items()):
                _pending.pop(key)
                result(turn, {'stopReason': 'cancelled'})
        elif method == 'session/prompt':
            text = ''.join(block.get('text', '') for block in params['prompt'])
            if text == 'exit':
                os._exit(9)
            if text == 'permission':
                _pending['permission-rpc'] = identifier
                send({'id': 'permission-rpc', 'method': 'session/request_permission', 'params': {
                    'sessionId': _session, 'toolCall': {'toolCallId': 'tool-1', 'title': 'test'},
                    'options': [{'optionId': 'allow', 'name': 'Allow', 'kind': 'allow_once'},
                                {'optionId': 'deny', 'name': 'Deny', 'kind': 'reject_once'}]}})
            elif text == 'question':
                _pending['question-rpc'] = identifier
                send({'id': 'question-rpc', 'method': 'elicitation/create', 'params': {
                    'sessionId': _session, 'mode': 'form', 'message': 'Choose a label',
                    'requestedSchema': {'type': 'object', 'properties': {'answer': {'type': 'string'}},
                                        'required': ['answer']}}})
            elif text.startswith('request:'):
                request = json.loads(text.removeprefix('request:'))
                _pending['client-rpc'] = identifier
                send({'id': 'client-rpc', **request})
            elif text == 'wait':
                _pending['wait'] = identifier
                update('agent_message_chunk', content={'type': 'text', 'text': 'waiting'})
            elif text == 'config':
                update('current_mode_update', currentModeId=_mode)
                result(identifier, {'stopReason': 'end_turn'})
            elif text.startswith('burst:'):
                for index in range(int(text.split(':')[1])):
                    update('agent_message_chunk', content={'type': 'text', 'text': str(index)})
                result(identifier, {'stopReason': 'end_turn'})
            else:
                update('agent_message_chunk', content={'type': 'text', 'text': text})
                result(identifier, {'stopReason': 'end_turn'})
        elif method == 'reverse':
            _pending[identifier] = params
            if len(_pending) == 2:
                for key in reversed(list(_pending)):
                    result(key, _pending.pop(key))
        elif identifier is not None:
            send({'id': identifier, 'error': {'code': -32601, 'message': 'Unknown method'}})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
