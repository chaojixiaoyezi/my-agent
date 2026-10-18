# LLM: worker启动错误仅解释原结构化状态，分类表不签发就绪、死亡或重试权。
# 模块用途: 验证真实worker错误码共享唯一分类，防止落入UNKNOWN_ERROR。
import pytest
from agent.contracts.error_taxonomy import error_contract


@pytest.mark.parametrize(
    ("code", "category"),
    [("WORKER_NOT_READY", "state"), ("WORKER_READY_REJECTED", "state"),
     ("WORKER_PLATFORM_UNSUPPORTED", "contract")],
)
def test_worker_start_error_contract(code, category):
    contract = error_contract(code)
    assert contract.code == code
    assert contract.category == category
    assert contract.retryable is False
    assert contract.recommended_action == "report_blocker"
