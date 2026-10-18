# LLM: 测正式parser.main到原dispatcher，不以直接调用dispatcher代替CLI接线；缺原C模式必须在任何配置/扩展/Home副作用前拒绝。
# 模块用途: 防止真实worker被普通命令解析器拒绝，并保普通Count0的原轻量/管理入口。
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_py_agent.cli import parser


# LLM: 启动参数仅定位本测试不存在的根；没有原C环境时不应触发配置、目录或实际W启动。
# 函数用途: 构造可被原内部parser识别、但不能自行获得运行许可的worker参数。
def worker_arguments(tmp_path):
    return ['--config', str(tmp_path / 'missing.yaml'), 'gateway', 'worker',
            '--index', '0', '--of', '2', '--run', 'parser-entry-test',
            '--gateway-root', str(tmp_path / 'gateway'), '--owner-root', str(tmp_path / 'owner'),
            '--parent-pid', str(os.getpid())]


@pytest.mark.parametrize('environment', [None, 'not-json'])
def test_real_parser_rejects_missing_original_worker_mode_before_config(
    tmp_path, monkeypatch, capsys, environment,
):
    from agent_py_agent.agent import extensions, settings

    monkeypatch.delenv('MY_AGENT_GATEWAY_WORKER', raising=False)
    if environment is not None:
        monkeypatch.setenv('MY_AGENT_GATEWAY_WORKER', environment)

    def forbidden(*_args, **_kwargs):
        raise AssertionError('worker必须先于普通配置/扩展/parser')

    monkeypatch.setattr(settings, 'load_config', forbidden)
    monkeypatch.setattr(extensions, 'load_extension_registry', forbidden)
    monkeypatch.setattr(parser, 'build_parser', forbidden)
    assert parser.main(worker_arguments(tmp_path)) == 2
    error = json.loads(capsys.readouterr().err)
    expected = ('WORKER_PLATFORM_UNSUPPORTED' if sys.platform == 'win32'
                else 'WORKER_MODE_REQUIRED' if environment is None else 'WORKER_MODE_INVALID')
    assert error == {'ok': False, 'error_code': expected}
    assert list(tmp_path.iterdir()) == []


def test_python_module_worker_entry_reaches_original_mode_gate_without_config(tmp_path):
    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment.pop('MY_AGENT_GATEWAY_WORKER', None)
    environment.update(HOME=str(tmp_path / 'home'), MY_AGENT_HOME=str(tmp_path / 'owner'),
                       PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-B', '-m', 'agent_py_agent', *worker_arguments(tmp_path)],
                            env=environment, cwd=root, capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    expected = 'WORKER_PLATFORM_UNSUPPORTED' if sys.platform == 'win32' else 'WORKER_MODE_REQUIRED'
    assert json.loads(result.stderr) == {'ok': False, 'error_code': expected}
    assert result.stdout == '' and list(tmp_path.iterdir()) == []


def test_ordinary_fast_chat_still_uses_original_dispatch(monkeypatch):
    from agent_py_agent.cli import chat_command_parser

    calls = []
    monkeypatch.setattr(chat_command_parser, '_cmd_chat', lambda args: calls.append(args) or 7)
    assert parser.main(['--plain']) == 7
    assert len(calls) == 1 and calls[0].command == 'chat' and calls[0].plain
