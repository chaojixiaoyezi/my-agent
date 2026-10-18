# LLM: 仅native端点/worker容器是fake物理边界；根仍用真实临时C/control/Root，不把缺session签成空树或物理退出。
# 模块用途: 固定启动握手未完成时的排空诊断，防止二次AttributeError遮住真实启动失败。
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_drain_trees as trees
from agent_py_agent.agent.gateway_parts import multiprocess_native_runtime as native
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case


# LLM: 只替换无业务native端点的容器；不产生租约、P3、退出证明或执行权限，完整根检查仍为产品实现。
# 类用途: 表达原worker已建lane但握手还没创建session的物理中间态。
class UnattachedLane:
    # LLM: 没有真实进程或资源会话；仅保本次原引用供树观察测试读取。
    # 函数用途: 构造一个缺少session的端点形状。
    def __init__(self):
        self.executor_lane = SimpleNamespace(session=None)

    # LLM: 此fake只服务普通叶子测试，不能提供死亡或排空证据。
    # 函数用途: 保持物理边界调用接口，不代替C准入。
    def _require_original(self):
        return None


def test_unattached_session_reports_unavailable_instead_of_empty_tree(monkeypatch):
    monkeypatch.setattr(native, "NativeWorkerLane", UnattachedLane)
    monkeypatch.setattr(trees, "original_drain_sources", lambda _: ((), (), ()))
    runtime = SimpleNamespace(controller=object(), _workers=SimpleNamespace(native_lanes=(UnattachedLane(),)))
    with pytest.raises(ValueError, match="drain_native_executor_session_unavailable"):
        trees._original_objects(runtime)


def test_real_runtime_keeps_unattached_native_session_as_drain_blocker(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "NativeWorkerLane", UnattachedLane)
    with curator_case(tmp_path, monkeypatch) as (_agent, _store, _thread, gate, _claim, _close):
        runtime = GatewayDrainRuntime(gate.paths._coordinator_role_lease.control_plane)
        workers = SimpleNamespace(control=runtime.control, native_lanes=(UnattachedLane(),))
        runtime.retain_workers(workers)
        runtime.stop_producers()
        result = runtime.assess()
        assert len(result) == 1 and not result[0].drained
        assert any(item.item_ref == "original-sources" and item.reason == "ValueError" for item in result[0].blockers)
