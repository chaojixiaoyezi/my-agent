from __future__ import annotations

from pathlib import Path


# LLM: owner capability requests outlive one subagent run but remain scoped to the owner home.
# 函数用途: 验证能力申请写在 owner home，子代理结束后仍可由父代理/用户查看和处理。
def test_owner_capability_request_lifecycle(tmp_path: Path):
    from agent_py_agent.agent.user_space.capability_requests import (
        CreateCapabilityRequest,
        close_capability_request,
        create_capability_request,
        list_capability_requests,
    )
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)

    request = create_capability_request(
        home,
        CreateCapabilityRequest(
            requested_by="agent_sub_1",
            capability="browser_login",
            reason="需要登录页面确认结果",
            task_id="task_1",
        ),
    )
    closed = close_capability_request(home, request.request_id, status="expired", note="子代理已结束")

    rows = list_capability_requests(home)

    assert request.path.parent == home.owner_capability_requests_dir
    assert closed.status == "expired"
    assert rows[-1].request_id == request.request_id
    assert rows[-1].status == "expired"
