# LLM: 本文件只验证测试用 running helper 的全局输出资源所有权；不替换公开投影或削弱秘密断言。
# 模块用途: 用真实 TUI 和末端异常注入，保证普通、嵌套及失败收尾都恢复进入前的 sink。
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from agent_py_agent.cli.chat_parts import rendering
from agent_py_agent.tests import test_tui_decision_menu as menu


# LLM: 此固定异常只用于证明异常对象原样传播，不承载模型或用户输入。
# 类用途: 区分被注入的资源生命周期故障。
class LifecycleFault(Exception):
    pass


@pytest.mark.parametrize('has_prior_sink', [False, True])
def test_running_restores_entry_sink(tmp_path, monkeypatch, has_prior_sink):
    # LLM: 沿原 asyncio.run 驱动本地资源，不发业务请求；异常与 sink 身份由本测试断言。
    # 函数用途: 在独立事件循环验证本次资源生命周期。
    async def scenario():
        prior = Mock() if has_prior_sink else None
        monkeypatch.setattr(rendering, '_TUI_OUTPUT_SINK', prior)
        async with menu.running(tmp_path, menu.Gateway(tmp_path)) as ui:
            owned = rendering._TUI_OUTPUT_SINK
            assert callable(owned) and owned is not prior
            assert ui.app.is_running
        assert not ui.app.is_running
        assert rendering._TUI_OUTPUT_SINK is prior

    asyncio.run(scenario())


@pytest.mark.parametrize('inner_fails', [False, True])
def test_nested_running_restores_outer_sink(tmp_path, monkeypatch, inner_fails):
    # LLM: 沿原 asyncio.run 驱动本地资源，不发业务请求；异常与 sink 身份由本测试断言。
    # 函数用途: 在独立事件循环验证本次资源生命周期。
    async def scenario():
        prior = Mock()
        monkeypatch.setattr(rendering, '_TUI_OUTPUT_SINK', prior)
        outer_path, inner_path = tmp_path / 'outer', tmp_path / 'inner'
        outer_path.mkdir()
        inner_path.mkdir()
        fault = LifecycleFault()
        async with menu.running(outer_path, menu.Gateway(outer_path)) as outer:
            outer_sink = rendering._TUI_OUTPUT_SINK
            try:
                async with menu.running(inner_path, menu.Gateway(inner_path)) as inner:
                    assert rendering._TUI_OUTPUT_SINK is not outer_sink
                    assert inner.app.is_running and outer.app.is_running
                    if inner_fails:
                        raise fault
            except LifecycleFault as caught:
                assert inner_fails and caught is fault
            assert not inner.app.is_running
            assert rendering._TUI_OUTPUT_SINK is outer_sink
            assert outer.app.is_running
        assert not outer.app.is_running
        assert rendering._TUI_OUTPUT_SINK is prior

    asyncio.run(scenario())


def test_running_restores_sink_after_body_failure(tmp_path, monkeypatch):
    # LLM: 沿原 asyncio.run 驱动本地资源，不发业务请求；异常与 sink 身份由本测试断言。
    # 函数用途: 在独立事件循环验证本次资源生命周期。
    async def scenario():
        prior, fault = Mock(), LifecycleFault()
        monkeypatch.setattr(rendering, '_TUI_OUTPUT_SINK', prior)
        with pytest.raises(LifecycleFault) as caught:
            async with menu.running(tmp_path, menu.Gateway(tmp_path)) as ui:
                assert rendering._TUI_OUTPUT_SINK is not prior
                raise fault
        assert caught.value is fault
        assert not ui.app.is_running
        assert rendering._TUI_OUTPUT_SINK is prior

    asyncio.run(scenario())


def test_running_restores_sink_after_setup_failure(tmp_path, monkeypatch):
    # LLM: 沿原 asyncio.run 驱动本地资源，不发业务请求；异常与 sink 身份由本测试断言。
    # 函数用途: 在独立事件循环验证本次资源生命周期。
    async def scenario():
        prior, fault = Mock(), LifecycleFault()
        monkeypatch.setattr(rendering, '_TUI_OUTPUT_SINK', prior)
        original_wait = menu.wait_ui
        observed = []

        # LLM: 先执行真实就绪等待，再在原 setup 边界失败；原 exit/await 必须仍被执行。
        # 函数用途: 注入已启动 TUI 的 setup 故障并保留真实 Application 身份。
        async def failed_wait(ui, predicate, what='TUI 状态'):
            await original_wait(ui, predicate, what)
            observed.append(ui.app)
            raise fault

        monkeypatch.setattr(menu, 'wait_ui', failed_wait)
        with pytest.raises(LifecycleFault) as caught:
            async with menu.running(tmp_path, menu.Gateway(tmp_path)):
                pytest.fail('setup 失败不能进入上下文正文')
        assert caught.value is fault
        assert len(observed) == 1 and not observed[0].is_running
        assert rendering._TUI_OUTPUT_SINK is prior

    asyncio.run(scenario())


def test_running_restores_sink_after_factory_failure(tmp_path, monkeypatch):
    # LLM: 沿原 asyncio.run 驱动本地资源，不发业务请求；异常与 sink 身份由本测试断言。
    # 函数用途: 在独立事件循环验证本次资源生命周期。
    async def scenario():
        prior, fault = Mock(), LifecycleFault()
        monkeypatch.setattr(rendering, '_TUI_OUTPUT_SINK', prior)
        original_make = menu.make_tui_app
        observed = []

        # LLM: 真实工厂已登记 sink 后才失败，不启动 Application 或旁路正文处理。
        # 函数用途: 覆盖 yield 和内层 try 之前的资源恢复。
        def failed_make(params):
            observed.append(original_make(params))
            assert rendering._TUI_OUTPUT_SINK is not prior
            raise fault

        monkeypatch.setattr(menu, 'make_tui_app', failed_make)
        with pytest.raises(LifecycleFault) as caught:
            async with menu.running(tmp_path, menu.Gateway(tmp_path)):
                pytest.fail('工厂失败不能进入上下文正文')
        assert caught.value is fault
        assert len(observed) == 1 and not observed[0].is_running
        assert rendering._TUI_OUTPUT_SINK is prior

    asyncio.run(scenario())


@pytest.mark.parametrize('boundary', ['exit', 'await'])
def test_running_restores_sink_without_swallowing_cleanup_failure(tmp_path, monkeypatch, boundary):
    # LLM: 沿原 asyncio.run 驱动本地资源，不发业务请求；异常与 sink 身份由本测试断言。
    # 函数用途: 在独立事件循环验证本次资源生命周期。
    async def scenario():
        prior, owned, fault = Mock(), Mock(), LifecycleFault()
        monkeypatch.setattr(rendering, '_TUI_OUTPUT_SINK', prior)
        app = SimpleNamespace(after_render=MagicMock(),
                              run_async=AsyncMock(side_effect=fault if boundary == 'await' else None),
                              exit=Mock(side_effect=fault if boundary == 'exit' else None))

        # LLM: 只在末端注入 exit/await 故障，避免 exit 抛错时遗留真实活动 App；正常路径另由真实 TUI 测试覆盖。
        # 函数用途: 提供可确定完成的受控 Application 及其专属 sink。
        def make_stub(params):
            rendering.set_tui_output_sink(owned)
            return app

        # LLM: 让受控 task 完成后再 yield，故障由原 cleanup 的 await 接回，不制造后台悬挂。
        # 函数用途: 替代仅依赖真实布局的就绪等待。
        async def ready_stub(ui, predicate):
            await asyncio.sleep(0)

        monkeypatch.setattr(menu, 'make_tui_app', make_stub)
        monkeypatch.setattr(menu, 'wait_ui', ready_stub)
        with pytest.raises(LifecycleFault) as caught:
            async with menu.running(tmp_path, menu.Gateway(tmp_path)):
                assert rendering._TUI_OUTPUT_SINK is owned
        assert caught.value is fault
        assert rendering._TUI_OUTPUT_SINK is prior
        app.exit.assert_called_once_with()
        app.run_async.assert_awaited_once_with(set_exception_handler=False)

    asyncio.run(scenario())
