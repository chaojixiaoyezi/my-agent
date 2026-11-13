"""两空间共用宿主模型资源时，原生后台请求与授权摘要不混入其它线程正文。"""

import json
import time
from dataclasses import replace

from agent_py_agent.agent.backends.base import EchoBackend
from agent_py_agent.agent.gateway_parts.business_api import BusinessServices, local_business_context
from agent_py_agent.tests.test_business_v12_background_scheduling import (
    add_echo_profile,
    assigned_task,
    native,
    ok,
    scheduler_for,
    tick,
)


def test_two_native_spaces_keep_model_history_and_controller_projection_separate(
    native, monkeypatch  # noqa: F811
):
    _, host = native
    add_echo_profile(host)
    first, task_a = assigned_task(native, "scope-A")
    second, task_b = assigned_task(native, "scope-B")
    markers = {"A": "PRIVATE_OLD_CHAT_A_731", "B": "PRIVATE_OLD_CHAT_B_925"}
    for label, worker in [("A", first), ("B", second)]:
        host.conversation_store.messages.append(
            {
                "thread_id": worker["agent_id"],
                "role": "user",
                "content": markers[label],
                "channel": "local",
            }
        )
    requests = []
    original = EchoBackend.generate

    def capture(self, prompt, *args, **kwargs):
        requests.append(
            {
                "backend": id(self),
                "prompt": prompt,
                "messages": kwargs.get("messages"),
                "options": str(kwargs.get("request_options")),
            }
        )
        return original(self, prompt, *args, **kwargs)

    monkeypatch.setattr(EchoBackend, "generate", capture)
    scheduler = scheduler_for(host)
    for worker, task_id in [(first, task_a), (second, task_b)]:
        before = len(requests)
        reports = tick(scheduler, worker["agent_id"], time.time())
        assert isinstance(reports, list) and reports and reports[0].runtime_status == "ok", reports
        assert host.conversation_store.session_tasks.require(task_id).status == "done"
        assert len(requests) > before
        # 每次运行均沿同一宿主资源获取路径；测试捕获真正传给backend的原生请求。
        payload = json.dumps(requests[before:], ensure_ascii=False)
        other = markers["B" if worker == first else "A"]
        assert other not in payload
    # 请求不是两个空记录；两线程自己的任务输入确实被发送。
    assert all("compute order A total" in json.dumps(row, ensure_ascii=False) for row in requests)
    services = BusinessServices(host)
    context, _ = local_business_context(host)
    actor = services.observation_actor(context)
    projection = services.observation
    original_authorize = actor.authorize
    allowed_space = first["space_id"]
    restricted = replace(
        actor,
        authorize=lambda action, facts: (
            original_authorize(action, facts) and facts.get("space_id") in (None, allowed_space)
        ),
    )
    summary = projection.snapshot({"space_id": allowed_space}, actor=restricted)
    assert any(row["task_id"] == task_a for row in summary["tasks"])
    assert all(row["task_id"] != task_b for row in summary["tasks"])
    serialized = json.dumps(summary, ensure_ascii=False)
    assert all(marker not in serialized for marker in markers.values())
    # 聚合只读事实；任务正文不作为聊天历史复制到controller投影。
    assert "messages" not in summary
    assert ok(native, "tasks.get", {"task_id": task_a})["task"]["status"] == "done"
