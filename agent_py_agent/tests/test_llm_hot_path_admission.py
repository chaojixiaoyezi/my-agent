"""T4 层4:全局 LLM 并发闸接进热路径——默认关零变化,配了才封顶+背压。"""

from __future__ import annotations

import threading
import time

import pytest
from agent.backends.errors import ProviderTransientError, is_provider_transient_error
from agent.llm_scale import hot_path


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("LLM_MAX_INFLIGHT", raising=False)
    monkeypatch.delenv("LLM_ADMISSION_WAIT_SECONDS", raising=False)
    hot_path.reset_hot_path_admission_for_test()
    yield
    hot_path.reset_hot_path_admission_for_test()


def test_default_off_is_nullcontext_no_limit(monkeypatch):
    # 未配 LLM_MAX_INFLIGHT → 任意并发都放行(默认路径零变化)。
    entered = []
    for _ in range(50):
        with hot_path.global_llm_admission_slot():
            entered.append(1)
    assert len(entered) == 50


def test_cap_enforced_when_configured(monkeypatch):
    monkeypatch.setenv("LLM_MAX_INFLIGHT", "2")
    hot_path.reset_hot_path_admission_for_test()

    holding = threading.Semaphore(0)
    release = threading.Event()
    peak = {"n": 0, "cur": 0}
    lock = threading.Lock()

    def worker():
        with hot_path.global_llm_admission_slot():
            with lock:
                peak["cur"] += 1
                peak["n"] = max(peak["n"], peak["cur"])
            holding.release()
            release.wait(2.0)
            with lock:
                peak["cur"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    # 只有 2 个能同时持槽(其余阻塞)。等前 2 个进入。
    holding.acquire()
    holding.acquire()
    time.sleep(0.2)
    assert peak["n"] == 2, f"并发上限没生效: peak={peak['n']}"
    release.set()
    for t in threads:
        t.join(3.0)
    assert peak["n"] == 2


def test_saturation_raises_retryable_transient(monkeypatch):
    monkeypatch.setenv("LLM_MAX_INFLIGHT", "1")
    monkeypatch.setenv("LLM_ADMISSION_WAIT_SECONDS", "0")  # 立刻超时,不等
    hot_path.reset_hot_path_admission_for_test()

    with hot_path.global_llm_admission_slot():
        # 槽已被占,第二个 0s 超时 → ProviderTransientError(可退避重试=背压)。
        with pytest.raises(ProviderTransientError) as caught:
            with hot_path.global_llm_admission_slot():
                pass
    assert is_provider_transient_error(caught.value)
