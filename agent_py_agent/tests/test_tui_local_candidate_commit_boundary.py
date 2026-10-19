"""本地未提交raw候选不可公开，Gateway公开事件和sealed-call正文沿原入口显示。"""
import pytest

from agent_py_agent.agent.conversation.channels import project_user_reply
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary

_SECRET = 'synthetic-only-secret-rev02'
_BODIES = [
    '{"password":"' + _SECRET + '"}',
    "{'db_password':'" + _SECRET + "'}",
    'https://user:' + _SECRET + '@example.invalid/',
    'PASSWORD="' + _SECRET + '"',
    'Authorization: Bearer ' + _SECRET,
    '-----BEGIN PRIVATE KEY-----\n' + _SECRET + '\n-----END PRIVATE KEY-----',
    'sk-' + _SECRET,
]


# LLM: 仅查看真实内存store，不重建影子投影，逐帧检查公开块。
# 函数用途: 合并当前active和stable助手正文，确保后续校正不能掩盖旧帧泄漏。
def _visible(runtime):
    snapshot = runtime.store.snapshot()
    return ''.join(row.text for row in (*snapshot.stable_blocks, *snapshot.active_blocks)
                   if row.role == 'assistant')


@pytest.mark.parametrize('body', _BODIES, ids=['json', 'single_json', 'url', 'assignment', 'authorization', 'multiline_key', 'known_key'])
@pytest.mark.parametrize('width', [1, 3, 13])
def test_local_candidate_all_frames_wait_for_original_public_summary(body, width):
    runtime = TuiRuntime('local-prefix-rev02')
    turn = runtime.begin_turn('local-prefix-rev02')
    for index in range(0, len(body), width):
        turn.write_model(body[index:index + width])
        assert _visible(runtime) == ''
    assert turn._output_chars == len(body)
    assert turn._output_bytes == len(body.encode('utf-8'))
    runtime.complete_turn('local-prefix-rev02', TuiTurnSummary(response_text=body))
    assert _visible(runtime) == project_user_reply(body).content
    assert _SECRET not in _visible(runtime)
    assert not runtime.store.snapshot().active_blocks


def test_local_sealed_tool_commentary_is_visible_immediately_and_only_once():
    runtime = TuiRuntime('local-commit-rev02')
    turn = runtime.begin_turn('local-commit-rev02')
    turn.write_model('先看现有目录。')
    assert _visible(runtime) == ''
    payload = {'schema': 'assistant_messages.v1', 'source': {'call_id': 'sealed-tools-rev02'},
               'boundary': 'tools', 'messages': [{'text': '先看现有目录。'}], 'final_text': ''}
    turn.commit_model_response(payload)
    turn.commit_model_response(payload)
    assert _visible(runtime) == '先看现有目录。'
    turn.write_model('已经完成。')
    assert _visible(runtime) == '先看现有目录。'
    runtime.complete_turn('local-commit-rev02', TuiTurnSummary(response_text='已经完成。'))
    assert _visible(runtime) == '先看现有目录。已经完成。'
    assert not runtime.store.snapshot().active_blocks


def test_gateway_typed_public_delta_still_displays_before_its_terminal_summary():
    runtime = TuiRuntime('gateway-public-rev02')
    turn = runtime.begin_turn('gateway-public-rev02')
    turn.on_gateway_event({'kind': 'model_delta', 'text': '宿主公开正文。'})
    assert _visible(runtime) == ''
    turn.commit_model_response({'schema': 'assistant_messages.v1', 'source': {'call_id': 'gateway-accepted'},
        'boundary': 'terminal', 'messages': [{'text': '宿主公开正文。'}], 'final_text': '宿主公开正文。'})
    assert _visible(runtime) == '宿主公开正文。'
    runtime.complete_turn('gateway-public-rev02', TuiTurnSummary(response_text='宿主公开正文。'))
    assert _visible(runtime) == '宿主公开正文。'
    assert not runtime.store.snapshot().active_blocks


@pytest.mark.parametrize('stream', [False, True])
def test_real_local_fake_loop_shows_each_sealed_tool_response_before_next_call(tmp_path, stream):
    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.tests.test_cli_run_conversation import _agent, _StaticBackend

    agent = _agent(tmp_path)
    path = agent.home_paths.owner_home_dir / 'source.txt'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('原工具事实', encoding='utf-8')
    runtime = TuiRuntime('local-real-loop-rev02')
    turn = runtime.begin_turn('local-real-loop-rev02')

    # LLM: 后端只按固定三次调用回放；读取真实store检查上一sealed call已展示，不发网络或替产品执行工具。
    # 类用途: 用原core工具循环验证流式与非流式的两个模型过程说明及时可见。
    class ReadTwiceThenReply(_StaticBackend):
        # LLM: 工具调用归原agent执行；raw callback不得进入公开块，下一次模型调用前必须已有前次完整响应说明。
        # 函数用途: 返回两次read_file和最终答复，同时检查实际提交时机与调用计数。
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            before = _visible(runtime)
            assert before == ''.join(f'第{index}次核对资料。' for index in range(1, self.calls))
            if stream:
                for chunk in ['{"password":"', _SECRET, '"}']:
                    on_chunk(chunk)
                    assert _visible(runtime) == before
            if self.calls < 3:
                return ModelResponse(text=f'第{self.calls}次核对资料。', backend=self.name, tool_use_blocks=[
                    {'id': f'read-{self.calls}', 'name': 'read_file', 'input': {'path': str(path)}}])
            return ModelResponse(text='最终核对完成。', backend=self.name)

    agent.backend = ReadTwiceThenReply()
    result = agent.run('核对当前资料', allowed_tools=['read_file'], on_chunk=turn, save=False)
    runtime.complete_turn('local-real-loop-rev02', TuiTurnSummary(response_text=result.response))
    assert result.response == '最终核对完成。' and result.tool_rounds == 2 and agent.backend.calls == 3
    assert _visible(runtime) == '第1次核对资料。第2次核对资料。最终核对完成。'
    assert not runtime.store.snapshot().active_blocks
